import asyncio
import json
import tomllib
from pathlib import Path

import pytest

from contracts import validate
from services.harness.adapter import Adapter
from services.harness.settings import bind_recovery_owner, configure


def test_settings_exclude_personal_and_local_environment(tmp_path: Path) -> None:
    home = tmp_path / "attempt"
    env = configure(
        home,
        manager_url="http://manager:8082",
        manager_token="manager-test",
        project_id="project",
        handle="handle",
        run_id="run",
        proxy_url="http://proxy/v0/runs/run",
        inference_token="inference-test",
        model="mock-model",
    )
    settings = tomllib.loads((home / "environments.toml").read_text())
    config = tomllib.loads((home / "config.toml").read_text())
    assert settings["include_local"] is False
    assert settings["default"] == "sandbox"
    assert config["shell_environment_policy"] == {
        "inherit": "none",
        "set": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"},
    }
    assert "MANAGER_TOKEN" not in env
    assert "inference-test" not in json.dumps(config)
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
    bind_recovery_owner(home, {"EVAL_STAND_ATTEMPT_OWNER": "a" * 64})
    relay = tomllib.loads((home / "environments.toml").read_text())["environments"][0][
        "env"
    ]
    assert relay["CODEX_HOME"] == str(home)
    assert relay["EVAL_STAND_ATTEMPT_OWNER"] == "a" * 64
    assert (home / "environments.toml").stat().st_mode & 0o777 == 0o600


def test_spawn_failure_releases_empty_slot(tmp_path: Path) -> None:
    adapter = Adapter(str(tmp_path / "missing-codex"))
    result = asyncio.run(adapter.run("test", "mock-model", tmp_path, {}, 1))
    assert result["released"] is True
    assert result["execution_status"] == "transport_error"
    assert result["evidence_complete"] is False


def test_cancel_during_version_check_never_starts_app_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = Adapter()

    async def version(env: dict) -> None:
        await adapter.cancel()

    async def forbidden_spawn(*args, **kwargs) -> None:
        raise AssertionError("app-server started after cancellation")

    monkeypatch.setattr(adapter, "check_version", version)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    result = asyncio.run(adapter.run("task", "mock-codex", tmp_path, {}, 10))
    assert result["execution_status"] == "cancelled"
    assert result["released"] is True


def test_cancel_after_thread_start_never_sends_turn() -> None:
    adapter = Adapter()
    methods = []

    class Rpc:
        async def call(self, method: str, params: dict) -> dict:
            methods.append(method)
            if method == "thread/start":
                adapter.cancel_requested = True
                return {"thread": {"id": "thread"}}
            return {}

        async def send(self, message: dict) -> None:
            pass

    adapter.rpc = Rpc()
    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(adapter._initialize("task", "mock-codex"))
    assert "turn/start" not in methods


def test_relay_separate_group_is_stopped_after_parent_exit(tmp_path: Path) -> None:
    import os
    import sys

    from services.common.processes import session_members, stop_session

    async def scenario() -> None:
        child_file = tmp_path / "child.pid"
        child = (
            "import os,signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"open({str(child_file)!r},'w').write(str(os.getpid()));"
            "time.sleep(30)"
        )
        parent = (
            "import subprocess,sys;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}],process_group=0)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent,
            start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await process.wait()
            async with asyncio.timeout(3):
                while not child_file.exists() or not child_file.read_text():
                    await asyncio.sleep(0.01)
            child_pid = int(child_file.read_text())
            assert os.getpgid(child_pid) != process.pid
            assert child_pid in await session_members(process.pid)
            assert await stop_session(process, 3)
            assert await session_members(process.pid) == []
        finally:
            await stop_session(process, 2)
            if child_file.exists():
                try:
                    pid = int(child_file.read_text())
                    if os.getsid(pid) == process.pid:
                        os.kill(pid, 9)
                except ProcessLookupError:
                    pass

    asyncio.run(scenario())


def test_native_output_redaction_and_evidence_independent_of_stdout() -> None:
    adapter = Adapter(secrets=("private-test-token",))

    async def scenario() -> None:
        for phase in ("started", "completed"):
            await adapter.on_event(
                {
                    "method": "item/" + phase,
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "item": {
                            "id": "item",
                            "type": "commandExecution",
                            "command": "echo ok",
                            "cwd": "/work",
                            "source": "agent",
                            "exitCode": 0 if phase == "completed" else None,
                            "aggregatedOutput": "private-test-token" + "x" * 20000,
                        },
                    },
                }
            )

    asyncio.run(scenario())
    assert adapter.evidence_complete
    assert len(adapter.events) == 2
    assert adapter.traces[-1]["output_truncated"]
    assert "private-test-token" not in json.dumps(adapter.snapshot())
    assert adapter.traces[-1]["output"].startswith("[REDACTED]")
    validate(
        "evidence",
        {
            "version": "v0",
            "codex_version": "0.159.3",
            "run_id": "run",
            "thread_id": "thread",
            "turn_id": "turn",
            "complete": True,
            "protected_inputs": True,
            "input_manifest": {"input/stats.py": "a" * 64, "input/data.csv": "b" * 64},
            "events": [{**event, "run_id": "run"} for event in adapter.events],
        },
    )


@pytest.mark.parametrize(
    "model", ["mock-codex", "mock-codex-alt", "arbitrary-provider-model"]
)
def test_catalog_keeps_requested_slug_and_direct_native_tools(
    tmp_path: Path,
    model: str,
) -> None:
    home = tmp_path / "mock"
    configure(
        home,
        manager_url="http://manager:8082",
        manager_token="manager-test",
        project_id="project",
        handle="handle",
        run_id="run",
        proxy_url="http://proxy",
        inference_token="inference-test",
        model=model,
    )
    catalog = json.loads((home / "models.json").read_text())["models"]
    assert [item["slug"] for item in catalog] == [model]
    assert catalog[0]["tool_mode"] == "direct"
    assert catalog[0]["use_responses_lite"] is False
    assert catalog[0]["node_repl_disabled"] is True
    assert catalog[0]["apply_patch_tool_type"] == "freeform"
    assert catalog[0]["shell_type"] == "unified_exec"
