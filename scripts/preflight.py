"""Сохранить факты среды без запуска нагрузки и без изменения настроек."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from services.common.storage import atomic_json


def inspect(shared_cpu: bool = False) -> dict:
    reply = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        capture_output=True,
        timeout=15,
        check=True,
    )
    info = json.loads(reply.stdout)
    required_limits = all(
        info.get(key) for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota")
    )
    image_reply = subprocess.run(
        ["docker", "image", "inspect", "eval-stand-codex:0.159.3"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    source_ready = False
    if image_reply.returncode == 0:
        images = json.loads(image_reply.stdout)
        labels = images[0].get("Config", {}).get("Labels", {})
        source_ready = (
            labels.get("org.opencontainers.image.revision")
            == "01fc69f4026735edfdf6789820549727a4867b11"
            and labels.get("org.eval-stand.codex-origin") == "source_build"
            and labels.get("org.eval-stand.codex-version") == "0.159.3"
        )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "architecture": info["Architecture"],
        "docker_version": info["ServerVersion"],
        "operating_system": info["OperatingSystem"],
        "cpu_available": info["NCPU"],
        "ram_available_bytes": info["MemTotal"],
        "storage_driver": info["Driver"],
        "driver_status": info.get("DriverStatus", []),
        "cgroup_version": info.get("CgroupVersion"),
        "ceilings": {
            "cpu": 1,
            "memory_bytes": 8_000_000_000,
            "docker_disk_setting_gb": 32,
            "build_deadline_seconds": 3600,
        },
        "source_commit": "01fc69f4026735edfdf6789820549727a4867b11",
        "rust_toolchain": "1.95.0",
        "source_build": "image_available" if source_ready else "image_missing",
        "quota_status": "docker_engine_single_cpu"
        if shared_cpu and info["NCPU"] == 1
        else "cpu_memory_enforced_by_build_controller",
        "shared_cpu_requested": shared_cpu,
        "shared_cpu_verified": shared_cpu and info["NCPU"] == 1,
        "ready": required_limits
        and source_ready
        and (not shared_cpu or info["NCPU"] == 1),
        "reason": "Проверяются квоты CPU/RAM и наличие закреплённого source-build образа; готовность сервисов проверяется healthcheck",
        "peak_memory_bytes": None,
        "peak_disk_bytes": None,
        "build_duration_seconds": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--shared-cpu",
        action="store_true",
        help="Допустить burst-лимиты только при одном CPU всего Docker engine",
    )
    args = parser.parse_args()
    try:
        report = inspect(args.shared_cpu)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        print("Не удалось прочитать параметры Docker; нагрузка не запускалась")
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            print("Файл предыдущего preflight уже существует")
            return 2
        atomic_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
