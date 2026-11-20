"""Аутентифицированный HTTP API управления без произвольного Docker API."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from aiohttp import web

from services.common.http import authenticated

from .docker import DockerError
from .manager import Manager, ManagerError
from .provenance import publish_provenance
from .tunnel import serve_tunnel
from .workspace_driver import WorkspaceDriver

MANAGER = web.AppKey("manager", Manager)


@web.middleware
async def manager_errors(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    version = "v1" if request.path.startswith("/v1/") else "v0"
    try:
        return await handler(request)
    except ManagerError as error:
        return web.json_response(
            {"version": version, "error": error.code}, status=error.http_status
        )
    except DockerError:
        return web.json_response(
            {"version": version, "error": "docker_unavailable"}, status=502
        )
    except OSError:
        return web.json_response(
            {"version": version, "error": "storage_budget_exceeded"}, status=507
        )


async def health(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    if manager.persistence_failed:
        raise ManagerError("storage_budget_exceeded", 503)
    if manager.unclean_workspaces:
        raise ManagerError("cleanup_failed", 503)
    await manager.docker.command(
        ["version", "--format", "{{.Server.Version}}"], limit=1024
    )
    if await manager.docker.inspect("image", manager.image) is None:
        raise ManagerError("sandbox_image_missing", 503)
    if (
        manager.workspace_driver is not None
        and not await manager.workspace_driver.ready()
    ):
        raise ManagerError("workspace_driver_unavailable", 503)
    return web.json_response({"status": "ready"})


async def provision(request: web.Request) -> web.Response:
    return web.json_response(await request.app[MANAGER].provision(await request.json()))


async def provision_mounted(request: web.Request) -> web.Response:
    return web.json_response(
        await request.app[MANAGER].provision_mounted(await request.json())
    )


async def lookup_mounted(request: web.Request) -> web.Response:
    return web.json_response(
        await request.app[MANAGER].lookup_mounted(
            request.match_info["session_id"], request.headers.get("X-Project-ID", "")
        )
    )


async def fence_mounted_release(request: web.Request) -> web.Response:
    return web.json_response(
        await request.app[MANAGER].fence_mounted_release(
            await request.json(), request.match_info["session_id"]
        )
    )


async def release_mounted(request: web.Request) -> web.Response:
    return web.json_response(
        await request.app[MANAGER].release_mounted(
            await request.json(), request.match_info["handle"]
        )
    )


async def mounted_operation(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    method = {
        "runs": manager.reserve_mounted_run,
        "collect": manager.collect_mounted_run,
    }[request.match_info["operation"]]
    return web.json_response(
        await method(await request.json(), request.match_info["handle"])
    )


async def lookup(request: web.Request) -> web.Response:
    return web.json_response(
        await request.app[MANAGER].lookup(
            request.match_info["run_id"],
            request.headers.get("X-Project-ID", ""),
        )
    )


async def operation(request: web.Request) -> web.Response:
    data = await request.json()
    if data.get("handle") != request.match_info["handle"]:
        raise ManagerError("handle_mismatch", 400)
    manager = request.app[MANAGER]
    method = {
        "collect": manager.collect,
        "release": manager.release,
        "cancel": manager.cancel,
    }[request.match_info["operation"]]
    return web.json_response(await method(data))


async def cleanup(request: web.Request) -> web.Response:
    await request.app[MANAGER].shutdown()
    manager = request.app[MANAGER]
    if any(
        manager._load(path.stem)["cleanup_status"] != "success"
        for path in manager.root.glob("*.json")
    ):
        raise ManagerError("cleanup_failed", 503)
    return web.json_response({"status": "stopped"})


def create_app(
    manager: Manager,
    token: str,
    *,
    tunnel_port: int | None = 8082,
    provenance: tuple[str, Path] | None = None,
) -> web.Application:
    app = web.Application(
        middlewares=[authenticated(token), manager_errors], client_max_size=2_500_000
    )
    app[MANAGER] = manager
    app.router.add_get("/health", health)
    app.router.add_post("/v0/environments", provision)
    app.router.add_get("/v0/environments/by-run/{run_id}", lookup)
    app.router.add_post(
        "/v0/environments/{handle}/{operation:collect|release|cancel}", operation
    )
    app.router.add_post("/v0/shutdown", cleanup)
    app.router.add_post("/v1/environments", provision_mounted)
    app.router.add_get("/v1/environments/by-session/{session_id}", lookup_mounted)
    app.router.add_delete(
        "/v1/environments/by-session/{session_id}", fence_mounted_release
    )
    app.router.add_post("/v1/environments/{handle}/release", release_mounted)
    app.router.add_post(
        "/v1/environments/{handle}/{operation:runs|collect}", mounted_operation
    )

    async def lifecycle(application: web.Application) -> AsyncIterator[None]:
        server = None
        try:
            if provenance is not None:
                await publish_provenance(manager.docker, *provenance)
            if manager.workspace_driver is not None:
                await manager.workspace_driver.start()
            await manager.recover()
            if tunnel_port is not None:
                server = await serve_tunnel(manager, token, "0.0.0.0", tunnel_port)
            yield
        finally:
            if server:
                server.close()
                await server.wait_closed()
            async with asyncio.timeout(90):
                await manager.shutdown()

    app.cleanup_ctx.append(lifecycle)
    return app


def manager_from_environment() -> Manager:
    manager = Manager(
        Path(os.environ.get("MANAGER_STATE", "/state/manager")),
        os.environ.get("PROJECT_ID", "eval-stand"),
        os.environ.get("SANDBOX_IMAGE", "eval-stand-sandbox:0.159.3"),
    )
    if ipc_volume := os.environ.get("WORKSPACE_IPC_VOLUME"):
        manager.workspace_driver = WorkspaceDriver(
            manager.docker,
            manager.root / "workspace-driver" / "identity.json",
            manager.project,
            ipc_volume,
        )
    return manager


def main() -> None:
    provenance = None
    if path := os.environ.get("RUNTIME_PROVENANCE_FILE"):
        provenance = (os.environ["CODEX_SOURCE_IMAGE"], Path(path))
    web.run_app(
        create_app(
            manager_from_environment(),
            os.environ["MANAGER_TOKEN"],
            provenance=provenance,
        ),
        port=8081,
        access_log=None,
    )


if __name__ == "__main__":
    main()
