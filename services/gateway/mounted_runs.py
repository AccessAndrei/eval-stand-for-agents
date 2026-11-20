"""Запуски на уже смонтированном /work и ограниченный буфер клиентских событий."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from contracts import validate
from contracts.mounted import validate_mounted
from services.common.storage import StorageBudgetExceeded, encode

from .clients import RemoteError
from .runtime import IO_ERRORS, Runtime
from .store import now

if TYPE_CHECKING:
    from .sessions import MountedSession, MountedSessions

COMMAND_FIELDS = (
    "type",
    "phase",
    "thread_id",
    "turn_id",
    "item_id",
    "exit_code",
    "command_kind",
    "source",
)
MIN_FINAL_RESERVE = 256 * 1024
MAX_EVENTS = 128
MAX_RECENT_RUNS = 16
LOGGER = logging.getLogger(__name__)


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class EventLog:
    events: deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    seen: set[tuple] = field(default_factory=set)
    seq: int = 0

    def publish(self, kind: str, **data: Any) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "type": kind, **data})

    def update(self, snapshot: dict) -> None:
        for item in snapshot.get("messages", [])[:64]:
            text = item.get("text")
            key = ("message", item.get("item_id"))
            if key not in self.seen and isinstance(text, str):
                self.seen.add(key)
                self.publish("message", text=text[:4096])
        for item in snapshot.get("events", [])[:256]:
            event = {key: item[key] for key in COMMAND_FIELDS if key in item}
            key = ("tool", event.get("item_id"), event.get("type"), event.get("phase"))
            if key not in self.seen and event.get("type") in {
                "commandExecution",
                "fileChange",
            }:
                self.seen.add(key)
                self.publish("tool", tool=event)


class MountedRuns:
    def __init__(self, runtime: Runtime, sessions: MountedSessions) -> None:
        self.runtime = runtime
        self.sessions = sessions
        self.root = runtime.store.path / ".mounted-runs"
        self.root.mkdir(exist_ok=True, mode=0o700)
        sync_directory(self.root.parent)
        self.logs: OrderedDict[tuple[str, str], EventLog] = OrderedDict()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.active: dict[str, str] = {}
        self.cancelled: set[tuple[str, str]] = set()

    def ensure_no_active_run(self, session_id: str) -> None:
        task = self.tasks.get(session_id)
        if task is not None and not task.done():
            raise web.HTTPConflict(text="run_active")

    def _session_runs(self, session_id: str) -> Iterator[dict]:
        for path in self.root.glob("*/receipt.json"):
            item = json.loads(path.read_text())
            if item["session_id"] == session_id:
                record = self._receipt(session_id, item["run_id"])
                if record is not None:
                    yield record

    def _require_prior_clearance(self, session_id: str) -> None:
        if any(
            not previous["result"]
            or previous["result"].get("continuation_ready") is not True
            for previous in self._session_runs(session_id)
        ):
            raise web.HTTPConflict(text="previous_run_unconfirmed")

    async def dispose_context(self, session_id: str) -> None:
        context_present = False
        closed = True
        for previous in self._session_runs(session_id):
            context_present = True
            if (previous["result"] or {}).get("proxy_closed") is not True:
                closure = self._result(previous)
                await self._close_proxy(previous, closure)
                closed = closed and closure["proxy_closed"]
        if not context_present:
            return
        reply = await self.runtime.harness.request(
            "DELETE", f"/v1/sessions/{session_id}", timeout=10
        )
        if not closed or reply.get("cleanup_status") != "success":
            raise RemoteError(502, "run_resources_unconfirmed")

    def _directory(self, run_id: str) -> Path:
        return self.root / run_id

    def _receipt(self, session_id: str, run_id: str) -> dict | None:
        directory = self._directory(run_id)
        path = directory / "receipt.json"
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        if record["session_id"] != session_id:
            return None
        terminal = directory / "result.json"
        record["result"] = (
            json.loads(terminal.read_text()) if terminal.exists() else None
        )
        return record

    @staticmethod
    def _public(record: dict) -> dict:
        keys = ("version", "session_id", "run_id", "task_id", "result")
        return {
            **{key: record[key] for key in keys},
            "state": "terminal" if record["result"] is not None else "running",
        }

    def _open(self, session: MountedSession, body: dict, fingerprint: str) -> dict:
        session_id, run_id = session.session_id, body["run_id"]
        disk = self.runtime.store.disk
        directory = self._directory(run_id)
        if directory.exists():
            raise web.HTTPConflict(text="run_conflict")
        key = f"mount-v1-{session_id}-{run_id}"
        reserve = disk.reserve(key + "-status", 4096)
        final = None
        path = directory / "receipt.json"
        try:
            final = disk.reserve(key, MIN_FINAL_RESERVE)
            directory.mkdir(mode=0o700)
            sync_directory(self.root)
            receipt = {
                "version": "v1",
                "session_id": session_id,
                "run_id": run_id,
                "task_id": body.get("task_id"),
                "started_at": now(),
                "profile": session.profile,
                "handle": session.manager_handle,
                "proxy_ref": secrets.token_hex(24),
                "fingerprint": fingerprint,
                "reserve": final.name,
            }
            disk.write_reserved(reserve, path, receipt)
        except (OSError, StorageBudgetExceeded):
            if final is not None and not path.exists():
                final.unlink(missing_ok=True)
            if directory.exists() and not path.exists():
                (directory / "receipt.json.pending").unlink(missing_ok=True)
                directory.rmdir()
            raise
        finally:
            reserve.unlink(missing_ok=True)
        return {**receipt, "result": None}

    async def start(self, request: web.Request) -> web.Response:
        async with self.sessions.locked(request) as session:
            body = await request.json()
            validate_mounted("run_start", body)
            if body.get("task_id") is not None:
                validate("task_id", body["task_id"])
            if not body["prompt"].strip():
                raise web.HTTPBadRequest(text="empty_prompt")
            fingerprint = hashlib.sha256(encode(body)).hexdigest()
            run_id = body["run_id"]
            record = self._receipt(session.session_id, run_id)
            if record is not None:
                if record["fingerprint"] != fingerprint:
                    raise web.HTTPConflict(text="run_conflict")
                return web.json_response(self._public(record))
            if session.state != "ready" or not session.manager_handle:
                raise web.HTTPConflict(text="workspace_not_ready")
            self.ensure_no_active_run(session.session_id)
            self._require_prior_clearance(session.session_id)
            try:
                record = self._open(session, body, fingerprint)
            except StorageBudgetExceeded:
                return web.json_response(
                    {"error": "storage_budget_exceeded"}, status=507
                )
            queue = EventLog()
            self.logs[(session.session_id, run_id)] = queue
            if len(self.logs) > MAX_RECENT_RUNS:
                self.logs.popitem(last=False)
            task = asyncio.create_task(
                self._execute(record, body, session.profile_snapshot, queue)
            )
            self.tasks[session.session_id] = task
            self.active[session.session_id] = run_id
            task.add_done_callback(
                lambda completed: self._finish_task(session.session_id, completed)
            )
        return web.json_response(self._public(record), status=202)

    def _finish_task(self, session_id: str, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            LOGGER.error("Не удалось сохранить итог смонтированного запуска")
        if self.tasks.get(session_id) is task:
            del self.tasks[session_id]
            self.active.pop(session_id, None)

    def _known(self, request: web.Request) -> tuple[tuple[str, str], dict]:
        session = self.sessions.owned(request)
        run_id = request.match_info["run_id"]
        validate_mounted("id", run_id)
        record = self._receipt(session.session_id, run_id)
        if record is None:
            raise web.HTTPNotFound()
        return (session.session_id, run_id), record

    async def status(self, request: web.Request) -> web.Response:
        _, record = self._known(request)
        return web.json_response(self._public(record))

    async def cancel(self, request: web.Request) -> web.Response:
        key, record = self._known(request)
        if record["result"] is None:
            self.cancelled.add(key)
        return web.json_response(self._public(record))

    def _events_after(
        self, key: tuple[str, str], record: dict, after: int
    ) -> list[dict]:
        queue = self.logs.get(key)
        if queue is not None:
            items = list(queue.events)
        elif record["result"] is not None:
            result = record["result"]
            items = [{"seq": result["last_seq"], "type": "result", "result": result}]
        else:
            items = []
        return [item for item in items if item["seq"] > after]

    async def events(self, request: web.Request) -> web.StreamResponse:
        key, record = self._known(request)
        after = int(request.query.get("after", "0"))
        if after < 0:
            raise web.HTTPBadRequest(text="invalid_cursor")
        entries = self._events_after(key, record, after)
        if entries and entries[0]["seq"] != after + 1:
            return web.json_response(
                {"error": "history_unavailable", "earliest_seq": entries[0]["seq"]},
                status=409,
            )
        stream = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await stream.prepare(request)
        try:
            async with asyncio.timeout(660):
                while True:
                    for entry in entries:
                        await stream.write(encode(entry) + b"\n")
                        after = entry["seq"]
                        if entry["type"] == "result":
                            return stream
                    if record["result"] is not None:
                        return stream
                    await asyncio.sleep(0.1)
                    record = self._receipt(*key)
                    entries = self._events_after(key, record, after)
                    if entries and entries[0]["seq"] != after + 1:
                        return stream
        except (ConnectionError, TimeoutError):
            pass
        return stream

    async def _execute(
        self, record: dict, request: dict, pinned_profile: dict, queue: EventLog
    ) -> None:
        key = (record["session_id"], record["run_id"])
        runtime = self.runtime
        proxy_path = f"/v1/runs/{record['proxy_ref']}"
        manager_path = f"/v1/environments/{record['handle']}"
        harness_path = f"/v1/sessions/{key[0]}/runs/{key[1]}"
        identity = self._identity(record)
        control = {
            **identity,
            "project_id": runtime.project_id,
            "handle": record["handle"],
        }
        result = self._result(record)
        started = time.monotonic()
        stage = "provider"
        proxy_attempted = False
        reservation_attempted = False
        harness_attempted = False
        native: dict = {}
        try:
            proxy_attempted = True
            grant = await runtime.proxy.request(
                "POST", proxy_path, {**identity, "provider_profile": record["profile"]}
            )
            self._check_binding(identity, grant)
            snapshot = grant["provider_snapshot"]
            if not isinstance(snapshot, dict) or any(
                snapshot.get(name) != value for name, value in pinned_profile.items()
            ):
                raise RemoteError(409, "profile_changed")
            result.update(model=grant["model"], provider_snapshot=snapshot)
            stage = "sandbox"
            reservation_attempted = True
            await runtime.manager.request(
                "POST",
                manager_path + "/runs",
                {**control, "timeout_s": request["timeout_s"]},
            )
            if key in self.cancelled:
                result["execution_status"] = "cancelled"
                result["harness_released"] = True
                result["native_continuation_safe"] = True
            else:
                stage = "execution"
                harness_attempted = True
                await runtime.harness.request(
                    "POST",
                    "/v1/runs",
                    {
                        "request": {
                            **identity,
                            "prompt": request["prompt"],
                            "timeout_s": request["timeout_s"],
                        },
                        "project_id": runtime.project_id,
                        "handle": record["handle"],
                        "proxy_url": runtime.proxy.url + proxy_path,
                        "inference_token": grant["inference_token"],
                        "model": grant["model"],
                    },
                )
                native = await self._wait(
                    harness_path, key, request["timeout_s"] + 90, queue
                )
                result["execution_status"] = native["execution_status"]
                result["harness_released"] = native.get("released") is True
                result["native_continuation_safe"] = (
                    native.get("continuation_safe") is True
                )
                if not result["harness_released"]:
                    runtime.error(result, "execution", "relay_shutdown_unconfirmed")
        except IO_ERRORS as error:
            code = (
                "timeout"
                if isinstance(error, TimeoutError)
                else {
                    "provider": "provider_error",
                    "sandbox": "sandbox_error",
                    "execution": "transport_error",
                }[stage]
            )
            result["execution_status"] = code
            runtime.error(result, stage, code)
            if isinstance(error, RemoteError) and error.code != code:
                runtime.error(result, stage, error.code)
            if harness_attempted:
                try:
                    await runtime.harness.request("POST", harness_path + "/cancel")
                except IO_ERRORS:
                    runtime.error(result, "execution", "cancel_unconfirmed")
        finally:
            if reservation_attempted:
                await self._collect(record, result)
            if proxy_attempted:
                await self._close_proxy(record, result)
            if native:
                result["evidence"] = {
                    **identity,
                    "codex_version": native.get("codex_version"),
                    "thread_id": native.get("thread_id"),
                    "turn_id": native.get("turn_id"),
                    "events": [
                        {
                            field: item[field]
                            for field in COMMAND_FIELDS
                            if field in item
                        }
                        for item in native.get("events", [])[:256]
                    ],
                }
                if (
                    record["task_id"] == "B5.1"
                    and native.get("evidence_complete") is True
                ):
                    result["evidence_status"] = "complete"
            if result["execution_status"] != "success" and not any(
                error["code"] == result["execution_status"]
                for error in result["errors"]
            ):
                runtime.error(result, "execution", result["execution_status"])
            result["continuation_ready"] = (
                result["harness_released"]
                and result["native_continuation_safe"]
                and result["proxy_closed"]
                and result["writers_stopped"]
                and result["collection_status"] == "complete"
                and result["execution_status"]
                in {"success", "cancelled", "budget_exceeded"}
            )
            self._finish(record, result, time.monotonic() - started, queue)
            self.cancelled.discard(key)

    @staticmethod
    def _identity(record: dict) -> dict:
        return {key: record[key] for key in ("version", "session_id", "run_id")}

    def _result(self, record: dict) -> dict:
        return {
            **self._identity(record),
            "trace_id": record["run_id"],
            "task_id": record["task_id"],
            "provider_profile": record["profile"],
            "model": None,
            "provider_snapshot": None,
            "execution_status": "interrupted",
            "collection_status": "not_started",
            "writers_stopped": False,
            "harness_released": False,
            "native_continuation_safe": False,
            "proxy_closed": False,
            "continuation_ready": False,
            "evidence_status": "incomplete"
            if record["task_id"] == "B5.1"
            else "not_required",
            "evidence": None,
            "errors": [],
            "tokens": self.runtime.unknown_tokens({}, "not_started"),
            "provenance": self.runtime.provenance,
            "started_at": record.get("started_at", now()),
        }

    async def _collect(self, record: dict, result: dict) -> None:
        identity = self._identity(record)
        handle = record["handle"]
        control = {**identity, "project_id": self.runtime.project_id, "handle": handle}
        result["collection_status"] = "incomplete"
        try:
            collection = await self.runtime.manager.request(
                "POST", f"/v1/environments/{handle}/collect", control, 55
            )
            validate_mounted("manager_collection", collection)
            if any(collection.get(field) != value for field, value in identity.items()):
                raise ValueError("collection_identity_mismatch")
            result.update(
                writers_stopped=collection["writers_stopped"],
                collection_status=collection["collection_status"],
            )
            for error in collection["errors"]:
                code = error.get("code")
                if code in {"transport_error", "timeout"}:
                    if result["execution_status"] == "success":
                        result["execution_status"] = code
                    self.runtime.error(result, "execution", code)
                elif code in {"workspace_mount_lost", "writers_unconfirmed"}:
                    self.runtime.error(result, "collection", code)
        except IO_ERRORS:
            self.runtime.error(result, "collection", "collection_unconfirmed")

    async def _close_proxy(self, record: dict, result: dict) -> None:
        try:
            tokens = await self.runtime.proxy.request(
                "POST", f"/v1/runs/{record['proxy_ref']}/close", timeout=15
            )
            self._check_binding(self._identity(record), tokens)
            if tokens["closed"] is not True:
                raise ValueError("proxy_close_unconfirmed")
            result["proxy_closed"] = True
            result["tokens"] = tokens["tokens"]
            if tokens.get("error") in {"budget_exceeded", "auth_error"}:
                self.runtime.error(result, "provider", tokens["error"])
                if result["execution_status"] in {"success", "provider_error"}:
                    result["execution_status"] = tokens["error"]
        except IO_ERRORS as error:
            if isinstance(error, RemoteError) and error.code == "proxy_ref_fenced":
                result["proxy_closed"] = True
            else:
                self.runtime.error(result, "provider", "token_accounting_unavailable")

    @staticmethod
    def _check_binding(identity: dict, status: dict) -> None:
        if any(status.get(key) != value for key, value in identity.items()):
            raise ValueError("proxy_run_identity_mismatch")

    def _finish(
        self, record: dict, result: dict, duration: float, queue: EventLog
    ) -> None:
        result.update(finished_at=now(), duration_s=duration, last_seq=queue.seq + 1)
        validate_mounted("run_result", result)
        self.runtime.store.disk.finalize(
            self.runtime.store.path / record["reserve"],
            self._directory(record["run_id"]) / "result.json",
            result,
        )
        queue.publish("result", result=result)

    async def recover(self, _: web.Application) -> None:
        async with asyncio.timeout(90):
            for path in sorted(self.root.glob("*/receipt.json")):
                if (path.parent / "result.json").exists():
                    continue
                record = json.loads(path.read_text())
                started = time.monotonic()
                result = self._result(record)
                self.runtime.error(result, "execution", "interrupted")
                try:
                    endpoint = (
                        f"/v1/sessions/{record['session_id']}/runs/{record['run_id']}"
                    )
                    native = await self.runtime.harness.request(
                        "GET", endpoint, timeout=8
                    )
                    if (
                        native["session_id"] == record["session_id"]
                        and not native["done"]
                    ):
                        await self.runtime.harness.request(
                            "POST", endpoint + "/cancel", timeout=10
                        )
                except IO_ERRORS:
                    self.runtime.error(result, "execution", "previous_turn_unconfirmed")
                await self._close_proxy(record, result)
                await self._collect(record, result)
                self._finish(record, result, time.monotonic() - started, EventLog())

    async def _wait(
        self, path: str, key: tuple[str, str], timeout: int, queue: EventLog
    ) -> dict:
        cancel_sent = False
        async with asyncio.timeout(timeout):
            while True:
                snapshot = await self.runtime.harness.request("GET", path)
                if (snapshot.get("session_id"), snapshot.get("run_id")) != key:
                    raise ValueError("harness_run_identity_mismatch")
                queue.update(snapshot)
                if snapshot["done"]:
                    return snapshot
                if key in self.cancelled and not cancel_sent:
                    await self.runtime.harness.request("POST", path + "/cancel")
                    cancel_sent = True
                await asyncio.sleep(0.1)

    async def shutdown(self, _: web.Application) -> None:
        self.cancelled.update(self.active.items())
        async with asyncio.timeout(90):
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def register_mounted_runs(
    app: web.Application, runtime: Runtime, sessions: MountedSessions
) -> None:
    runs = MountedRuns(runtime, sessions)
    sessions.require_inactive = runs.ensure_no_active_run
    sessions.dispose_context = runs.dispose_context
    prefix = "/v1/sessions/{session_id}/runs"
    app.add_routes(
        [
            web.post(prefix, runs.start),
            web.get(prefix + "/{run_id}", runs.status),
            web.get(prefix + "/{run_id}/events", runs.events),
            web.post(prefix + "/{run_id}/cancel", runs.cancel),
        ]
    )
    app.on_startup.append(runs.recover)
    app.on_shutdown.append(runs.shutdown)
