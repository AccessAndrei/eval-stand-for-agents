"""Ограниченная сборка закреплённого Codex; не изменяет настройки Docker Desktop."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMMIT = "01fc69f4026735edfdf6789820549727a4867b11"
VERSION = "0.159.3"
BUILDER = "eval-stand-codex-source"
BUILDER_IMAGE = (
    "moby/buildkit:v0.34.2@sha256:"
    "c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe"
)
CONTAINER = f"buildx_buildkit_{BUILDER}0"
IMAGE = f"eval-stand-codex:{VERSION}"
LOG_LIMIT = 16_777_216
_DEADLINE: float | None = None


def command(args: list[str], timeout: float = 30, *, cleanup: bool = False) -> str:
    if _DEADLINE is not None and not cleanup:
        timeout = min(timeout, _DEADLINE - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired(args, 0)
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:])
    return result.stdout.strip()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def lock_packages(document: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Идентичность всех внешних зависимостей, без локальных workspace-версий."""
    return sorted(
        (item["name"], item["version"], item["source"], item.get("checksum"))
        for item in document["package"]
        if "source" in item
    )


def normalize_lock(root: Path) -> dict[str, Any]:
    workspace = tomllib.loads((root / "Cargo.toml").read_text())
    version = workspace["workspace"]["package"]["version"]
    names = set()
    for path in root.rglob("Cargo.toml"):
        package = tomllib.loads(path.read_text()).get("package", {})
        if package.get("version") == {"workspace": True}:
            names.add(package["name"])
    path = root / "Cargo.lock"
    original = path.read_text()
    before = tomllib.loads(original)
    changes: dict[str, tuple[str, str]] = {}
    sections = original.split("[[package]]")
    for index in range(1, len(sections)):
        package = tomllib.loads("[[package]]" + sections[index])["package"][0]
        if (
            "source" not in package
            and package["name"] in names
            and package["version"] != version
        ):
            changes[package["name"]] = (package["version"], version)
            sections[index] = re.sub(
                r'^version = "[^"]+"',
                f'version = "{version}"',
                sections[index],
                count=1,
                flags=re.MULTILINE,
            )
    normalized = "[[package]]".join(sections)
    for name, (old, new) in changes.items():
        normalized = normalized.replace(f'"{name} {old}"', f'"{name} {new}"')
    after = tomllib.loads(normalized)
    if lock_packages(before) != lock_packages(after):
        raise ValueError("Нормализация изменила внешние зависимости")
    evidence = root / ".build-evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "lock-normalization.patch").write_text(
        "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                normalized.splitlines(keepends=True),
                fromfile="upstream/Cargo.lock",
                tofile="normalized/Cargo.lock",
            )
        )
    )
    report = {
        "before_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "after_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "workspace_version_changes": changes,
        "external_dependencies_unchanged": True,
    }
    write_json(evidence / "lock-normalization.json", report)
    path.write_text(normalized)
    return report


def provenance(root: Path) -> None:
    evidence = root / ".build-evidence"
    value = {
        "source_commit": command(["git", "-C", str(root), "rev-parse", "HEAD"]),
        "origin": "source_build",
        "version": VERSION,
        "rustc": command(["rustc", "--version"]),
        "cargo": command(["cargo", "--version"]),
        "architecture": command(["uname", "-m"]),
        "profile": "dev-small",
        "jobs": 1,
        "incremental": False,
        "binary_sha256": hashlib.sha256(Path("/out/codex").read_bytes()).hexdigest(),
        "binary_version": command(["/out/codex", "--version"]),
        "lock": json.loads((evidence / "lock-normalization.json").read_text()),
    }
    if value["source_commit"] != COMMIT:
        raise ValueError("Исходный commit изменился")
    validate_version(value["binary_version"])
    write_json(evidence / "provenance.json", value)


def validate_version(actual: str) -> None:
    if actual.strip() != f"codex-cli {VERSION}":
        raise ValueError("Версия собранного бинарника не соответствует закреплённой")


