"""Реальные down-проверки с отдельным контрольным контейнером чужого проекта."""

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path


def docker(*args: str) -> bytes:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        check=True,
        timeout=30,
        env={
            **os.environ,
            "DOCKER_DEFAULT_PLATFORM": os.getenv("DOCKER_PLATFORM", "linux/arm64"),
        },
    ).stdout


def exports() -> dict[str, str]:
    files = []
    for path in Path("results").glob("native-*"):
        files.extend(path.rglob("*") if path.is_dir() else [path])
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
        if path.is_file()
    }


def configured_projects(path: Path = Path(".env")) -> tuple[str, str]:
    names = {
        key: os.environ[key]
        for key in ("COMPOSE_PROJECT_NAME", "PROJECT_ID")
        if key in os.environ
    }
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"COMPOSE_PROJECT_NAME", "PROJECT_ID"}:
            names.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return names.get("COMPOSE_PROJECT_NAME", "eval-stand"), names.get(
        "PROJECT_ID", "eval-stand"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("make", "compose"))
    args = parser.parse_args()
    identifier = uuid.uuid4().hex
    name = "eval-foreign-sentinel-" + identifier[:12]
    compose_project, manager_project = configured_projects()
    before = exports()
    env_digest = hashlib.sha256(Path(".env").read_bytes()).hexdigest()
    auth_path = Path(".auth/current.json")
    auth_before = None
    if auth_path.exists():
        metadata = auth_path.stat()
        auth_before = (
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_mode,
        )
    foreign = (
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--pull",
            "never",
            "--platform",
            os.getenv("DOCKER_PLATFORM", "linux/arm64"),
            "--label",
            "io.eval-stand.shutdown-sentinel=" + identifier,
            "--label",
            "io.eval-stand.project=foreign",
            "--label",
            "com.docker.compose.project=foreign-shutdown-test",
            "--label",
            "com.docker.compose.service=sentinel",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "1001:1001",
            "--cpus",
            "0.01",
            "--memory",
            "16000000",
            "--memory-swap",
            "16000000",
            "--pids-limit",
            "8",
            "--entrypoint",
            "/usr/local/bin/python3",
            "eval-stand-sandbox:0.159.3",
            "-I",
            "-S",
            "-c",
            "import time;time.sleep(300)",
        )
        .decode()
        .strip()
    )
    report = {
        "mode": args.mode,
        "foreign_container": foreign,
        "passed": False,
        "compose_project": compose_project,
        "manager_project": manager_project,
    }
    try:
        command = (
            ["make", "down"]
            if args.mode == "make"
            else [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker/compose.mock.yml",
                "-f",
                "docker/compose.single-cpu.yml",
                "down",
                "-v",
            ]
        )
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=180,
            env={
                **os.environ,
                "DOCKER_DEFAULT_PLATFORM": os.getenv("DOCKER_PLATFORM", "linux/arm64"),
            },
        )
        info = json.loads(docker("inspect", foreign))[0]
        containers = (
            docker(
                "ps",
                "-a",
                "--filter",
                "label=io.eval-stand.owner=eval-stand-for-agents",
                "--filter",
                "label=io.eval-stand.project=" + manager_project,
                "--format",
                "{{.ID}}",
            )
            .decode()
            .splitlines()
        )
        compose_rows = (
            docker(
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=" + compose_project,
                "--format",
                '{{.ID}} {{.Label "com.docker.compose.service"}}',
            )
            .decode()
            .splitlines()
        )
        own_services = {
            "gateway",
            "harness",
            "sandbox-manager",
            "llm-proxy",
            "mock-provider",
            "mock-provider-alt",
            "sandbox-image",
            "runner",
        }
        containers.extend(
            row.split()[0]
            for row in compose_rows
            if len(row.split()) == 2 and row.split()[1] in own_services
        )
        volumes = (
            docker(
                "volume",
                "ls",
                "--filter",
                "label=com.docker.compose.project=" + compose_project,
                "--format",
                "{{.Name}}",
            )
            .decode()
            .splitlines()
        )
        dynamic_volumes = (
            docker(
                "volume",
                "ls",
                "--filter",
                "label=io.eval-stand.owner=eval-stand-for-agents",
                "--filter",
                "label=io.eval-stand.project=" + manager_project,
                "--format",
                "{{.Name}}",
            )
            .decode()
            .splitlines()
        )
        preserved = all(
            Path(path).is_file()
            and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
            for path, digest in before.items()
        )
        report.update(
            returncode=result.returncode,
            foreign_running=info["State"]["Running"],
            runtime_containers_left=containers,
            state_volumes_left=volumes + dynamic_volumes,
            exported_files_checked=len(before),
            exports_preserved=preserved,
            management_env_preserved=hashlib.sha256(
                Path(".env").read_bytes()
            ).hexdigest()
            == env_digest,
        )
        auth_preserved = True
        if auth_before is not None:
            metadata = auth_path.stat()
            auth_preserved = (
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_mode,
            ) == auth_before
        report["auth_store_metadata_preserved"] = (
            auth_preserved if auth_before is not None else None
        )
        report["passed"] = (
            result.returncode == 0
            and info["State"]["Running"]
            and not containers
            and not volumes
            and not dynamic_volumes
            and preserved
            and report["management_env_preserved"]
            and auth_preserved
        )
    finally:
        # Удаляем только созданный этим тестом sentinel; auth-setup не адресуется.
        docker("rm", "-f", foreign)
        path = Path("results") / f"native-shutdown-{args.mode}-{identifier[:8]}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(path), **report}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
