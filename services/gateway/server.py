"""Авторизованный вход в последовательный runtime первого чекпоинта."""

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web

from contracts import validate
from services.common.http import authenticated
from services.common.storage import StorageBudgetExceeded
from services.gateway.clients import Client, RemoteError
from services.gateway.runtime import IO_ERRORS, Runtime
from services.gateway.store import Busy, Conflict, RunStore


def create_app(runtime: Runtime, token: str) -> web.Application:
    app = web.Application(middlewares=[authenticated(token)], client_max_size=2_500_000)
    tasks: dict[str, asyncio.Task] = {}
    lock = asyncio.Lock()
    ready = False

    async def startup(application: web.Application) -> None:
        nonlocal ready
        for record in runtime.store.records():
            run_id = record["run_id"]
            saved = runtime.store.path / run_id / "result.json"
            if saved.exists():
                runtime.store.finish(json.loads(saved.read_text()))
            elif record["state"] != "terminal":
                launch = json.loads(
                    (runtime.store.path / run_id / "request.json").read_text()
                )
                await runtime.execute(launch, recover=True)
        ready = True

    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {"ready": ready, "busy": runtime.store.busy()}, status=200 if ready else 503
        )

    async def create(request: web.Request) -> web.Response:
        launch = await request.json()
        validate("launch", launch)
        async with lock:
            try:
                try:
                    existing = runtime.store.get(launch["run_id"])
                except FileNotFoundError:
                    existing = None
                if existing is None:
                    if not ready:
                        raise web.HTTPServiceUnavailable()
                    if runtime.store.busy():
                        raise Busy("busy")
                    await runtime.prepare_profile(launch["provider_profile"])
                record, created = runtime.store.accept(launch)
            except Conflict as error:
                raise web.HTTPConflict(text="conflict") from error
            except Busy as error:
                raise web.HTTPConflict(text="busy") from error
            except StorageBudgetExceeded:
                return web.json_response(
                    {"error": "storage_budget_exceeded"}, status=507
                )
            except RemoteError as error:
                return web.json_response(
                    {
                        "error": "auth_error"
                        if error.status in {401, 403} or "auth" in error.code
                        else "profile_incompatible",
                        "reason": error.code,
                    },
                    status=422,
                )
            if created:
                tasks[launch["run_id"]] = asyncio.create_task(runtime.execute(launch))
        return web.json_response(record, status=202 if created else 200)

    async def status(request: web.Request) -> web.Response:
        try:
            return web.json_response(runtime.store.get(request.match_info["run_id"]))
        except FileNotFoundError as error:
            raise web.HTTPNotFound() from error

    async def cancel(request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        try:
            record = runtime.store.get(run_id)
        except FileNotFoundError as error:
            raise web.HTTPNotFound() from error
        if record["state"] != "terminal":
            runtime.cancellations.add(run_id)
        return web.json_response(record)

    async def artifacts(request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        try:
            record = runtime.store.get(run_id)
            if record["state"] != "terminal":
                raise web.HTTPConflict(text="not_terminal")
            path = runtime.store.path / run_id / "artifacts.json"
            return web.json_response(json.loads(path.read_text()))
        except FileNotFoundError as error:
            raise web.HTTPNotFound() from error

    async def events(request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        try:
            runtime.store.get(run_id)
            launch = json.loads(
                (runtime.store.path / run_id / "request.json").read_text()
            )
        except FileNotFoundError as error:
            raise web.HTTPNotFound() from error
        stream = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await stream.prepare(request)
        cursor = 0
        try:
            async with asyncio.timeout(launch["timeout_s"] + 180):
                while True:
                    state = runtime.store.get(run_id)
                    saved = runtime.store.path / run_id / "events.json"
                    entries = json.loads(saved.read_text()) if saved.exists() else []
                    if not saved.exists() and state["state"] in {
                        "running",
                        "cancelling",
                    }:
                        native = await runtime.harness.request(
                            "GET", f"/v0/runs/{run_id}"
                        )
                        entries = runtime.events(launch, native)
                    for entry in entries[cursor:]:
                        await stream.write(
                            json.dumps(entry, ensure_ascii=False).encode() + b"\n"
                        )
                        cursor += 1
                    if state["state"] == "terminal":
                        break
                    await asyncio.sleep(0.2)
        except (*IO_ERRORS, ConnectionResetError):
            pass
        try:
            await stream.write_eof()
        except ConnectionResetError:
            pass
        return stream

    async def shutdown(application: web.Application) -> None:
        nonlocal ready
        ready = False
        runtime.cancellations.update(tasks)
        if tasks:
            async with asyncio.timeout(90):
                await asyncio.gather(*tasks.values(), return_exceptions=True)

    app.add_routes(
        [
            web.get("/health", health),
            web.post("/v0/runs", create),
            web.get("/v0/runs/{run_id}", status),
            web.post("/v0/runs/{run_id}/cancel", cancel),
            web.get("/v0/runs/{run_id}/artifacts", artifacts),
            web.get("/v0/runs/{run_id}/events", events),
        ]
    )
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    return app


def main() -> None:
    project = os.environ["PROJECT_ID"]
    store = RunStore(
        Path(os.environ.get("GATEWAY_STATE", "/state")),
        int(os.environ.get("STATE_LIMIT_BYTES", "1000000000")),
    )
    provenance = json.loads(Path(os.environ["PROVENANCE_FILE"]).read_text())
    runtime = Runtime(
        store,
        Client(os.environ["MANAGER_URL"], os.environ["MANAGER_TOKEN"], project),
        Client(os.environ["HARNESS_URL"], os.environ["HARNESS_TOKEN"], project),
        Client(os.environ["PROXY_URL"], os.environ["PROXY_TOKEN"], project),
        project,
        provenance,
    )
    web.run_app(
        create_app(runtime, os.environ["CLIENT_TOKEN"]), port=8080, access_log=None
    )


if __name__ == "__main__":
    main()
