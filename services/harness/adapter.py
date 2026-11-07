"""Адаптер штатного агентного цикла Codex 0.159.3."""

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from services.common.processes import read_bounded, stop_group, stop_session
from services.harness.rpc import Rpc, RpcError
from services.harness.settings import VERSION


class AttemptCancelled(RuntimeError):
    """Отмена до отправки следующей операции агентного цикла."""


class Adapter:
    def __init__(
        self, binary: str = "/usr/local/bin/codex", secrets: tuple[str, ...] = ()
    ) -> None:
        self.binary = binary
        self.secrets = tuple(value for value in secrets if value)
        self.process: asyncio.subprocess.Process | None = None
        self.rpc: Rpc | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.finished = asyncio.Event()
        self.events: list[dict] = []
        self.traces: list[dict] = []
        self.trace_bytes = 0
        self.trace_truncated = False
        self.evidence_bytes = 0
        self.evidence_complete = True
        self.execution_status = "transport_error"
        self.tool_started: dict[str, float] = {}
        self.cancel_requested = False
        self.released = False

    async def on_event(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if method == "turn/completed":
            status = params.get("turn", {}).get("status")
            if status == "failed":
                self.record_diagnostic(params.get("turn", {}).get("error", {}))
            self.execution_status = {
                "completed": "success",
                "interrupted": "cancelled",
                "failed": "provider_error",
            }.get(status, "transport_error")
            self.finished.set()
        if method not in {"item/started", "item/completed"}:
            return
        item = params.get("item", {})
        if item.get("type") in {"commandExecution", "fileChange"}:
            self.record_trace(item, method.split("/")[1])
        if item.get("type") != "commandExecution":
            return
        item_id = item.get("id")
        if method == "item/started":
            self.tool_started[item_id] = time.monotonic()
        else:
            self.tool_started.pop(item_id, None)
        event = {
            "type": "commandExecution",
            "phase": method.split("/")[1],
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "item_id": item_id,
            "command": item.get("command"),
            "cwd": item.get("cwd"),
            "source": item.get("source"),
        }
        if method == "item/completed":
            event["exit_code"] = item.get("exitCode")
        if isinstance(event["command"], str):
            for secret in self.secrets:
                if secret in event["command"]:
                    event["command"] = event["command"].replace(secret, "[REDACTED]")
                    self.evidence_complete = False
        size = len(json.dumps(event))
        if (
            len(self.events) >= 256
            or size > 16384
            or self.evidence_bytes + size > 524288
        ):
            self.evidence_complete = False
        else:
            self.events.append(event)
            self.evidence_bytes += size

    def record_diagnostic(self, error: object) -> None:
        output = json.dumps(error, ensure_ascii=False)
        for secret in self.secrets:
            output = output.replace(secret, "[REDACTED]")
        self.traces.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "harness_error",
                "output": output[:4096],
                "output_truncated": len(output) > 4096,
                "outcome": "failed",
            }
        )

    def record_trace(self, item: dict, phase: str) -> None:
        output = item.get("aggregatedOutput") or ""
        if item["type"] == "fileChange":
            output = json.dumps(
                [change.get("path") for change in item.get("changes", [])],
                ensure_ascii=False,
            )
        for secret in self.secrets:
            output = output.replace(secret, "[REDACTED]")
        trace = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": (
                "command_" if item["type"] == "commandExecution" else "file_change_"
            )
            + phase,
            "output": output[:16384],
            "output_truncated": len(output) > 16384,
            "outcome": str(item.get("exitCode", item.get("status"))),
        }
        if self.trace_bytes + len(json.dumps(trace)) > 524288:
            trace.update(output="", output_truncated=True)
            self.trace_truncated = True
        if (
            len(self.traces) < 512
            and self.trace_bytes + len(json.dumps(trace)) <= 524288
        ):
            self.traces.append(trace)
            self.trace_bytes += len(json.dumps(trace))
        else:
            self.trace_truncated = True

    async def run(
        self, prompt: str, model: str, home: Path, env: dict, timeout: int
    ) -> dict:
        try:
            await self.check_version(env)
            if self.cancel_requested:
                self.execution_status = "cancelled"
                self.released = True
                return self.snapshot()
            self.process = await asyncio.create_subprocess_exec(
                self.binary,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                cwd=home,
                env=env,
                limit=2_000_000,
            )
        except (OSError, ValueError, asyncio.TimeoutError):
            self.released = True
            self.evidence_complete = False
            return self.snapshot()
        assert self.process.stdout and self.process.stdin and self.process.stderr
        self.rpc = Rpc(self.process.stdout, self.process.stdin, self.on_event)
        reader = asyncio.create_task(self.rpc.read())
        stderr = asyncio.create_task(read_bounded(self.process.stderr, 65536))
        completion: asyncio.Task | None = None
        watchdog: asyncio.Task | None = None
        try:
            async with asyncio.timeout(timeout):
                self._check_cancelled()
                await self._initialize(prompt, model)
                completion = asyncio.create_task(self.finished.wait())
                watchdog = asyncio.create_task(self._watch_tools())
                done, _ = await asyncio.wait(
                    [completion, reader, watchdog], return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
        except AttemptCancelled:
            self.execution_status = "cancelled"
        except asyncio.TimeoutError:
            self.execution_status = "timeout"
        except (RpcError, OSError, ValueError) as error:
            self.execution_status = "transport_error"
            self.evidence_complete = False
            self.record_diagnostic(
                {"code": type(error).__name__, "message": str(error)}
            )
        finally:
            self.released = await stop_session(self.process)
            for task in (reader, stderr, completion, watchdog):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (reader, stderr, completion, watchdog) if task),
                return_exceptions=True,
            )
        if self.cancel_requested:
            self.execution_status = "cancelled"
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "execution_status": self.execution_status,
            "released": self.released,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "events": self.events,
            "traces": self.traces,
            "trace_truncated": self.trace_truncated,
            "evidence_complete": self.evidence_complete and not self.tool_started,
            "codex_version": VERSION,
        }

    async def check_version(self, attempt_env: dict[str, str] | None = None) -> None:
        environment = {"PATH": "/usr/local/bin:/usr/bin:/bin"}
        for key in ("CODEX_HOME", "EVAL_STAND_ATTEMPT_OWNER"):
            if attempt_env and key in attempt_env:
                environment[key] = attempt_env[key]
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        assert process.stdout
        reader = asyncio.create_task(read_bounded(process.stdout, 1024))
        try:
            async with asyncio.timeout(10):
                await process.wait()
                output, truncated = await reader
            if (
                process.returncode != 0
                or truncated
                or output.strip() != f"codex-cli {VERSION}".encode()
            ):
                raise ValueError("Неподдерживаемая версия Codex")
        finally:
            await stop_group(process)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _initialize(self, prompt: str, model: str) -> None:
        assert self.rpc
        self._check_cancelled()
        await self.rpc.call(
            "initialize",
            {
                "clientInfo": {"name": "eval-stand", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.rpc.send({"method": "initialized", "params": {}})
        self._check_cancelled()
        response = await self.rpc.call(
            "thread/start",
            {
                "model": model,
                "modelProvider": "stand",
                "ephemeral": True,
                "allowProviderModelFallback": False,
                "approvalPolicy": "never",
                "cwd": "/work",
                "environments": [{"environmentId": "sandbox", "cwd": "/work"}],
            },
        )
        self.thread_id = response["thread"]["id"]
        self._check_cancelled()
        response = await self.rpc.call(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "model": model,
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "externalSandbox",
                    "networkAccess": "restricted",
                },
                "environments": [{"environmentId": "sandbox", "cwd": "/work"}],
            },
        )
        self.turn_id = response["turn"]["id"]

    def _check_cancelled(self) -> None:
        if self.cancel_requested:
            raise AttemptCancelled("cancelled")

    async def _watch_tools(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            if any(
                time.monotonic() - start > 120 for start in self.tool_started.values()
            ):
                raise asyncio.TimeoutError("Истёк срок инструмента")

    async def cancel(self) -> None:
        self.cancel_requested = True
        if self.process:
            await stop_session(self.process)