def preflight(info: dict[str, Any], memory: int, timeout: int) -> str:
    if not 0 < memory <= 8_000_000_000 or memory > info["MemTotal"]:
        raise ValueError("Лимит памяти превышает разрешённый или доступный объём")
    if not 0 < timeout <= 3600:
        raise ValueError("Проверка ограничена 60 минутами")
    if not all(info.get(key) for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota")):
        raise ValueError("Docker не подтверждает необходимые квоты")
    if info["Architecture"] not in {"aarch64", "arm64", "x86_64", "amd64"}:
        raise ValueError("Целевая архитектура не поддерживается")
    return (
        "linux/arm64" if info["Architecture"] in {"aarch64", "arm64"} else "linux/amd64"
    )


def recorded_builder_ids() -> set[str]:
    ids = set()
    for path in Path("results").glob("source-build-*/report.json"):
        try:
            builder_id = json.loads(path.read_text()).get("builder", {}).get("Id")
            if isinstance(builder_id, str):
                ids.add(builder_id)
        except (OSError, json.JSONDecodeError):
            continue
    return ids


def validate_builder(container: dict[str, Any], memory: int) -> None:
    config = container["HostConfig"]
    if (
        container["Name"] != "/" + CONTAINER
        or container["Config"]["Image"] != BUILDER_IMAGE
    ):
        raise ValueError("Идентичность builder не подтверждена")
    if (
        config["Memory"] != memory
        or config["MemorySwap"] != memory
        or config["CpuQuota"] != 100_000
        or config["CpuPeriod"] != 100_000
    ):
        raise ValueError("Квоты существующего builder не соответствуют запросу")


def prepare_builder(memory: int, ownership: dict[str, str]) -> str:
    listed = command(["docker", "buildx", "ls", "--format", "{{.Name}}"])
    created = BUILDER not in listed.splitlines()
    if created:
        command(
            [
                "docker",
                "buildx",
                "create",
                "--name",
                BUILDER,
                "--driver",
                "docker-container",
                "--driver-opt",
                f"image={BUILDER_IMAGE}",
                "--driver-opt",
                f"memory={memory}",
                "--driver-opt",
                f"memory-swap={memory}",
                "--driver-opt",
                "cpu-quota=100000",
                "--driver-opt",
                "cpu-period=100000",
                "--driver-opt",
                "restart-policy=no",
                "--buildkitd-config",
                "docker/buildkit.toml",
            ]
        )
    else:
        current = json.loads(command(["docker", "inspect", CONTAINER]))[0]
        validate_builder(current, memory)
        if current["Id"] not in recorded_builder_ids():
            raise ValueError("Существующий builder не принадлежит журналу проекта")
        if current["State"]["Running"]:
            raise ValueError("Выделенный builder уже работает; повтор не допускается")
        ownership["container_id"] = current["Id"]
    try:
        command(["docker", "buildx", "inspect", "--bootstrap", BUILDER], timeout=180)
    finally:
        if created:
            # После собственного create имя принадлежит этой попытке. Даже
            # неудачный bootstrap мог успеть запустить контейнер.
            try:
                current = json.loads(
                    command(
                        ["docker", "inspect", CONTAINER],
                        timeout=5,
                        cleanup=True,
                    )
                )[0]
                if (
                    current["Name"] == "/" + CONTAINER
                    and current["Config"]["Image"] == BUILDER_IMAGE
                ):
                    ownership["container_id"] = current["Id"]
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
    container = json.loads(command(["docker", "inspect", CONTAINER]))[0]
    validate_builder(container, memory)
    return container["Id"]


def snapshot() -> dict[str, Any]:
    """Измерения, а не декларация жёсткой квоты диска."""
    measurement: dict[str, Any] = {"time": datetime.now(timezone.utc).isoformat()}
    reads = {
        "memory_peak_bytes": ["cat", "/sys/fs/cgroup/memory.peak"],
        "memory_current_bytes": ["cat", "/sys/fs/cgroup/memory.current"],
        "memory_events": ["cat", "/sys/fs/cgroup/memory.events"],
        "builder_disk": ["df", "-B1", "/var/lib/buildkit"],
        "builder_state_kib": ["du", "-sk", "/var/lib/buildkit"],
    }
    for name, args in reads.items():
        try:
            measurement[name] = command(["docker", "exec", CONTAINER, *args], timeout=5)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            measurement[name] = {"unavailable": type(error).__name__}
    return measurement


def stop_builder(container_id: str | None) -> None:
    if container_id is None:
        return
    inspected = json.loads(
        command(
            ["docker", "inspect", container_id],
            timeout=5,
            cleanup=True,
        )
    )[0]
    if inspected["Id"] != container_id or inspected["Name"] != "/" + CONTAINER:
        raise ValueError("Владение builder не подтверждено")
    try:
        command(
            ["docker", "stop", "--time", "5", container_id], timeout=10, cleanup=True
        )
    except (RuntimeError, subprocess.TimeoutExpired):
        command(["docker", "kill", container_id], timeout=5, cleanup=True)


def monitor(
    process: subprocess.Popen, directory: Path, deadline: float, container_id: str
) -> str:
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    next_measurement, written = 0.0, 0
    status = "failed"
    with (directory / "build.log").open("wb") as log:
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline - 40:
                status = "timeout"
                stop_builder(container_id)
                break
            for key, _ in selector.select(timeout=1):
                chunk = os.read(key.fileobj.fileno(), 16_384)
                if written < LOG_LIMIT:
                    piece = chunk[: LOG_LIMIT - written]
                    log.write(piece)
                    log.flush()
                    written += len(piece)
            if now >= next_measurement:
                measurement = snapshot()
                with (directory / "measurements.jsonl").open("a") as stream:
                    stream.write(json.dumps(measurement) + "\n")
                next_measurement = now + 30
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        # Не ждать EOF от случайно уцелевшего потомка Docker CLI.
        while written < LOG_LIMIT and selector.select(timeout=0):
            chunk = os.read(process.stdout.fileno(), min(16_384, LOG_LIMIT - written))
            if not chunk:
                break
            log.write(chunk)
            written += len(chunk)
    selector.close()
    return "success" if process.wait(timeout=5) == 0 else status


def export_artifact(image_id: str, platform: str, directory: Path) -> dict[str, Any]:
    """Экспорт собственного source-build для локального named build context."""
    root = Path("build/codex-artifact")
    binary = root / "usr/local/bin/codex"
    metadata = root / "usr/local/share/codex-build"
    binary.parent.mkdir(parents=True, exist_ok=True)
    metadata.mkdir(parents=True, exist_ok=True)
    name = "eval-stand-codex-export-" + uuid.uuid4().hex[:12]
    container_id = command(
        [
            "docker",
            "create",
            "--pull=never",
            f"--platform={platform}",
            "--name",
            name,
            "--network=none",
            "--read-only",
            image_id,
            "--version",
        ]
    )
    try:
        command(["docker", "cp", container_id + ":/usr/local/bin/codex", str(binary)])
        command(
            [
                "docker",
                "cp",
                container_id + ":/usr/local/share/codex-build/.",
                str(metadata),
            ]
        )
    finally:
        command(["docker", "rm", container_id], timeout=5, cleanup=True)
    proof = json.loads((metadata / "provenance.json").read_text())
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if (
        proof["source_commit"] != COMMIT
        or proof["origin"] != "source_build"
        or proof["binary_sha256"] != digest
    ):
        raise ValueError("Экспортированный бинарник не соответствует provenance")
    validate_version(proof["binary_version"])
    binary.chmod(0o755)
    for filename in (
        "provenance.json",
        "lock-normalization.json",
        "lock-normalization.patch",
    ):
        (directory / filename).write_bytes((metadata / filename).read_bytes())
    result = {
        "source_image_id": image_id,
        "binary_sha256": digest,
        "binary_bytes": binary.stat().st_size,
        "export_root": str(root.resolve()),
        "temporary_container_removed": True,
    }
    write_json(directory / "artifact-export.json", result)
    return result


def build(memory: int, timeout: int, attempt_reason: str | None = None) -> Path:
    global _DEADLINE
    started = time.monotonic()
    _DEADLINE = started + timeout
    directory = Path("results") / (
        "source-build-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    directory.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "source_commit": COMMIT,
        "status": "preflight",
        "memory_limit_bytes": memory,
        "cpu_quota": 1,
        "timeout_s": timeout,
        "profile": "dev-small",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "attempt_reason": attempt_reason,
    }
    process = None
    container_id = None
    ownership: dict[str, str] = {}
    try:
        info = json.loads(command(["docker", "info", "--format", "{{json .}}"]))
        platform = preflight(info, memory, timeout)
        report["platform"] = platform
        report["available_memory_bytes"] = info["MemTotal"]
        report["disk_before"] = command(
            ["docker", "system", "df", "--format", "{{json .}}"]
        )
        container_id = prepare_builder(memory, ownership)
        builder = json.loads(command(["docker", "inspect", CONTAINER]))[0]
        report["builder"] = {
            key: builder[key] for key in ("Id", "Image", "HostConfig", "Mounts")
        }
        report["status"] = "building"
        write_json(directory / "report.json", report)
        watchdog_deadline = time.time() + max(0, _DEADLINE - time.monotonic())
        with (directory / "watchdog.log").open("ab") as watchdog_log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.build_codex",
                    "--watchdog",
                    str(directory),
                    "--watchdog-deadline",
                    str(watchdog_deadline),
                    "--watchdog-container",
                    container_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=watchdog_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        args = [
            "docker",
            "buildx",
            "build",
            "--builder",
            BUILDER,
            "--platform",
            platform,
            "--progress=plain",
            "--provenance=false",
            "--load",
            "--tag",
            IMAGE,
            "--file",
            "docker/Dockerfile.codex",
            "--target",
            "runtime",
            "--metadata-file",
            str(directory / "build-metadata.json"),
            ".",
        ]
        if time.monotonic() >= _DEADLINE:
            raise subprocess.TimeoutExpired(args, timeout)
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        report["status"] = monitor(process, directory, _DEADLINE, container_id)
        report["final_measurement"] = snapshot()
        if report["status"] == "success":
            image = json.loads(command(["docker", "image", "inspect", IMAGE]))[0]
            report["image_id"], report["image_size_bytes"] = image["Id"], image["Size"]
            report["version"] = command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    f"--platform={platform}",
                    "--network=none",
                    "--read-only",
                    "--cpus=1",
                    "--memory=268435456",
                    image["Id"],
                    "--version",
                ]
            )
            validate_version(report["version"])
            report["origin"] = "source_build"
            write_json(
                directory / "provenance.runtime.json",
                {
                    "codex_version": VERSION,
                    "commit": COMMIT,
                    "origin": "source_build",
                    "image_digest": image["Id"],
                    "architecture": image["Architecture"],
                },
            )
            report["artifact_export"] = export_artifact(
                image["Id"], platform, directory
            )
    except KeyboardInterrupt:
        report.update(status="interrupted", error="controller_received_interrupt")
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as error:
        report.update(status="failed", error=str(error)[-4000:])
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            owned_id = ownership.get("container_id")
            if owned_id is not None and "builder" not in report:
                report["builder"] = {"Id": owned_id}
            stop_builder(owned_id)
            report["builder_stopped"] = owned_id is not None
            report["disk_after"] = command(
                ["docker", "system", "df", "--format", "{{json .}}"],
                cleanup=True,
            )
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            report["cleanup_error"] = str(error)[-1000:]
        report["duration_s"] = time.monotonic() - started
        write_json(directory / "report.json", report)
    print(
        json.dumps(
            {"report": str(directory / "report.json"), "status": report["status"]}
        )
    )
    return directory


