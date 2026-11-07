"""Сверка только помеченных ресурсов проекта после потери локального журнала."""

from __future__ import annotations

from typing import TYPE_CHECKING

from contracts import ContractError, validate

from .config import OWNER, PREFIX
from .manager import ManagerError, resource_name

if TYPE_CHECKING:
    from .manager import Manager


async def discover_orphans(manager: Manager) -> list[dict]:
    unsaved = []
    filters = [
        "--filter",
        f"label={PREFIX}.owner={OWNER}",
        "--filter",
        f"label={PREFIX}.project={manager.project}",
    ]
    for kind in ("container", "volume"):
        command = [
            kind,
            "ls",
            *(["-a"] if kind == "container" else []),
            *filters,
            "--format",
            "{{.Names}}" if kind == "container" else "{{.Name}}",
        ]
        reply = await manager.docker.command(command, limit=65536)
        for name in reply.stdout.decode().splitlines():
            info = await manager.docker.inspect(kind, name)
            if info is None:
                continue
            labels = (
                info.get("Labels", {})
                if kind == "volume"
                else info.get("Config", {}).get("Labels", {})
            )
            try:
                record = _record(manager, name, kind, labels)
            except (ContractError, KeyError):
                # Неизвестная разметка не даёт права на удаление.
                continue
            path = manager._path(record["run_id"])
            if path.exists():
                current = manager._load(record["run_id"])
                if current["handle"] != record["handle"]:
                    raise ManagerError("orphan_handle_conflict")
            else:
                try:
                    manager._save(record)
                except OSError:
                    unsaved.append(record)
    return unsaved


def _record(manager: Manager, name: str, kind: str, labels: dict) -> dict:
    role = labels[f"{PREFIX}.role"]
    expected_kind = {
        "sandbox": "container",
        "prepare": "container",
        "collector": "container",
        "inputs": "volume",
    }[role]
    if expected_kind != kind:
        raise KeyError("resource_role")
    record = {
        "version": "v0",
        "run_id": labels[f"{PREFIX}.run"],
        "project_id": labels[f"{PREFIX}.project"],
        "handle": labels[f"{PREFIX}.handle"],
        "fingerprint": labels[f"{PREFIX}.fingerprint"],
        "state": "recovering",
        "collection_status": "not_started",
        "cleanup_status": "not_started",
        "errors": [],
    }
    validate("manager_status", record)
    record["name"] = resource_name(record["project_id"], record["run_id"])
    suffix = {
        "sandbox": "",
        "prepare": "-prepare",
        "collector": "-collector",
        "inputs": "-inputs",
    }[role]
    if name != record["name"] + suffix or any(
        labels.get(key) != value for key, value in manager._labels(record, role).items()
    ):
        raise KeyError("resource_identity")
    return {
        **record,
        "protected_inputs": False,
        "input_manifest": {},
        "execution_started": True,
        "request": None,
    }
