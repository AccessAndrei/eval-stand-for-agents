"""Аутентифицированный HTTP API управления без произвольного Docker API."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from aiohttp import web

from services.common.http import authenticated

from .docker import DockerError
from .manager import Manager, ManagerError
from .tunnel import serve_tunnel

MANAGER = web.AppKey("manager", Manager)


@web.middleware
async def manager_errors(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        return await handler(request)
    except ManagerError as error:
        return web.json_response(
            {"version": "v0", "error": error.code}, status=error.http_status
        )
    except DockerError:
        return web.json_response(
            {"version": "v0", "error": "docker_unavailable"}, status=502
        )
    except OSError:
        return web.json_response(
            {"version": "v0", "error": "storage_budget_exceeded"}, status=507
        )


async def health(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    if manager.persistence_failed:
        raise ManagerError("storage_budget_exceeded", 503)
    await manager.docker.command(
        ["version", "--format", "{{.Server.Version}}"], limit=1024
    )
    if await manager.docker.inspect("image", manager.image) is None:
        raise ManagerError("sandbox_image_missing", 503)
    return web.json_response({"status": "ready"})


async def provision(request: web.Request) -> web.Response:
    return web.json_response(await request.app[MANAGER].provision(await request.json()))


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
    manager: Manager, token: str, *, tunnel_port: int | None = 8082
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

    async def lifecycle(application: web.Application) -> AsyncIterator[None]:
        await manager.recover()
        server = (
            await serve_tunnel(manager, token, "0.0.0.0", tunnel_port)
            if tunnel_port is not None
            else None
        )
        try:
            yield
        finally:
            if server:
                server.close()
                await server.wait_closed()
            async with asyncio.timeout(90):
                await manager.shutdown()

    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    manager = Manager(
        Path(os.environ.get("MANAGER_STATE", "/state/manager")),
        os.environ.get("PROJECT_ID", "checkpoint1"),
        os.environ.get("SANDBOX_IMAGE", "eval-stand-sandbox:0.159.3"),
    )
    web.run_app(
        create_app(manager, os.environ["MANAGER_TOKEN"]), port=8081, access_log=None
    )


if __name__ == "__main__":
    main()
