"""Детерминированный Responses-провайдер; инструменты исполняет настоящий Codex."""

from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aiohttp import web

from services.llm_proxy.profiles import MOCK_BUDGET, mock_input_tokens

DEFAULT_SCRIPT = (
    {
        "type": "function_call",
        "name": "exec_command",
        "arguments": json.dumps(
            {
                "cmd": "cat /work/container-marker.txt; env",
                "yield_time_ms": 1000,
            }
        ),
    },
    {
        "type": "custom_tool_call",
        "name": "apply_patch",
        "input": (
            "*** Begin Patch\n*** Add File: /work/route-proof.txt\n+из сандбокса\n*** End Patch"
        ),
    },
    {
        "type": "function_call",
        "name": "exec_command",
        "arguments": json.dumps(
            {
                "cmd": "cat /work/route-proof.txt",
                "yield_time_ms": 1000,
            }
        ),
    },
)


def encode_event(event: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


class MockProvider:
    def __init__(
        self, script: Sequence[dict[str, Any]], models: tuple[str, ...]
    ) -> None:
        self.script, self.models = tuple(script), models
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.calls = 0
        self.received: dict[str, int] = {}

    async def stats(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        if len(run_id) > 64:
            raise web.HTTPBadRequest()
        return web.json_response(
            {
                "requests_received": self.received.get(run_id, 0),
                "tool_calls": len(self.sessions.get(run_id, [])),
            }
        )

    async def capabilities(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "mechanism": MOCK_BUDGET,
                "models": list(self.models),
                "enforces_max_output_tokens": True,
            }
        )

    def next_item(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        issued = self.sessions.setdefault(run_id, [])
        results = {
            item.get("call_id"): item
            for item in body["input"]
            if isinstance(item, dict)
            and item.get("type") in {"function_call_output", "custom_tool_call_output"}
        }
        if issued:
            previous = issued[-1]
            expected_type = previous["type"].replace("_call", "_call_output")
            result = results.get(previous["call_id"])
            if (
                result is None
                or result.get("type") != expected_type
                or "output" not in result
            ):
                raise ValueError("missing_native_tool_result")
        if len(issued) < len(self.script):
            item = {
                **self.script[len(issued)],
                "id": "item_" + uuid.uuid4().hex,
                "call_id": "call_" + uuid.uuid4().hex,
                "status": "completed",
            }
            issued.append(item)
            return item
        return {
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex,
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "Проверка завершена.",
                    "annotations": [],
                }
            ],
        }

    async def responses(self, request: web.Request) -> web.Response:
        run_id = request.headers.get("X-Eval-Run-Id", "")
        if 0 < len(run_id) <= 64:
            self.received[run_id] = self.received.get(run_id, 0) + 1
        try:
            body = await request.json()
            if (
                not run_id
                or len(run_id) > 64
                or body.get("model") not in self.models
                or body.get("stream") is not True
                or not isinstance(body.get("input"), list)
            ):
                raise ValueError("invalid_request")
            maximum = body.get("max_output_tokens")
            if type(maximum) is not int or not 1 <= maximum <= 30_000:
                raise ValueError("missing_output_bound")
            item = self.next_item(run_id, body)
        except (ValueError, KeyError, TypeError) as error:
            code = str(error)
            logging.getLogger(__name__).warning(
                "mock_request_rejected code=%s",
                code
                if code
                in {
                    "missing_native_tool_result",
                    "invalid_request",
                    "missing_output_bound",
                }
                else "invalid_tool_cycle",
            )
            return web.json_response({"error": "invalid_tool_cycle"}, status=400)
        self.calls += 1
        output_tokens = (len(json.dumps(item, ensure_ascii=False).encode()) + 3) // 4
        limited = output_tokens > maximum
        output = [] if limited else [item]
        response_id = "resp_" + uuid.uuid4().hex
        input_tokens = mock_input_tokens(body)
        result = {
            "id": response_id,
            "object": "response",
            "model": body["model"],
            "status": "incomplete" if limited else "completed",
            "output": output,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0 if limited else output_tokens,
                "total_tokens": input_tokens + (0 if limited else output_tokens),
                "input_tokens_details": {"cached_tokens": 0},
            },
        }
        events = [
            {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "status": "in_progress",
                    "output": [],
                },
            }
        ]
        if not limited:
            events.extend(self.item_events(item))
        else:
            result["incomplete_details"] = {"reason": "max_output_tokens"}
        events.append(
            {
                "type": "response.incomplete" if limited else "response.completed",
                "response": result,
            }
        )
        return web.Response(
            body=b"".join(map(encode_event, events)), content_type="text/event-stream"
        )

    @staticmethod
    def item_events(item: dict[str, Any]) -> list[dict[str, Any]]:
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress"},
            }
        ]
        if item["type"] == "function_call":
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "arguments": item["arguments"],
                }
            )
        elif item["type"] == "custom_tool_call":
            events.append(
                {
                    "type": "response.custom_tool_call_input.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "input": item["input"],
                }
            )
        events.append(
            {"type": "response.output_item.done", "output_index": 0, "item": item}
        )
        return events


MOCK_KEY = web.AppKey("mock", MockProvider)


def create_mock_app(
    script: Sequence[dict[str, Any]] = DEFAULT_SCRIPT,
    models: tuple[str, ...] = ("mock-codex", "mock-codex-alt"),
) -> web.Application:
    provider = MockProvider(script, models)
    app = web.Application(client_max_size=1_048_576)
    app[MOCK_KEY] = provider
    app.router.add_get("/budget-capabilities", provider.capabilities)
    app.router.add_post("/responses", provider.responses)
    app.router.add_get("/stats/{run_id}", provider.stats)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Офлайн Responses-провайдер")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--script", type=Path, default=os.getenv("MOCK_SCRIPT_PATH") or None
    )
    args = parser.parse_args()
    script = DEFAULT_SCRIPT
    if args.script is not None:
        data = Path(args.script).read_bytes()
        if len(data) > 65536:
            raise ValueError("mock_script_too_large")
        script = json.loads(data)
        if not isinstance(script, list) or not 1 <= len(script) <= 32:
            raise ValueError("invalid_mock_script")
        for item in script:
            if not isinstance(item, dict) or item.get("type") not in {
                "function_call",
                "custom_tool_call",
            }:
                raise ValueError("invalid_mock_script_item")
            if item.get("name") not in {"exec_command", "apply_patch"}:
                raise ValueError("unsupported_mock_tool")
    web.run_app(
        create_mock_app(script), host=args.host, port=args.port, access_log=None
    )


if __name__ == "__main__":
    main()
