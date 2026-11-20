"""Владение одним харнессом до устойчивого подтверждения освобождения."""

import asyncio
import hashlib
import json
import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from contracts import ContractError, validate
from contracts.mounted import validate_mounted
from services.common.http import authenticated
from services.harness.adapter import Adapter
from services.harness.recovery import HarnessRecovery, RecoveryError
from services.harness.settings import bind_recovery_owner, configure


@dataclass
class MountedChat:
    binding: tuple[str, str, str]
    thread_id: str | None = None
    blocked: bool = False


def mounted_launch(data: dict) -> tuple[str, str]:
    if (
        not isinstance(data, dict)
        or data.keys()
        != {"request", "project_id", "handle", "proxy_url", "inference_token", "model"}
        or any(
            not isinstance(value, str) or not value
            for key, value in data.items()
            if key != "request"
        )
    ):
        raise web.HTTPBadRequest(text="invalid_mounted_launch")
    launch = data["request"]
    if not isinstance(launch, dict) or launch.keys() != {
        "version",
        "session_id",
        "run_id",
        "prompt",
        "timeout_s",
    }:
        raise web.HTTPBadRequest(text="invalid_mounted_launch")
    try:
        validate_mounted(
            "manager_turn",
            {
                **{
                    key: launch[key]
                    for key in ("version", "session_id", "run_id", "timeout_s")
                },
                **{key: data[key] for key in ("project_id", "handle")},
            },
        )
        validate_mounted(
            "run_start", {key: launch[key] for key in ("run_id", "prompt", "timeout_s")}
        )
        for key in ("session_id", "run_id"):
            validate(
                "lookup",
                {
                    "version": "v0",
                    "project_id": data["project_id"],
                    "run_id": launch[key],
                },
            )
    except ContractError as error:
        raise web.HTTPBadRequest(text="invalid_mounted_launch") from error
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return launch["session_id"], hashlib.sha256(encoded).hexdigest()


