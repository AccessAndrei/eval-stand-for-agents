"""Владение одним харнессом до устойчивого подтверждения освобождения."""

import asyncio
import os
import shutil
from pathlib import Path

from aiohttp import web

from contracts import validate
from services.common.http import authenticated
from services.harness.adapter import Adapter
from services.harness.recovery import HarnessRecovery, RecoveryError
from services.harness.settings import bind_recovery_owner, configure


def create_app(
    token: str,
    state: Path,
    manager_url: str,
    manager_token: str,
) -> web.Application:
    state = state.absolute()
    app = web.Application(middlewares=[authenticated(token)], client_max_size=2_500_000)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    recovery = HarnessRecovery(state)
    active: dict[str, tuple[Adapter, asyncio.Task]] = {}
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
        ready = True

    async def run_attempt(
        adapter: Adapter, launch: dict, model: str, home: Path, env: dict
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
            result = await recovery.complete(run_id, result)
        except (RecoveryError, OSError, ValueError):
            result = {
                **adapter.snapshot(),
                "done": True,
                "released": False,
                "evidence_complete": False,
                "cleanup_error": "harness_cleanup_unconfirmed",
            }
            failed[run_id] = result
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
        validate("launch", data["request"])
        launch = data["request"]
        run_id = launch["run_id"]
        async with lock:
            if run_id in active or recovery.status(run_id) is not None:
                return web.json_response({"run_id": run_id, "accepted": True})
            if active or blocked() or not ready:
                raise web.HTTPConflict(text="busy")
            home = state / run_id
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
            )
            try:
                owner = recovery.begin_attempt(run_id, home)
                controls[run_id] = False
                bind_recovery_owner(home, owner)
            except (RecoveryError, OSError, ValueError) as error:
                controls[run_id] = False
                shutil.rmtree(home)
                raise web.HTTPServiceUnavailable(
                    text="harness_registration_failed"
                ) from error
            env.update(owner)
            adapter = Adapter(
                os.environ.get("CODEX_BINARY", "/usr/local/bin/codex"),
                (token, manager_token, data["inference_token"], *owner.values()),
            )
            controls[run_id] = False
            task = asyncio.create_task(
                run_attempt(adapter, launch, data["model"], home, env)
            )
            active[run_id] = (adapter, task)
        return web.json_response({"run_id": run_id, "accepted": True}, status=202)

    async def status(request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        if run_id in failed:
            return web.json_response(failed[run_id])
        pair = active.get(run_id)
        if pair is not None:
            return web.json_response(
                {**pair[0].snapshot(), "done": False, "released": False}
            )
        try:
            saved = recovery.status(run_id)
        except RecoveryError as error:
            raise web.HTTPServiceUnavailable(
                text="harness_status_unconfirmed"
            ) from error
        if saved is None:
            raise web.HTTPNotFound(text="unknown_run")
        return web.json_response(saved)

    async def cancel(request: web.Request) -> web.Response:
        pair = active.get(request.match_info["run_id"])
        if pair is not None:
            await pair[0].cancel()
        return await status(request)

    async def shutdown(application: web.Application) -> None:
        nonlocal ready
        ready = False
        attempts = list(active.values())
        async with asyncio.timeout(90):
            for adapter, _ in attempts:
                await adapter.cancel()
            await asyncio.gather(
                *(task for _, task in attempts), return_exceptions=True
            )

    app.add_routes(
        [
            web.get("/health", health),
            web.post("/v0/runs", launch),
            web.get("/v0/runs/{run_id}", status),
            web.post("/v0/runs/{run_id}/cancel", cancel),
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
