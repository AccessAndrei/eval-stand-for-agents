"""Адаптер штатного агентного цикла Codex 0.159.3."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from contracts.tool_evidence import B5_COMMAND_KIND, B5_CWD, B5_SOURCES, is_b5_command
from services.common.processes import (
    read_bounded,
    session_members,
    stop_group,
    stop_session,
)
from services.harness.rpc import Rpc, RpcError
from services.harness.settings import VERSION

EARLY_EVENT_LIMIT = 256
EARLY_EVENT_BYTES = 524_288
TRANSCRIPT_SAVE_WARNING = "Failed to save the conversation transcript;"


class AttemptCancelled(RuntimeError):
    """Отмена до отправки следующей операции агентного цикла."""


class Adapter:
    def __init__(
        self,
        binary: str = "/usr/local/bin/codex",
        secrets: tuple[str, ...] = (),
        *,
        mounted: bool = False,
        resume_thread_id: str | None = None,
    ) -> None:
        if resume_thread_id is not None and (
            not mounted or not isinstance(resume_thread_id, str) or not resume_thread_id
        ):
            raise ValueError("Недопустимый thread для продолжения")
        self.binary = binary
        self.secrets = tuple(value for value in secrets if value)
        self.mounted = mounted
        self.resume_thread_id = resume_thread_id
        self.messages: list[dict[str, str]] = []
        self.message_bytes = 0
        self.process: asyncio.subprocess.Process | None = None
        self.rpc: Rpc | None = None
        self.thread_id: str | None = resume_thread_id
        self.turn_id: str | None = None
        self._early_events: list[dict] | None = None
        self._early_event_bytes = 0
        self._events_closed = False
        self._rollout_failed = False
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
        self._cancel_event = asyncio.Event()
        self.released = False
        self.continuation_safe = False

    async def on_event(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if self.mounted:
            if (
                not isinstance(params, dict)
                or self.thread_id is None
                or params.get("threadId") != self.thread_id
            ):
                return
            if method == "warning":
                warning = params.get("message")
                if (
                    (self._early_events is not None or self.turn_id is not None)
                    and isinstance(warning, str)
                    and warning.startswith(TRANSCRIPT_SAVE_WARNING)
                ):
                    # При ошибке flush Codex может всё равно завершить turn.
                    self._rollout_failed = True
                return
            if (
                method not in {"turn/completed", "item/started", "item/completed"}
                or self._events_closed
                or self.finished.is_set()
            ):
                return
            if method == "turn/completed":
                terminal = params.get("turn")
                native_turn = terminal.get("id") if isinstance(terminal, dict) else None
            else:
                native_turn = params.get("turnId")
            if not isinstance(native_turn, str) or not native_turn:
                return
            if self._early_events is not None:
                # Codex может прислать события до ответа на turn/start.
                size = len(json.dumps(message))
                if (
                    len(self._early_events) >= EARLY_EVENT_LIMIT
                    or self._early_event_bytes + size > EARLY_EVENT_BYTES
                ):
                    raise RpcError("Превышен предел событий до подтверждения turn")
                self._early_events.append(message)
                self._early_event_bytes += size
                return
            if native_turn != self.turn_id:
                return
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
        kind = item.get("type")
        if self.mounted and kind == "agentMessage" and method == "item/completed":
            self._record_message(item)
        if kind in {"commandExecution", "fileChange"}:
            self.record_trace(item, method.split("/")[1])
        if kind != "commandExecution" and not (self.mounted and kind == "fileChange"):
            return
        item_id = item.get("id")
        if self.mounted:
            if not isinstance(item_id, str) or not item_id:
                self.evidence_complete = False
                return
            # В Codex идентификатор команды может происходить из call_id модели.
            item_id = self._item_digest(item_id)
        if method == "item/started":
            if self.mounted and len(self.tool_started) >= 256:
                self.evidence_complete = False
            else:
                self.tool_started[item_id] = time.monotonic()
        else:
            self.tool_started.pop(item_id, None)
        event = {
            "type": kind,
            "phase": method.split("/")[1],
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "item_id": item_id,
        }
        if not self.mounted:
            event.update(
                command=item.get("command"),
                cwd=item.get("cwd"),
                source=item.get("source"),
            )
        elif kind == "commandExecution" and item.get("cwd") == B5_CWD:
            command, source = item.get("command"), item.get("source")
            if (
                source in B5_SOURCES
                and isinstance(command, str)
                and len(command) <= 16_384
                and is_b5_command(command)
            ):
                event.update(command_kind=B5_COMMAND_KIND, source=source)
        if method == "item/completed" and kind == "commandExecution":
            exit_code = item.get("exitCode")
            event["exit_code"] = (
                exit_code if not self.mounted or type(exit_code) is int else None
            )
        if isinstance(event.get("command"), str):
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

    def _record_message(self, item: dict) -> None:
        text, item_id = item.get("text"), item.get("id")
        if (
            not isinstance(text, str)
            or not isinstance(item_id, str)
            or not 0 < len(item_id) <= 128
            or len(self.messages) >= 16
            or self.message_bytes >= 65536
        ):
            return
        item_id = self._item_digest(item_id)
        if any(message["item_id"] == item_id for message in self.messages):
            return
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        remaining = 65536 - self.message_bytes
        data = text.encode("utf-8", errors="replace")[:remaining]
        self.messages.append(
            {"item_id": item_id, "text": data.decode("utf-8", errors="ignore")}
        )
        self.message_bytes += len(data)

    @staticmethod
    def _item_digest(item_id: str) -> str:
        return hashlib.sha256(item_id.encode("utf-8", errors="replace")).hexdigest()

    def record_diagnostic(self, error: object) -> None:
        output = "" if self.mounted else json.dumps(error, ensure_ascii=False)
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
        output = ""
        if not self.mounted:
            output = item.get("aggregatedOutput") or ""
            if item["type"] == "fileChange":
                output = json.dumps(
                    [change.get("path") for change in item.get("changes", [])],
                    ensure_ascii=False,
                )
        for secret in self.secrets:
            output = output.replace(secret, "[REDACTED]")
        outcome = item.get("exitCode", item.get("status"))
        if (
            self.mounted
            and type(outcome) is not int
            and outcome not in ("inProgress", "completed", "failed", "declined")
        ):
            outcome = None
        trace = {
            "timestamp": datetime.now(UTC).isoformat(),
            "type": (
                "command_" if item["type"] == "commandExecution" else "file_change_"
            )
            + phase,
            "output": output[:16384],
            "output_truncated": len(output) > 16384,
            "outcome": str(outcome),
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
                self.continuation_safe = self.mounted
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
        tasks = [reader]
        cancelled_before_turn = False
        try:
            async with asyncio.timeout(timeout):
                self._check_cancelled()
                await self._initialize(prompt, model)
                tasks.extend(
                    (
                        asyncio.create_task(self.finished.wait()),
                        asyncio.create_task(self._watch_tools()),
                    )
                )
                if self.mounted:
                    tasks.append(asyncio.create_task(self._interrupt_on_cancel()))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except AttemptCancelled:
            self.execution_status = "cancelled"
            if self.mounted:
                self.thread_id = self.resume_thread_id
                cancelled_before_turn = True
        except asyncio.TimeoutError:
            self.execution_status = "timeout"
        except (RpcError, OSError, ValueError) as error:
            self.execution_status = "transport_error"
            self.evidence_complete = False
            self.record_diagnostic(
                {"code": type(error).__name__, "message": str(error)}
            )
        finally:
            try:
                if self.mounted:
                    # Канал ещё читается для остановки app-server, но события
                    # после дедлайна не изменяют принятый итог.
                    self._events_closed = True
                    graceful = await self._close_app_server(self.process)
                    self.released = graceful or await stop_session(self.process)
                    if graceful:
                        # Считать решение после всех предупреждений из stdout.
                        drained, _ = await asyncio.wait({reader}, timeout=1)
                        self.continuation_safe = (
                            reader in drained
                            and self.process.stdout.at_eof()
                            and not self._rollout_failed
                            and (
                                cancelled_before_turn
                                or (
                                    self.finished.is_set()
                                    and self.execution_status
                                    in {"success", "cancelled"}
                                )
                            )
                        )
                else:
                    self.released = await stop_session(self.process)
            finally:
                tasks.append(stderr)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        if self.cancel_requested and not self.mounted:
            self.execution_status = "cancelled"
        return self.snapshot()

    @staticmethod
    async def _close_app_server(process: asyncio.subprocess.Process) -> bool:
        assert process.stdin
        try:
            process.stdin.close()
            async with asyncio.timeout(5):
                if await process.wait() != 0:
                    return False
                while await session_members(process.pid):
                    await asyncio.sleep(0.05)
        except (OSError, TimeoutError):
            return False
        return True

    def snapshot(self) -> dict:
        snapshot = {
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
        if self.mounted:
            snapshot["messages"] = self.messages
            snapshot["continuation_safe"] = self.continuation_safe
        return snapshot

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
        environments = [{"environmentId": "sandbox", "cwd": "/work"}]
        routing = {
            "model": model,
            "modelProvider": "stand",
            "approvalPolicy": "never",
            "cwd": "/work",
            "environments": environments,
        }
        if self.resume_thread_id is None:
            response = await self.rpc.call(
                "thread/start",
                {
                    **routing,
                    "ephemeral": not self.mounted,
                    "allowProviderModelFallback": False,
                },
            )
        else:
            response = await self.rpc.call(
                "thread/resume",
                {**routing, "threadId": self.resume_thread_id, "excludeTurns": True},
            )
        thread_id = response["thread"]["id"]
        if self.mounted and (
            not isinstance(thread_id, str)
            or not thread_id
            or (
                self.resume_thread_id is not None and thread_id != self.resume_thread_id
            )
        ):
            raise RpcError("Идентификатор thread не подтверждён")
        self.thread_id = thread_id
        self._check_cancelled()
        self._early_events = [] if self.mounted else None
        try:
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
                    "environments": environments,
                },
            )
            turn_id = response["turn"]["id"]
            if self.mounted and (not isinstance(turn_id, str) or not turn_id):
                raise RpcError("Идентификатор turn не подтверждён")
            self.turn_id = turn_id
            early_events = self._early_events
        finally:
            self._early_events = None
            self._early_event_bytes = 0
        for message in early_events or ():
            await self.on_event(message)

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

    async def _interrupt_on_cancel(self) -> None:
        await self._cancel_event.wait()
        if self._events_closed or self.finished.is_set():
            return
        assert self.rpc and self.thread_id and self.turn_id
        try:
            async with asyncio.timeout(8):
                await self.rpc.call(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                )
                await self.finished.wait()
        except (RpcError, OSError, TimeoutError) as error:
            if not self.finished.is_set():
                raise RpcError("Остановка нативного turn не подтверждена") from error

    async def cancel(self) -> None:
        self.cancel_requested = True
        if self.mounted:
            self._cancel_event.set()
        elif self.process:
            await stop_session(self.process)
