"""Очистка при остановленном основном менеджере; только его ресурсы."""

import asyncio
import json

from .server import manager_from_environment


async def cleanup() -> bool:
    manager = manager_from_environment()
    async with asyncio.timeout(90):
        if (
            manager.workspace_driver is not None
            and manager.workspace_driver.state.is_file()
        ):
            await manager.workspace_driver.start_existing()
        await manager.recover()
        await manager.shutdown()
    return not manager.persistence_failed and all(
        json.loads(path.read_text())["cleanup_status"] == "success"
        for path in manager.root.glob("*.json")
    )


def main() -> int:
    try:
        success = asyncio.run(cleanup())
    except (OSError, ValueError, RuntimeError, TimeoutError):
        success = False
    print("Очистка подтверждена" if success else "Очистка не подтверждена")
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
