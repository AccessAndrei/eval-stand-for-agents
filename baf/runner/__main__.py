"""CLI последовательного прогона явно выбранного датасета БАФ."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from baf.runner.batch import run_batch
from baf.runner.catalog import CatalogError, load_dataset
from baf.runner.transport import GatewayClient

LOCAL_ENV = Path(__file__).resolve().parents[2] / ".env"
HOST_SETTINGS = (
    "CLIENT_TOKEN",
    "GATEWAY_URL",
    "GATEWAY_PORT",
    "RESULTS_PATH",
    "RUNNER_STORAGE_BYTES",
)


class HostSettingsError(ValueError):
    """Ошибка настроек, текст которой безопасен для CLI."""


def _host_settings() -> dict[str, str]:
    values: dict[str, str] = {}
    if LOCAL_ENV.is_symlink():
        raise ValueError("Символическая ссылка вместо .env запрещена")
    if LOCAL_ENV.exists():
        if not LOCAL_ENV.is_file():
            raise ValueError("Ожидался обычный файл .env")
        from scripts.configure_local import assignments

        values = assignments(LOCAL_ENV.read_text(encoding="utf-8").splitlines())
    return {
        key: os.environ[key] if key in os.environ else values[key]
        for key in HOST_SETTINGS
        if key in os.environ or key in values
    }


def _host_value(settings: dict[str, str], key: str) -> str:
    value = settings.get(key, "")
    if key not in os.environ and "$" in value:
        raise HostSettingsError(f"Задайте {key} в окружении готовым значением")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Последовательный прогон датасета БАФ")
    parser.add_argument(
        "--dataset", required=True, type=Path, help="Путь к YAML-манифесту датасета"
    )
    parser.add_argument(
        "--profile", required=True, help="Имя настроенного профиля провайдера"
    )
    parser.add_argument("--results", type=Path)
    parser.add_argument("--protocol", choices=("v1", "v0"), default="v1")
    parser.add_argument(
        "--smoke", action="store_true", help="Проверить создание файлов"
    )
    args = parser.parse_args()
    if args.smoke and args.protocol == "v0":
        parser.error("--smoke требует протокол v1")
    try:
        mounted = args.protocol == "v1"
        tasks = load_dataset(args.dataset, mounted=mounted)
        if mounted:
            from baf.runner.mounted_batch import run_mounted_batch
            from client.session import MountedClient

            settings = _host_settings()
            if "GATEWAY_URL" in settings:
                gateway = _host_value(settings, "GATEWAY_URL")
            else:
                port = _host_value(settings, "GATEWAY_PORT") or "8080"
                gateway = f"http://127.0.0.1:{port}"
            options = {"smoke": True} if args.smoke else {}
            path, report = asyncio.run(
                run_mounted_batch(
                    MountedClient(gateway, _host_value(settings, "CLIENT_TOKEN")),
                    tasks=tasks,
                    profile=args.profile,
                    results=args.results
                    or Path(_host_value(settings, "RESULTS_PATH") or "results"),
                    storage_limit=int(
                        _host_value(settings, "RUNNER_STORAGE_BYTES") or "1000000000"
                    ),
                    **options,
                )
            )
        else:
            path, report = run_batch(
                GatewayClient(
                    os.getenv("GATEWAY_URL", "http://127.0.0.1:8080"),
                    os.getenv("CLIENT_TOKEN", ""),
                ),
                tasks=tasks,
                profile=args.profile,
                results=args.results or Path("results"),
                storage_limit=int(os.getenv("RUNNER_STORAGE_BYTES", "1000000000")),
            )
    except CatalogError as error:
        print(f"Ошибка датасета: {error}", file=sys.stderr)
        return 2
    except HostSettingsError as error:
        print(f"Ошибка настроек: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError) as error:
        print(
            f"Не удалось начать или сохранить набор: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    print(f"Отчёт: {path.resolve()}")
    if args.smoke:
        passed = sum(task["smoke_status"] == "passed" for task in report["tasks"])
        print(f"Проверку файлов smoke прошли {passed}/{len(report['tasks'])} задач.")
    else:
        print(
            f"Оценено: {report['evaluated']}/{len(report['tasks'])}. "
            f"Неверных ответов: {report['wrong_answers']}. "
            f"Технических пропусков: {report['technical_skips']}"
        )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