def create_app(
    token: str,
    state: Path,
    manager_url: str,
    manager_token: str,
    *,
    mounted_state: Path = Path("/tmp/eval-stand-mounted-codex"),
) -> web.Application:
    state = state.absolute()
    app = web.Application(middlewares=[authenticated(token)], client_max_size=2_500_000)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    recovery = HarnessRecovery(state, mounted_state=mounted_state)
    active: dict[str, tuple[Adapter, asyncio.Task, str | None]] = {}
    recent_messages: OrderedDict[str, tuple[str, list[dict[str, str]]]] = OrderedDict()
    chats: dict[str, MountedChat] = {}
    fenced_sessions: set[str] = set()
    controls: dict[str, bool] = {}
    failed: dict[str, dict] = {}
    lock = asyncio.Lock()
    ready = False

    def blocked() -> bool:
        return any(
            not released
            for run_id, released in controls.items()
            if run_id not in active
        )

    async def startup(application: web.Application) -> None:
        nonlocal ready
        restored = await recovery.reconcile(controls_only=True)
        controls.update(
            {run_id: status["released"] for run_id, status in restored.items()}
        )
        # Процессы сверены, но приложение не восстанавливает volatile-контекст
        # после перезапуска самого харнесса.
        fenced_sessions.update(
            status["session_id"]
            for status in restored.values()
            if "session_id" in status
        )
        ready = True

    async def run_attempt(
        adapter: Adapter,
        launch: dict,
        model: str,
        home: Path,
        env: dict,
        chat: MountedChat | None,
    ) -> dict:
        run_id = launch["run_id"]
        try:
            try:
                result = await adapter.run(
                    launch["prompt"], model, home, env, launch["timeout_s"]
                )
            except Exception as error:
                await adapter.cancel()
                adapter.record_diagnostic(
                    {"code": "harness_runtime_error", "kind": type(error).__name__}
                )
                result = {
                    **adapter.snapshot(),
                    "execution_status": "transport_error",
                    "evidence_complete": False,
                }
            if messages := result.pop("messages", []):
                if chat is not None:
                    recent_messages[run_id] = (launch["session_id"], messages)
                    if len(recent_messages) > 16:
                        recent_messages.popitem(last=False)
            result = await recovery.complete(run_id, result)
        except (RecoveryError, OSError, ValueError):
            result = {
                **adapter.snapshot(),
                "done": True,
                "released": False,
                "evidence_complete": False,
                "cleanup_error": "harness_cleanup_unconfirmed",
            }
            result.pop("messages", None)
        if launch["version"] == "v1":
            result.update(version="v1", run_id=run_id, session_id=launch["session_id"])
        async with lock:
            if chat is not None:
                if result["released"] and result.get("continuation_safe") is True:
                    chat.thread_id = result["thread_id"]
                else:
                    chat.blocked = True
            if not result["released"]:
                failed[run_id] = result
            controls[run_id] = result["released"]
            active.pop(run_id, None)
        return result

    async def health(request: web.Request) -> web.Response:
        usable = ready and not blocked()
        busy = bool(active) or not usable
        return web.json_response(
            {
                "status": "ready" if usable else "blocked",
                "ready": usable and not busy,
                "busy": busy,
            },
            status=200 if usable else 503,
        )

    async def launch(request: web.Request) -> web.Response:
        data = await request.json()
        mounted = None
        if request.path.startswith("/v1/"):
            mounted = mounted_launch(data)
        else:
            validate("launch", data["request"])
        launch = data["request"]
        run_id = launch["run_id"]
        async with lock:
            saved = _receipt(run_id)
            if saved is not None:
                identity = (
                    (saved["session_id"], saved["request_fingerprint"])
                    if saved.get("version") == "v1"
                    else None
                )
                if identity != mounted:
                    raise web.HTTPConflict(text="run_conflict")
                return web.json_response({"run_id": run_id, "accepted": True})
            if active or blocked() or not ready:
                raise web.HTTPConflict(text="busy")
            chat = None
            session_id = mounted[0] if mounted is not None else None
            if session_id is not None:
                if session_id in fenced_sessions or recovery.session_closed(session_id):
                    return web.json_response(
                        {"error": "harness_context_unavailable"}, status=409
                    )
                chat = chats.get(session_id)
                binding = (data["project_id"], data["handle"], data["model"])
                if chat is not None and (chat.blocked or chat.binding != binding):
                    return web.json_response(
                        {"error": "harness_context_unavailable"}, status=409
                    )
                home = recovery.session_home(session_id)
            else:
                home = recovery.attempt_home(run_id)
            registered = False
            configured = False
            try:
                if mounted is not None:
                    owner = recovery.begin_attempt(run_id, home, mounted=mounted)
                    registered = True
                env = configure(
                    home,
                    manager_url=manager_url,
                    manager_token=manager_token,
                    project_id=data["project_id"],
                    handle=data["handle"],
                    run_id=run_id,
                    proxy_url=data["proxy_url"],
                    inference_token=data["inference_token"],
                    model=data["model"],
                    session_id=session_id,
                    reuse_home=chat is not None,
                )
                configured = True
                if mounted is None:
                    owner = recovery.begin_attempt(run_id, home)
                bind_recovery_owner(home, owner)
            except (RecoveryError, OSError, ValueError) as error:
                if session_id is None and not configured:
                    raise
                controls[run_id] = False
                if session_id is None:
                    if home.is_dir():
                        shutil.rmtree(home)
                else:
                    fenced_sessions.add(session_id)
                    if registered:
                        result = await recovery.complete(
                            run_id, {"execution_status": "transport_error"}
                        )
                        controls[run_id] = result["released"]
                raise web.HTTPServiceUnavailable(
                    text="harness_registration_failed"
                ) from error
            env.update(owner)
            if session_id is not None and chat is None:
                chat = chats[session_id] = MountedChat(binding)
            adapter = Adapter(
                os.environ.get("CODEX_BINARY", "/usr/local/bin/codex"),
                (token, manager_token, data["inference_token"], *owner.values()),
                mounted=mounted is not None,
                **({"resume_thread_id": chat.thread_id} if chat is not None else {}),
            )
            controls[run_id] = False
            task = asyncio.create_task(
                run_attempt(adapter, launch, data["model"], home, env, chat)
            )
            active[run_id] = (adapter, task, mounted[0] if mounted else None)
        return web.json_response({"run_id": run_id, "accepted": True}, status=202)

    def _receipt(run_id: str) -> dict | None:
        try:
            return recovery.status(run_id)
        except RecoveryError as error:
            raise web.HTTPServiceUnavailable(
                text="harness_status_unconfirmed"
            ) from error

    def session_scope(request: web.Request) -> str | None:
        session_id = request.match_info.get("session_id")
        if session_id is not None:
            try:
                validate_mounted("id", session_id)
                if "run_id" in request.match_info:
                    validate_mounted("id", request.match_info["run_id"])
            except ContractError as error:
                raise web.HTTPNotFound(text="unknown_run") from error
        return session_id

    async def status(request: web.Request) -> web.Response:
        scoped_session = session_scope(request)
        run_id = request.match_info["run_id"]
        if run_id in failed:
            snapshot = {**failed[run_id]}
        elif pair := active.get(run_id):
            adapter, _, session_id = pair
            snapshot = {**adapter.snapshot(), "done": False, "released": False}
            if session_id is not None:
                snapshot.update(
                    version="v1",
                    run_id=run_id,
                    session_id=session_id,
                    cleanup_error=None,
                )
        else:
            saved = _receipt(run_id)
            if saved is None:
                raise web.HTTPNotFound(text="unknown_run")
            snapshot = {**saved}
        if snapshot.get("session_id") != scoped_session:
            raise web.HTTPNotFound(text="unknown_run")
        if scoped_session is not None:
            snapshot.pop("request_fingerprint", None)
            _, messages = recent_messages.get(run_id, (scoped_session, []))
            snapshot.setdefault("messages", messages)
        return web.json_response(snapshot)

    async def cancel(request: web.Request) -> web.Response:
        scoped_session = session_scope(request)
        run_id = request.match_info["run_id"]
        pair = active.get(run_id)
        if pair is not None and pair[2] == scoped_session:
            await pair[0].cancel()
        return await status(request)

    async def close_session(request: web.Request) -> web.Response:
        session_id = session_scope(request)
        assert session_id is not None
        async with lock:
            if any(session_id == scope for _, _, scope in active.values()):
                raise web.HTTPConflict(text="run_active")
            fenced_sessions.add(session_id)
            chats.pop(session_id, None)
            for run_id, (scope, _) in tuple(recent_messages.items()):
                if scope == session_id:
                    recent_messages.pop(run_id)
            try:
                recovery.close_session(session_id)
            except (RecoveryError, OSError):
                return web.json_response({"error": "cleanup_failed"}, status=503)
            fenced_sessions.discard(session_id)
        return web.json_response({"cleanup_status": "success"})

    async def shutdown(application: web.Application) -> None:
        nonlocal ready
        ready = False
        attempts = list(active.values())
        async with asyncio.timeout(90):
            for adapter, _, _ in attempts:
                await adapter.cancel()
            await asyncio.gather(
                *(task for _, task, _ in attempts), return_exceptions=True
            )

    app.add_routes(
        [
            web.get("/health", health),
            web.post("/v0/runs", launch),
            web.get("/v0/runs/{run_id}", status),
            web.post("/v0/runs/{run_id}/cancel", cancel),
            web.post("/v1/runs", launch),
            web.get("/v1/sessions/{session_id}/runs/{run_id}", status),
            web.post("/v1/sessions/{session_id}/runs/{run_id}/cancel", cancel),
            web.delete("/v1/sessions/{session_id}", close_session),
        ]
    )
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(
            os.environ["HARNESS_TOKEN"],
            Path(os.environ.get("HARNESS_STATE", "/state")),
            os.environ["MANAGER_TUNNEL_URL"],
            os.environ["MANAGER_TOKEN"],
        ),
        port=8083,
        access_log=None,
    )
