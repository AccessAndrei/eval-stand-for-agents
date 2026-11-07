"""Очистка при остановленном основном менеджере; только его ресурсы."""

import asyncio
import json
import os
from pathlib import Path

from .manager import Manager


async def cleanup() -> bool:
    root = Path(os.environ.get("MANAGER_STATE", "/state/manager"))
    manager = Manager(root, os.environ["PROJECT_ID"], os.environ["SANDBOX_IMAGE"])
    async with asyncio.timeout(90):
        await manager.recover()
        await manager.shutdown()
    return not manager.persistence_failed and all(
        json.loads(path.read_text())["cleanup_status"] == "success"
        for path in root.glob("*.json")
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
