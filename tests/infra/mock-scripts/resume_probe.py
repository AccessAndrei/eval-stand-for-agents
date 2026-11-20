"""Локальная запись mock для проверки продолжения Codex после отмены."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from aiohttp import web

A_FILE = "probe-resume-seed.txt"
B_FILE = "probe-resume-result.txt"
HEARTBEAT = "probe-resume-heartbeat.txt"
PIDS_FILE = "probe-resume-writer-pids.txt"
A_PROMPT = "Start the heartbeat writer. Preserve this memory marker for the next turn."
B_PROMPT = "Continue the previous conversation. Run the next tool on the same folder."
SEED = "workspace-seed"
VALUE = "resumed-same-mounted-workdir"
PARENT_NAME = "probe-resume-parent.py"
CHILD_NAME = "probe-resume-child.py"
PARENT = f"""\
import os
import subprocess
import sys
from pathlib import Path

Path('/work/{A_FILE}').write_text({SEED!r})
child = subprocess.Popen([sys.executable, '-I', '-S', '/work/{CHILD_NAME}'])
Path('/work/{PIDS_FILE}').write_text(f'{{os.getpid()}} {{child.pid}}\\n')
sys.exit(child.wait())
"""
CHILD = f"""\
import time
from pathlib import Path

for _ in range(350):
    with Path('/work/{HEARTBEAT}').open('a') as stream:
        stream.write('tick\\n')
    time.sleep(0.2)
"""
FIRST_TOOL = f"exec /usr/local/bin/python3 -I -S /work/{PARENT_NAME}"
NEXT_TOOL = (
    f"""test "$(cat /work/{A_FILE})" = {SEED} && """
    f"printf %s {VALUE} > /work/{B_FILE} && cat /work/{B_FILE}"
)


def command_item(command: str) -> dict[str, str]:
    return {
        "type": "function_call",
        "name": "exec_command",
        "arguments": json.dumps(
            {"cmd": command, "workdir": "/work", "yield_time_ms": 1000}
        ),
    }


def run_mock() -> None:
    from services.llm_proxy.mock import MockProvider, encode_event

    run_a = ""
    run_b = ""
    context = os.environ["PROBE_CONTEXT"]
    if not re.fullmatch(r"context-[0-9a-f]{32,64}", context):
        raise ValueError("Нужен синтетический PROBE_CONTEXT")
    model = "mock-codex"

    class ProofMock(MockProvider):
        def __init__(self) -> None:
            super().__init__((), (model,))
            self.expected = {}
            self.old_held = False
            self.old_closed = False
            self.context_retained = False
            self.resumed_output = False
            self.resumed_call: str | None = None
            self.new_held = False
            self.finish_b = asyncio.Event()

        def next_item(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
            nonlocal run_a, run_b
            user = [
                json.dumps(item.get("content"), ensure_ascii=False)
                for item in body["input"]
                if isinstance(item, dict) and item.get("role") == "user"
            ]
            if not run_a:
                if not user or A_PROMPT not in user[-1] or context not in user[-1]:
                    raise ValueError("foreign_probe_run")
                run_a = run_id
                self.expected[run_a] = MockProvider(
                    (command_item(FIRST_TOOL),), (model,)
                )
            elif run_a != run_id and not run_b and self.old_closed:
                run_b = run_id
                self.expected[run_b] = MockProvider(
                    (command_item(NEXT_TOOL),), (model,)
                )
            if run_id not in self.expected:
                raise ValueError("foreign_probe_run")
            provider = self.expected[run_id]
            if run_id == run_b:
                if (
                    len(user) < 2
                    or B_PROMPT not in user[-1]
                    or context in user[-1]
                    or not any(context in message for message in user[:-1])
                ):
                    raise ValueError("historical_user_context_missing")
                if self.resumed_call is not None:
                    self.resumed_output = any(
                        VALUE in json.dumps(item.get("output"))
                        for item in body["input"]
                        if isinstance(item, dict)
                        and item.get("type") == "function_call_output"
                        and item.get("call_id") == self.resumed_call
                    )
                    if not self.resumed_output:
                        raise ValueError("mounted_output_missing")
                self.context_retained = True
            item = provider.next_item(run_id, body)
            if run_id == run_b and item["type"] == "function_call":
                self.resumed_call = item["call_id"]
            return item

        async def responses(self, request: web.Request) -> web.StreamResponse:
            run = request.headers.get("X-Eval-Run-Id")
            response = await super().responses(request)
            if (
                run == run_b
                and self.received.get(run_b) == 2
                and response.status == 200
            ):
                self.new_held = True
                await asyncio.wait_for(self.finish_b.wait(), 16)
            if run != run_a or self.received.get(run_a) != 2 or response.status != 200:
                return response
            stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await stream.prepare(request)
            await stream.write(
                encode_event(
                    {
                        "type": "response.created",
                        "response": {
                            "id": "resp_resume_probe_old_held",
                            "status": "in_progress",
                            "output": [],
                        },
                    }
                )
            )
            self.old_held = True
            try:
                for _ in range(200):
                    await asyncio.sleep(0.2)
                    await stream.write(b": probe keepalive\n\n")
            except (asyncio.CancelledError, ConnectionResetError):
                self.old_closed = True
            return stream

        async def probe_status(self, _: web.Request) -> web.Response:
            return web.json_response(
                {
                    "old_held": self.old_held,
                    "old_closed": self.old_closed,
                    "old_requests": self.received.get(run_a, 0),
                    "new_requests": self.received.get(run_b, 0),
                    "new_held": self.new_held,
                    "context_retained": self.context_retained,
                    "resumed_output": self.resumed_output,
                }
            )

        async def allow_finish_b(self, _: web.Request) -> web.Response:
            if not self.new_held:
                raise web.HTTPConflict(text="new_mock_stream_is_not_held")
            self.finish_b.set()
            return web.json_response({"released": True})

    mock = ProofMock()
    app = web.Application(client_max_size=1_048_576)
    app.router.add_get("/budget-capabilities", mock.capabilities)
    app.router.add_post("/responses", mock.responses)
    app.router.add_get("/probe-status", mock.probe_status)
    app.router.add_post("/probe-finish", mock.allow_finish_b)
    web.run_app(
        app, host="0.0.0.0", port=8090, access_log=None, handler_cancellation=True
    )


if __name__ == "__main__":
    run_mock()