def watchdog(directory: Path, deadline: float, container_id: str) -> None:
    """Независимый срок остановки, даже если основной контроллер был убит."""
    path = directory / "report.json"
    while time.time() < deadline - 25:
        try:
            report = json.loads(path.read_text())
            if report.get("builder_stopped"):
                return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(min(5, max(0, deadline - 25 - time.time())))
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        report = {"source_commit": COMMIT}
    if report.get("builder_stopped"):
        return
    stop_builder(container_id)
    report.update(
        status="timeout",
        builder_stopped=True,
        error="independent_watchdog_deadline",
        watchdog_stopped_at=datetime.now(timezone.utc).isoformat(),
    )
    write_json(path, report)


def interrupted(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(f"signal={signum}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normalize-lock", type=Path)
    parser.add_argument("--write-provenance", type=Path)
    parser.add_argument("--memory-bytes", type=int, default=7_000_000_000)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--attempt-reason")
    parser.add_argument("--watchdog", type=Path)
    parser.add_argument("--watchdog-deadline", type=float)
    parser.add_argument("--watchdog-container")
    args = parser.parse_args()
    if args.watchdog:
        watchdog(args.watchdog, args.watchdog_deadline, args.watchdog_container)
    elif args.normalize_lock:
        normalize_lock(args.normalize_lock)
    elif args.write_provenance:
        provenance(args.write_provenance)
    else:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, interrupted)
        directory = build(args.memory_bytes, args.timeout_seconds, args.attempt_reason)
        if json.loads((directory / "report.json").read_text())["status"] != "success":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
