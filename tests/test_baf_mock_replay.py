"""Детерминированное воспроизведение записей smoke-БАФ через Responses API."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from baf.runner.catalog import load_dataset
from services.llm_proxy.mock import create_mock_app, load_recording

ROOT = Path(__file__).resolve().parents[1]
PLAYLIST = ROOT / "tests/infra/mock-scripts/baf-smoke/playlist.json"


def _message(text: str, role: str = "user") -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def _result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": item["type"].replace("_call", "_call_output"),
        "call_id": item["call_id"],
        "output": "ok",
    }


def test_playlist_matches_dataset_prompts_and_trusted_single_task_files() -> None:
    tasks = load_dataset(ROOT / "baf/smoke.yaml")
    source = load_recording(PLAYLIST)
    assert source == {
        "\n".join(task.messages): load_recording(PLAYLIST.with_name(f"{task.id}.json"))
        for task in tasks
    }


def test_playlist_chooses_each_run_once_and_rejects_unknown_or_ambiguous() -> None:
    async def scenario() -> None:
        tasks = {task.id: task for task in load_dataset(ROOT / "baf/smoke.yaml")}
        text_a = "\n".join(tasks["B1.1"].messages)
        text_b = "\n".join(tasks["B8.1"].messages)
        model_a, model_b = "mock-codex", "mock-codex-alt"
        async with TestClient(
            TestServer(create_mock_app(load_recording(PLAYLIST)))
        ) as client:

            async def response(
                run_id: str, model: str, inputs: list[dict[str, Any]]
            ) -> tuple[int, dict[str, Any]]:
                reply = await client.post(
                    "/responses",
                    headers={"X-Eval-Run-Id": run_id},
                    json={
                        "model": model,
                        "stream": True,
                        "input": inputs,
                        "max_output_tokens": 2048,
                    },
                )
                if reply.status != 200:
                    return reply.status, await reply.json()
                frames = [
                    json.loads(line.removeprefix("data: "))
                    for line in (await reply.text()).splitlines()
                    if line.startswith("data: ")
                ]
                assert frames[-1]["type"] == "response.completed"
                return reply.status, frames[-1]["response"]["output"][0]

            assert await response("unknown", model_a, [_message(text_a + "?")]) == (
                400,
                {"error": "playlist_prompt_unmatched"},
            )
            assert await response(
                "ambiguous", model_a, [_message(text_a), _message(text_b)]
            ) == (400, {"error": "playlist_prompt_ambiguous"})
            context = _message(
                "<environment_context><cwd>/work</cwd></environment_context>"
            )
            status, first_a = await response(
                "run-a",
                model_a,
                [context, _message(text_b, "assistant"), _message(text_a)],
            )
            assert status == 200
            assert (
                json.loads(first_a["arguments"])["cmd"] == "cat /work/config/app.yaml"
            )
            status, first_b = await response("run-b", model_b, [_message(text_b)])
            assert status == 200
            assert json.loads(first_b["arguments"])["cmd"] == "cat /work/sales/*.csv"

            status, write_b = await response("run-b", model_b, [_result(first_b)])
            assert status == 200
            assert "*** Add File: /work/answer.txt\n+1025\n" in write_b["input"]
            status, write_a = await response(
                "run-a", model_a, [_message(text_b), _result(first_a)]
            )
            assert status == 200
            assert "*** Add File: /work/answer.txt\n+8137\n" in write_a["input"]
            for run, model, write in (
                ("run-a", model_a, write_a),
                ("run-b", model_b, write_b),
            ):
                status, final = await response(run, model, [_result(write)])
                assert status == 200 and final["type"] == "message"

    asyncio.run(scenario())


def test_playlist_rejects_path_escape_duplicates_and_oversized_recordings(
    tmp_path: Path,
) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    manifest = recordings / "playlist.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(PLAYLIST.with_name("B1.1.json").read_bytes())
    (recordings / "alias.json").symlink_to(outside)
    for invalid in ("../outside.json", str(outside), "alias.json"):
        manifest.write_text(
            json.dumps(
                {"version": 1, "entries": [{"prompt": "query", "script": invalid}]}
            )
        )
        with pytest.raises(ValueError, match="invalid_mock_playlist_script_path"):
            load_recording(manifest)

    inside = recordings / "inside.json"
    inside.write_bytes(outside.read_bytes())
    assert load_recording(inside) == load_recording(outside)
    entry = {"prompt": "query", "script": inside.name}
    manifest.write_text(json.dumps({"version": 1, "entries": [entry, entry]}))
    with pytest.raises(ValueError, match="duplicate_mock_playlist_prompt"):
        load_recording(manifest)

    inside.write_bytes(b" " * 65_537)
    manifest.write_text(json.dumps({"version": 1, "entries": [entry]}))
    with pytest.raises(ValueError, match="mock_script_too_large"):
        load_recording(manifest)
