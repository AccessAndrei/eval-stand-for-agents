"""Жизненный цикл среды: запись перед побочным эффектом и отсутствие replay."""

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from contracts import validate
from services.common.storage import atomic_json

from .config import AGENT_UID, CODEX, OWNER, PREFIX, PYTHON, SUPERVISOR_UID, Limits
from .docker import Docker, DockerError


class ManagerError(RuntimeError):
    def __init__(self, code: str, http_status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def resource_name(project: str, run: str) -> str:
    digest = hashlib.sha256(f"{project}\0{run}".encode()).hexdigest()[:32]
    return f"eval-{digest}"


class Manager:
    def __init__(
        self,
        root: Path,
        project: str,
        image: str,
        docker: Docker | None = None,
        limits: Limits | None = None,
    ) -> None:
        validate(
            "lookup", {"version": "v0", "run_id": "validation", "project_id": project}
        )
        if not image or image.startswith("-"):
            raise ValueError("invalid_image")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root, self.project, self.image = root, project, image
        self.docker, self.limits = docker or Docker(), limits or Limits()
        self.lock = asyncio.Lock()
        self.tunnels: dict[str, asyncio.subprocess.Process] = {}
        self.helper = Path(__file__).with_name("helpers.py").read_text()
        self.closing = False
        self.persistence_failed = False
        self.transport_failures: dict[str, int] = {}

    def _path(self, run_id: str) -> Path:
        validate(
            "lookup", {"version": "v0", "run_id": run_id, "project_id": self.project}
        )
        return self.root / f"{run_id}.json"

    def _load(self, run_id: str) -> dict:
        path = self._path(run_id)
        if not path.exists():
            raise ManagerError("unknown_run", 404)
        return json.loads(path.read_text())

    def _save(self, record: dict) -> None:
        self._transport_error(record)
        try:
            atomic_json(self._path(record["run_id"]), record)
        except OSError:
            self.persistence_failed = True
            raise

    def _labels(self, record: dict, role: str) -> dict[str, str]:
        return {
            f"{PREFIX}.{key}": value
            for key, value in {
                "owner": OWNER,
                "project": self.project,
                "run": record["run_id"],
                "handle": record["handle"],
                "fingerprint": record["fingerprint"],
                "role": role,
            }.items()
        }

    def _label_args(self, record: dict, role: str) -> list[str]:
        labels = [
            part
            for pair in self._labels(record, role).items()
            for part in ("--label", "=".join(pair))
        ]
        # Compose добавляет метки в build image; динамическая среда не является
        # его сервисом и должна удаляться только после collect через менеджер.
        return labels + [
            "--label",
            "com.docker.compose.project=",
            "--label",
            "com.docker.compose.service=",
            "--label",
            "com.docker.compose.version=",
        ]

    async def _owned(
        self, record: dict, role: str, kind: str = "container"
    ) -> dict | None:
        suffix = {
            "sandbox": "",
            "prepare": "-prepare",
            "collector": "-collector",
            "inputs": "-inputs",
        }[role]
        info = await self.docker.inspect(kind, record["name"] + suffix)
        if info is not None:
            labels = (
                info.get("Labels")
                if kind == "volume"
                else info.get("Config", {}).get("Labels")
            )
            if not labels or any(
                labels.get(k) != v for k, v in self._labels(record, role).items()
            ):
                raise ManagerError("foreign_resource", 403)
        return info

    def _check_project(self, project: str) -> None:
        if project != self.project:
            raise ManagerError("foreign_project", 403)

    async def lookup(self, run_id: str, project: str) -> dict:
        self._check_project(project)
        async with self.lock:
            record = self._load(run_id)
            if record["state"] not in {"released", "failed"}:
                info = await self._owned(record, "sandbox")
                if info is None:
                    record["state"] = "failed"
                    record["errors"].append(
                        {
                            "stage": "provision",
                            "code": "resource_missing",
                            "message": "Среда отсутствует",
                        }
                    )
                    self._save(record)
            return self.public(record)

    def public(self, record: dict) -> dict:
        self._transport_error(record)
        fields = (
            "version",
            "run_id",
            "project_id",
            "handle",
            "fingerprint",
            "state",
            "collection_status",
            "cleanup_status",
            "errors",
        )
        return {key: record[key] for key in fields}

    def _transport_error(self, record: dict) -> None:
        if record["run_id"] not in self.transport_failures:
            return
        code = self.transport_failures[record["run_id"]]
        error = {
            "stage": "execution",
            "code": "transport_error",
            "message": f"exec-server завершился с кодом {code} во время исполнения",
        }
        if error not in record["errors"]:
            record["errors"].append(error)

    async def executor_exited(self, run_id: str, returncode: int) -> None:
        if returncode == 0 or self._load(run_id)["state"] != "executing":
            return
        # Фиксируем наблюдение до ожидания lock: сбор/очистка уже могут его занимать.
        self.transport_failures[run_id] = returncode
        async with self.lock:
            record = self._load(run_id)
            try:
                self._save(record)
            except OSError:
                pass  # Ошибка остаётся в памяти и отражается в ответе release.

    def _by_handle(self, handle: str, run_id: str, project: str) -> dict:
        self._check_project(project)
        record = self._load(run_id)
        if not secrets.compare_digest(record["handle"], handle):
            raise ManagerError("foreign_handle", 403)
        return record

    async def provision(self, request: dict[str, Any]) -> dict:
        validate("provision", request)
        self._check_project(request["project_id"])
        async with self.lock:
            if self.closing:
                raise ManagerError("manager_stopping", 503)
            if self.persistence_failed:
                raise ManagerError("storage_budget_exceeded", 507)
            if self._path(request["run_id"]).exists():
                record = self._load(request["run_id"])
                if record["request"] != request:
                    raise ManagerError("run_conflict")
                return self.public(record)
            for path in self.root.glob("*.json"):
                previous = json.loads(path.read_text())
                if previous["cleanup_status"] != "success":
                    raise ManagerError("busy")
            readonly = request["read_only_paths"]
            if readonly not in ([], ["input"]) or (
                readonly and request["task_id"] != "B5.1"
            ):
                raise ManagerError("unsupported_readonly_layout", 400)
            if request["task_id"] == "B5.1" and readonly != ["input"]:
                raise ManagerError("protected_inputs_required", 400)
            record = self._new_record(request)
            self._save(record)
            try:
                await self._create(record)
                record["state"] = "ready"
                self._save(record)
            except (DockerError, OSError, ValueError) as error:
                record["state"] = "failed"
                record["errors"].append(
                    {
                        "stage": "provision",
                        "code": "provision_failed",
                        "message": "Создание среды не подтверждено",
                    }
                )
                self._save(record)
                raise ManagerError("provision_failed", 502) from error
            return self.public(record)

    def _new_record(self, request: dict) -> dict:
        return {
            "version": "v0",
            "run_id": request["run_id"],
            "project_id": self.project,
            "handle": secrets.token_hex(24),
            "fingerprint": request["fingerprint"],
            "state": "provisioning",
            "collection_status": "not_started",
            "cleanup_status": "not_started",
            "errors": [],
            "request": request,
            "name": resource_name(self.project, request["run_id"]),
            "protected_inputs": False,
            "input_manifest": {},
            "execution_started": False,
        }

    async def _prepare_inputs(self, record: dict) -> None:
        volume = record["name"] + "-inputs"
        await self.docker.command(
            ["volume", "create", *self._label_args(record, "inputs"), volume]
        )
        files = [
            item
            for item in record["request"]["files"]
            if item["path"].startswith("input/")
        ]
        payload = {"files": [{**item, "path": item["path"][6:]} for item in files]}
        await self.docker.command(
            [
                "run",
                "--pull=never",
                "--name",
                record["name"] + "-prepare",
                "-i",
                *self._label_args(record, "prepare"),
                *self.limits.docker_args(),
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={volume},dst=/input,volume-nocopy",
                "--entrypoint",
                PYTHON,
                self.image,
                "-I",
                "-S",
                "-c",
                self.helper,
                "prepare",
                "/input",
            ],
            json.dumps(payload).encode(),
        )
        await self.docker.command(["rm", record["name"] + "-prepare"])
        record["input_manifest"] = {
            item["path"]: hashlib.sha256(item["content"].encode()).hexdigest()
            for item in files
        }
        self._save(record)

    async def _create(self, record: dict) -> None:
        readonly = record["request"]["read_only_paths"]
        if readonly:
            await self._prepare_inputs(record)
        mounts = (
            [
                "--mount",
                f"type=volume,src={record['name']}-inputs,dst=/work/input,readonly,volume-nocopy",
            ]
            if readonly
            else []
        )
        await self.docker.command(
            [
                "create",
                "--pull=never",
                "--name",
                record["name"],
                *self._label_args(record, "sandbox"),
                *self.limits.docker_args(),
                "--user",
                SUPERVISOR_UID,
                "--env",
                "HOME=/nonexistent",
                "--env",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                *mounts,
                "--entrypoint",
                PYTHON,
                self.image,
                "-I",
                "-S",
                "-c",
                self.helper,
                "idle",
            ]
        )
        await self.docker.command(["start", record["name"]])
        writable = [
            item
            for item in record["request"]["files"]
            if not (readonly and item["path"].startswith("input/"))
        ]
        await self.docker.command(
            self._helper_args(record, "prepare", "/work"),
            json.dumps({"files": writable}).encode(),
        )
        if readonly:
            info = await self._owned(record, "sandbox")
            record["protected_inputs"] = bool(
                info
                and any(
                    mount.get("Destination") == "/work/input"
                    and mount.get("RW") is False
                    and mount.get("Name") == record["name"] + "-inputs"
                    for mount in info.get("Mounts", [])
                )
            )
            if not record["protected_inputs"]:
                raise DockerError("readonly_mount_unconfirmed")

    def _helper_args(self, record: dict, *args: str) -> list[str]:
        return [
            "exec",
            "-i",
            "--user",
            AGENT_UID,
            record["name"],
            PYTHON,
            "-I",
            "-S",
            "-c",
            self.helper,
            *args,
        ]

    async def _trusted_collection(self, record: dict, *args: str) -> bytes:
        sandbox = await self._owned(record, "sandbox")
        if sandbox is None:
            raise DockerError("sandbox_missing")
        previous = await self._owned(record, "collector")
        if previous is not None:
            await self.docker.command(["rm", "-f", previous["Id"]])
        reply = await self.docker.command(
            [
                "run",
                "--pull=never",
                "--name",
                record["name"] + "-collector",
                *self._label_args(record, "collector"),
                *self.limits.collector_args(),
                "--pid",
                "container:" + sandbox["Id"],
                "--entrypoint",
                PYTHON,
                self.image,
                "-I",
                "-S",
                "-c",
                self.helper,
                *args,
            ]
        )
        await self.docker.command(["rm", record["name"] + "-collector"])
        return reply.stdout

    async def connect(
        self, handle: str, run_id: str, project: str
    ) -> asyncio.subprocess.Process:
        async with self.lock:
            record = self._by_handle(handle, run_id, project)
            if (
                self.closing
                or record["state"] != "ready"
                or record["execution_started"]
            ):
                raise ManagerError("execution_cannot_replay")
            if await self._owned(record, "sandbox") is None:
                raise ManagerError("sandbox_missing", 502)
            record.update(state="executing", execution_started=True)
            self._save(record)
            process = await self.docker.spawn(
                [
                    "exec",
                    "-i",
                    "--user",
                    AGENT_UID,
                    "--workdir",
                    "/work",
                    record["name"],
                    "/usr/bin/env",
                    "-i",
                    "HOME=/nonexistent",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                    CODEX,
                    "exec-server",
                    "--listen",
                    "stdio",
                ]
            )
            self.tunnels[handle] = process
            return process

    async def collect(self, request: dict) -> dict:
        validate("collect", request)
        async with self.lock:
            record = self._by_handle(
                request["handle"], request["run_id"], request["project_id"]
            )
            return await self._collect_record(record)

    async def _collect_record(self, record: dict) -> dict:
        collection_file = self.root / (record["run_id"] + ".collection")
        if collection_file.exists():
            return json.loads(collection_file.read_text())
        record["state"] = "quiescing"
        try:
            self._save(record)
        except OSError:
            pass  # Сбор и удаление нужны даже при недоступности журнала.
        try:
            if await self._owned(record, "sandbox") is None:
                raise DockerError("sandbox_missing")
            output = await self._trusted_collection(
                record,
                "collect_trusted",
                str(self.limits.collected),
                str(self.limits.files),
            )
            collection = json.loads(output)
            if collection.get("status") != "complete":
                raise DockerError("collection_not_complete")
            collection.update(
                protected_inputs=record["protected_inputs"],
                input_manifest=record["input_manifest"],
            )
            record.update(state="collected", collection_status="complete")
        except (DockerError, ValueError, OSError):
            collection = {
                "status": "incomplete",
                "files": [],
                "manifest": {},
                "protected_inputs": False,
                "input_manifest": record["input_manifest"],
            }
            record["collection_status"] = "incomplete"
            record["errors"].append(
                {
                    "stage": "collection",
                    "code": "collection_failed",
                    "message": "Полнота стабильной копии не подтверждена",
                }
            )
        try:
            atomic_json(collection_file, collection)
            self._save(record)
        except OSError as error:
            self.persistence_failed = True
            raise ManagerError("storage_budget_exceeded", 507) from error
        return collection

    async def release(self, request: dict) -> dict:
        validate("release", request)
        async with self.lock:
            record = self._by_handle(
                request["handle"], request["run_id"], request["project_id"]
            )
            await self._release_record(record)
            return self.public(record)

    async def _release_record(self, record: dict) -> None:
        try:
            for role in ("collector", "sandbox", "prepare"):
                info = await self._owned(record, role)
                if info:
                    await self.docker.command(["rm", "-f", info["Id"]])
                    if await self._owned(record, role) is not None:
                        raise DockerError("resource_remains")
            if await self._owned(record, "inputs", "volume"):
                await self.docker.command(["volume", "rm", record["name"] + "-inputs"])
                if await self._owned(record, "inputs", "volume") is not None:
                    raise DockerError("volume_remains")
            process = self.tunnels.get(record["handle"])
            if process:
                from services.common.processes import stop_group

                if not await stop_group(process):
                    raise DockerError("relay_remains")
                self.tunnels.pop(record["handle"], None)
            record.update(state="released", cleanup_status="success")
        except (DockerError, OSError):
            record["cleanup_status"] = "failed"
            record["errors"].append(
                {
                    "stage": "cleanup",
                    "code": "cleanup_failed",
                    "message": "Удаление ресурсов не подтверждено",
                }
            )
        try:
            self._save(record)
        except OSError:
            record["errors"].append(
                {
                    "stage": "storage",
                    "code": "storage_budget_exceeded",
                    "message": "Ресурсы обработаны, но состояние очистки не удалось сохранить",
                }
            )

    async def cancel(self, request: dict) -> dict:
        validate("manager_cancel", request)
        async with self.lock:
            record = self._by_handle(
                request["handle"], request["run_id"], request["project_id"]
            )
            if record["state"] == "released":
                return self.public(record)
            record["state"] = "quiescing"
            self._save(record)
            await self._trusted_collection(record, "quiesce")
            return self.public(record)

    async def shutdown(self) -> None:
        self.closing = True
        async with asyncio.timeout(90), self.lock:
            for path in sorted(self.root.glob("*.json")):
                record = json.loads(path.read_text())
                if record["cleanup_status"] != "success":
                    await self._recover_record(record)

    async def _recover_record(self, record: dict) -> None:
        record["state"] = "recovering"
        try:
            async with asyncio.timeout(20):
                await self._collect_record(record)
        except (DockerError, ManagerError, OSError, ValueError, TimeoutError):
            record["collection_status"] = "incomplete"
            record["errors"].append(
                {
                    "stage": "collection",
                    "code": "recovery_collection_failed",
                    "message": "Сбор перед аварийной очисткой не подтверждён",
                }
            )
        finally:
            async with asyncio.timeout(30):
                await self._release_record(record)

    async def recover(self) -> None:
        """Сначала сохранить доступную копию, затем убрать только собственные остатки."""
        async with asyncio.timeout(90), self.lock:
            from .recovery import discover_orphans

            unsaved = await discover_orphans(self)
            for record in unsaved:
                await self._recover_record(record)
            for path in sorted(self.root.glob("*.json")):
                record = json.loads(path.read_text())
                if record["cleanup_status"] != "success":
                    await self._recover_record(record)
