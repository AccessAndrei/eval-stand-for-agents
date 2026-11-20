"""Headless-запуск одного сообщения через общий клиент v1."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .session import ClientError, MountedClient


def _arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, help="URL Gateway")
    parser.add_argument("--directory", required=True, type=Path, help="Папка для /work")
    parser.add_argument("--mode", choices=("ro", "rw"), default="rw")
    parser.add_argument("--profile", required=True, help="Профиль модели")
    parser.add_argument("--prompt", required=True, help="Сообщение для агента")
    parser.add_argument(
        "--timeout-s", type=int, default=120, help="Лимит turn в секундах"
    )
    parser.add_argument("--task-id", help="Идентификатор задачи БАФ")
    parser.add_argument("--ssh-host", help="Адрес брокера при отдельных точках входа")
    return parser


def _print_event(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def _main(arguments: argparse.Namespace, token: str) -> int:
    async with MountedClient(
        arguments.gateway, token, ssh_host=arguments.ssh_host
    ) as client:
        session = client.session(
            arguments.directory, profile=arguments.profile, mode=arguments.mode
        )
        result = None
        try:
            mounted = await session.mount()
            _print_event({"type": "session", "session": mounted})
            run = await session.start_run(
                arguments.prompt,
                timeout_s=arguments.timeout_s,
                task_id=arguments.task_id,
            )
            async for event in session.events(
                run["run_id"], timeout=arguments.timeout_s + client.timeout
            ):
                _print_event(event)
                if event["type"] == "result":
                    result = event["result"]
        finally:
            report = await session.close()
            _print_event(
                {
                    "type": "session_closed",
                    "session_id": session.session_id,
                    "server": report.server,
                    "server_error": report.server_error.code
                    if report.server_error
                    else None,
                    "tunnel_closed": report.tunnel_closed,
                    "export_drained": report.export_drained,
                    "ok": report.ok,
                }
            )
        return int(
            result is None
            or result.get("execution_status") != "success"
            or not report.ok
        )


def main() -> int:
    parser = _arguments()
    arguments = parser.parse_args()
    token = os.environ.get("CLIENT_TOKEN")
    if not token:
        parser.error("Укажите токен в CLIENT_TOKEN")
    try:
        return asyncio.run(_main(arguments, token))
    except (ClientError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
