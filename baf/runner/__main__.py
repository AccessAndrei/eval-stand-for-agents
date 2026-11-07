"""Явный CLI первого чекпоинта; live-профиль не заменяется автоматически."""

import argparse
import os
from pathlib import Path

from baf.runner.batch import run_batch
from baf.runner.transport import GatewayClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Пять последовательных задач первого чекпоинта"
    )
    parser.add_argument(
        "--profile", choices=("luna", "mock", "mock-alt", "deepseek"), required=True
    )
    parser.add_argument("--results", type=Path, default=Path("results"))
    args = parser.parse_args()
    try:
        client = GatewayClient(
            os.getenv("GATEWAY_URL", "http://127.0.0.1:8080"),
            os.getenv("CLIENT_TOKEN", ""),
        )
        path, report = run_batch(
            client,
            profile=args.profile,
            results=args.results,
            storage_limit=int(os.getenv("RUNNER_STORAGE_BYTES", "1000000000")),
        )
    except (ValueError, OSError, RuntimeError) as error:
        print("Не удалось начать или сохранить набор: " + type(error).__name__)
        return 2
    print(f"Отчёт: {path.resolve()}")
    print(
        f"Оценено: {report['evaluated']}/5; неверных ответов: {report['wrong_answers']}; технических пропусков: {report['technical_skips']}"
    )
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
