"""Удалять тома можно только после подтверждённой очистки динамических сред."""

import argparse
import os
import subprocess


def shutdown(files: list[str], remove_volumes: bool) -> int:
    command = ["docker", "compose"]
    for path in files:
        command.extend(["-f", path])
    env = {
        **os.environ,
        "DOCKER_DEFAULT_PLATFORM": os.getenv("DOCKER_PLATFORM", "linux/arm64"),
    }
    running = subprocess.run(
        command + ["ps", "--status", "running", "--services"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
        env=env,
    ).stdout.splitlines()
    if "sandbox-manager" in running:
        source = (
            "import os,urllib.request;"
            "r=urllib.request.Request('http://127.0.0.1:8081/v0/shutdown',method='POST',"
            "headers={'Authorization':'Bearer '+os.environ['MANAGER_TOKEN']});"
            "urllib.request.urlopen(r,timeout=95).close()"
        )
        cleanup = command + ["exec", "-T", "sandbox-manager", "python", "-c", source]
    else:
        cleanup = command + [
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            "sandbox-manager",
            "python",
            "-m",
            "services.sandbox_manager.cleanup",
        ]
    checked = subprocess.run(cleanup, capture_output=True, timeout=110, env=env)
    if checked.returncode:
        print("Очистка не подтверждена; тома и журнал сохранены")
        return 2
    if remove_volumes:
        subprocess.run(
            command + ["down", "--volumes"], check=True, timeout=150, env=env
        )
    print("Очистка подтверждена")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", action="append")
    parser.add_argument(
        "--down", action="store_true", help="После очистки выполнить compose down -v"
    )
    args = parser.parse_args()
    try:
        return shutdown(
            args.compose_file or ["docker-compose.yml", "docker/compose.mock.yml"],
            args.down,
        )
    except (OSError, subprocess.SubprocessError):
        print("Остановка не подтверждена; автоматическое удаление истории не выполнено")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
