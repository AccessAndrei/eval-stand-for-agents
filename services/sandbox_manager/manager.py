"""Жизненный цикл среды: запись перед побочным эффектом и отсутствие replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import ContractError, validate
from contracts.mounted import validate_mounted
from services.common.processes import stop_group
from services.common.storage import atomic_json

from .config import AGENT_UID, CODEX, OWNER, PREFIX, PYTHON, SUPERVISOR_UID, Limits
from .docker import Docker, DockerError
from .workspace_driver import ENGINE_RPC_TIMEOUT_S, WorkspaceDriver

RESOURCE_SUFFIXES = {
    "sandbox": "",
    "prepare": "-prepare",
    "collector": "-collector",
    "inputs": "-inputs",
    "workspace": "-workspace",
}
WORKSPACE_REF = re.compile(r"[a-f0-9]{32}")
MOUNTED_EXEC_WRAPPER = (
    "import os,sys;os.write(1,bytes.fromhex(sys.argv[1]));"
    "os.execv(sys.argv[2],[sys.argv[2],'exec-server','--listen','stdio'])"
)


@dataclass(frozen=True)
class MountedExecutor:
    session_id: str
    run_id: str
    process: asyncio.subprocess.Process
    timeout_s: int
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    failure: dict[str, str] = field(default_factory=dict)


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
        self.workspace_driver: WorkspaceDriver | None = None
        self.lock = asyncio.Lock()
        self.tunnels: dict[str, asyncio.subprocess.Process] = {}
        self.mounted_executors: dict[tuple[str, str], MountedExecutor] = {}
        self.helper = Path(__file__).with_name("helpers.py").read_text()
        self.closing = False
        self.persistence_failed = False
        self.unclean_workspaces: set[str] = set()
        self.transport_failures: dict[str, tuple[str, str]] = {}

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
        labels = {
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
        if workspace := record.get("workspace"):
            labels[f"{PREFIX}.workspace_ref"] = workspace["workspace_ref"]
            labels[f"{PREFIX}.workspace_mode"] = workspace["mode"]
        return labels

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
        info = await self.docker.inspect(kind, record["name"] + RESOURCE_SUFFIXES[role])
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
            if role == "workspace" and (
                self.workspace_driver is None
                or info.get("Driver") != self.workspace_driver.name
                or info.get("Options") != record["workspace"]
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
            self._require_legacy(record)
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
        code, message = self.transport_failures[record["run_id"]]
        error = {
            "stage": "execution",
            "code": code,
            "message": message,
        }
        if error not in record["errors"]:
            record["errors"].append(error)

    async def executor_exited(self, run_id: str, returncode: int) -> None:
        if returncode != 0:
            await self._executor_failure(
                run_id,
                "transport_error",
                f"exec-server завершился с кодом {returncode} во время исполнения",
            )

    async def mounted_transport_failure(
        self,
        link: MountedExecutor,
        *,
        timed_out: bool = False,
        returncode: int | None = None,
    ) -> None:
        async with self.lock:
            pair = (link.session_id, link.run_id)
            if (
                self.mounted_executors.get(pair) is not link
                or link.stop_requested.is_set()
                or link.drained.is_set()
                or link.failure
            ):
                return
            record = self._load(link.session_id)
            turn = record.get("mounted_turn") or {}
            if turn.get("run_id") != link.run_id or record["state"] != "executing":
                return
            message = "Прерван обмен с exec-server"
            if timed_out:
                message = "Истёк срок канала exec-server"
            elif returncode is not None:
                message = (
                    f"exec-server завершился с кодом {returncode} во время исполнения"
                )
            link.failure.update(
                stage="execution",
                code="timeout" if timed_out else "transport_error",
                message=message,
            )
            turn.setdefault("errors", []).append(link.failure)
            try:
                self._save(record)
            except OSError:
                pass  # Сбор по той же привязке сохраняет наблюдение из памяти.

    async def _executor_failure(self, run_id: str, code: str, message: str) -> None:
        if self._load(run_id)["state"] != "executing":
            return
        # Фиксируем наблюдение до ожидания lock: сбор/очистка уже могут его занимать.
        self.transport_failures.setdefault(run_id, (code, message))
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
        readonly = request["read_only_paths"]
        if readonly not in ([], ["input"]) or (
            readonly and request["task_id"] != "B5.1"
        ):
            raise ManagerError("unsupported_readonly_layout", 400)
        if request["task_id"] == "B5.1" and readonly != ["input"]:
            raise ManagerError("protected_inputs_required", 400)
        return self.public(await self._provision(request))

    def _mounted_provision_request(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        self._validate_workspace("manager_provision", request)
        if not WORKSPACE_REF.fullmatch(request["workspace_ref"]):
            raise ManagerError("invalid_workspace_request", 400)
        self._validate_session_identity(request)
        workspace = {key: request[key] for key in ("mode", "workspace_ref")}
        fingerprint = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return (
            {**request, "run_id": request["session_id"], "fingerprint": fingerprint},
            workspace,
        )

    async def provision_mounted(self, request: dict[str, Any]) -> dict:
        normalized, workspace = self._mounted_provision_request(request)
        if self.workspace_driver is None:
            raise ManagerError("workspace_driver_unavailable", 503)
        record = await self._provision(normalized, workspace=workspace)
        return self._mounted_status(record)

    @staticmethod
    def _validate_session_identity(request: dict[str, Any]) -> None:
        if not isinstance(request["session_id"], str) or not WORKSPACE_REF.fullmatch(
            request["session_id"]
        ):
            raise ManagerError("invalid_workspace_request", 400)
        try:
            validate(
                "lookup",
                {
                    "version": "v0",
                    "project_id": request["project_id"],
                    "run_id": request["session_id"],
                },
            )
        except ContractError as error:
            raise ManagerError("invalid_workspace_request", 400) from error

    @staticmethod
    def _validate_workspace(kind: str, request: dict[str, Any]) -> None:
        try:
            validate_mounted(kind, request)
        except ContractError as error:
            raise ManagerError("invalid_workspace_request", 400) from error

    @staticmethod
    def _require_legacy(record: dict) -> None:
        if record.get("workspace"):
            raise ManagerError("mounted_contract_required")

    def _mounted_status(self, record: dict) -> dict:
        status = self.public(record)
        status["session_id"] = status.pop("run_id")
        status["version"] = "v1"
        if turn := record.get("mounted_turn"):
            status["run_id"] = turn["run_id"]
            status["errors"] = [*status["errors"], *turn.get("errors", [])]
        return status

    def _archive_path(self, record: dict, run_id: str) -> Path:
        return self.root / f"{record['run_id']}-{run_id}.mounted-turn"

    def _archived_turn(self, record: dict, run_id: str) -> dict | None:
        path = self._archive_path(record, run_id)
        if not path.exists():
            return None
        receipt = json.loads(path.read_text())
        status = receipt["status"]
        if (status["session_id"], status["run_id"], status["handle"]) != (
            record["run_id"],
            run_id,
            record["handle"],
        ):
            raise ManagerError("workspace_run_receipt_mismatch", 503)
        return receipt

    def _archive_turn(self, record: dict, previous: dict) -> None:
        receipt = {
            "status": self._mounted_status(record),
            "timeout_s": previous["timeout_s"],
            "collection": previous["collection"],
        }
        existing = self._archived_turn(record, previous["run_id"])
        if existing is not None:
            if existing != receipt:
                raise ManagerError("workspace_run_receipt_mismatch", 503)
            return
        try:
            atomic_json(self._archive_path(record, previous["run_id"]), receipt)
        except OSError:
            self.persistence_failed = True
            raise

    async def lookup_mounted(self, session_id: str, project: str) -> dict:
        self._validate_session_identity(
            {"session_id": session_id, "project_id": project}
        )
        self._check_project(project)
        async with self.lock:
            record = self._load(session_id)
            if not record.get("workspace"):
                raise ManagerError("unknown_workspace", 404)
            status = self._mounted_status(record)
            if record["state"] in {"ready", "executing", "quiescing", "collected"}:
                if not await self._mounted_ready(record):
                    status["state"] = "failed"
                    status["errors"] = [
                        *status["errors"],
                        {
                            "stage": "provision",
                            "code": "workspace_mount_lost",
                            "message": "Готовность /work не подтверждена",
                        },
                    ]
            return status

    async def fence_mounted_release(
        self, request: dict[str, Any], session_id: str
    ) -> dict:
        normalized, workspace = self._mounted_provision_request(request)
        if normalized["session_id"] != session_id:
            raise ManagerError("invalid_workspace_request", 400)
        self._check_project(normalized["project_id"])
        async with self.lock:
            if self._path(session_id).exists():
                record = self._load(session_id)
                if (
                    record["request"] != normalized
                    or record.get("workspace") != workspace
                ):
                    raise ManagerError("run_conflict")
                if record["cleanup_status"] != "success":
                    await self._release_record(record)
            else:
                record = self._new_record(normalized)
                for role, suffix in RESOURCE_SUFFIXES.items():
                    kind = "volume" if role in {"inputs", "workspace"} else "container"
                    if (
                        await self.docker.inspect(kind, record["name"] + suffix)
                        is not None
                    ):
                        raise ManagerError("workspace_cleanup_unconfirmed", 409)
                # Барьер сохраняется до ответа. Запоздавший provision читает
                # ту же запись под тем же lock и не создаёт окружение.
                record.update(
                    workspace=workspace, state="released", cleanup_status="success"
                )
                self._save(record)
            return self._mounted_status(record)

    def _mounted_run_record(
        self, handle: str, session_id: str, run_id: str, project: str
    ) -> dict:
        record = self._by_handle(handle, session_id, project)
        if not record.get("workspace"):
            raise ManagerError("unknown_workspace", 404)
        if (record.get("mounted_turn") or {}).get("run_id") != run_id:
            raise ManagerError("unknown_workspace_run", 404)
        return record

    def _mounted_run_request(
        self, request: dict[str, Any], handle: str, *, reserve: bool = False
    ) -> None:
        keys = {"version", "project_id", "session_id", "handle", "run_id"}
        if reserve:
            keys.add("timeout_s")
        self._validate_workspace("manager_turn", request)
        if (
            request.keys() != keys
            or request["handle"] != handle
            or not WORKSPACE_REF.fullmatch(request["run_id"])
        ):
            raise ManagerError("invalid_workspace_request", 400)
        self._validate_session_identity(request)

    async def reserve_mounted_run(self, request: dict[str, Any], handle: str) -> dict:
        self._mounted_run_request(request, handle, reserve=True)
        async with self.lock:
            record = self._by_handle(
                handle, request["session_id"], request["project_id"]
            )
            if not record.get("workspace"):
                raise ManagerError("unknown_workspace", 404)
            run_id, timeout = request["run_id"], request["timeout_s"]
            previous = record.get("mounted_turn")
            if archived := self._archived_turn(record, run_id):
                if archived["timeout_s"] != timeout:
                    raise ManagerError("workspace_run_conflict")
                return archived["status"]
            if previous and previous["run_id"] == run_id:
                if previous["timeout_s"] != timeout:
                    raise ManagerError("workspace_run_conflict")
                return self._mounted_status(record)
            if self.closing or record["state"] in {"failed", "released"}:
                raise ManagerError("execution_cannot_replay")
            if self.persistence_failed:
                raise ManagerError("storage_budget_exceeded", 507)
            if previous is not None:
                collection = previous.get("collection", {})
                if (
                    record["state"] != "collected"
                    or collection.get("collection_status") != "complete"
                    or collection.get("writers_stopped") is not True
                    or (record["run_id"], previous["run_id"]) in self.mounted_executors
                ):
                    raise ManagerError("busy")
            elif record["state"] != "ready":
                raise ManagerError("execution_cannot_replay")
            if previous is not None:
                # Сохраняем старый итог до замены текущего запуска. Повтор после
                # прерывания записи возвращает ту же квитанцию без нового exec.
                self._archive_turn(record, previous)
            if not await self._mounted_ready(record):
                await self._fail_mounted(record, "workspace_mount_lost")
                raise ManagerError("workspace_mount_lost", 502)
            record.update(
                state="ready",
                collection_status="not_started",
                execution_started=False,
                mounted_turn={"run_id": run_id, "timeout_s": timeout},
            )
            self._save(record)
            return self._mounted_status(record)

    def _mounted_collect_record(
        self, request: dict, handle: str
    ) -> tuple[dict, dict | None]:
        record = self._by_handle(handle, request["session_id"], request["project_id"])
        if not record.get("workspace"):
            raise ManagerError("unknown_workspace", 404)
        turn = record.get("mounted_turn") or {}
        if turn.get("run_id") == request["run_id"]:
            return record, turn.get("collection")
        if archived := self._archived_turn(record, request["run_id"]):
            return record, archived["collection"]
        raise ManagerError("unknown_workspace_run", 404)

    async def collect_mounted_run(self, request: dict[str, Any], handle: str) -> dict:
        self._mounted_run_request(request, handle)
        pair = (request["session_id"], request["run_id"])
        async with self.lock:
            record, cached = self._mounted_collect_record(request, handle)
            if cached is not None:
                return cached
            if record["state"] not in {"ready", "executing", "quiescing"}:
                raise ManagerError("workspace_run_not_collectable")
            record["state"] = "quiescing"
            try:
                self._save(record)
            except OSError:
                pass  # При сбое журнала остановка писателей всё равно нужна.
            link = self.mounted_executors.get(pair)
            if link is not None:
                link.stop_requested.set()

        # CONNECT подтверждает завершение канала через callback, который сам
        # берёт lock менеджера. Нельзя ждать этот callback внутри lock.
        if link is not None and link.entered.is_set():
            try:
                async with asyncio.timeout(10):
                    await link.drained.wait()
            except TimeoutError:
                pass  # Подтверждение проверяется под lock перед сбором.

        async with self.lock:
            record, cached = self._mounted_collect_record(request, handle)
            if cached is not None:
                return cached
            if record["state"] not in {"ready", "executing", "quiescing"}:
                raise ManagerError("workspace_run_not_collectable")
            turn = record["mounted_turn"]
            errors = turn.setdefault("errors", [])
            if link is not None and link.failure and link.failure not in errors:
                errors.append(link.failure)
            try:
                async with asyncio.timeout(34):
                    if record["execution_started"]:
                        if (
                            link is None
                            or not link.entered.is_set()
                            or not link.drained.is_set()
                            or self.mounted_executors.get(pair) is not link
                        ):
                            raise DockerError("mounted_executor_unconfirmed")
                        await self._quiesce_mounted(record)
                    elif not await self._mounted_ready(record):
                        raise ManagerError("workspace_mount_lost", 502)
            except (DockerError, ManagerError, OSError, TimeoutError) as error:
                record.update(state="failed", collection_status="incomplete")
                code = (
                    "workspace_mount_lost"
                    if isinstance(error, ManagerError)
                    and error.code == "workspace_mount_lost"
                    else "writers_unconfirmed"
                )
                errors.append(
                    {
                        "stage": "collection",
                        "code": code,
                        "message": "Не подтверждена готовность /work"
                        if code == "workspace_mount_lost"
                        else "Остановка процессов записи не подтверждена",
                    }
                )
            else:
                record.update(state="collected", collection_status="complete")
                self.mounted_executors.pop(pair, None)
            collection = {
                **{
                    key: request[key]
                    for key in ("version", "project_id", "session_id", "run_id")
                },
                "collection_status": record["collection_status"],
                "writers_stopped": record["collection_status"] == "complete",
                "execution_started": record["execution_started"],
                "errors": [*record["errors"], *errors],
            }
            turn["collection"] = collection
            try:
                self._save(record)
            finally:
                if not collection["writers_stopped"]:
                    await self._release_record(record, terminal_state="failed")
            return collection

    async def release_mounted(self, request: dict[str, Any], handle: str) -> dict:
        self._validate_workspace("manager_release", request)
        if request["handle"] != handle:
            raise ManagerError("invalid_workspace_request", 400)
        self._validate_session_identity(request)
        async with self.lock:
            record = self._by_handle(
                handle, request["session_id"], request["project_id"]
            )
            if not record.get("workspace"):
                raise ManagerError("unknown_workspace", 404)
            await self._release_record(record)
            return self._mounted_status(record)

    async def _provision(
        self, request: dict[str, Any], *, workspace: dict[str, str] | None = None
    ) -> dict:
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
                if (
                    workspace
                    and record["state"] == "ready"
                    and not await self._mounted_ready(record)
                ):
                    await self._fail_mounted(record, "workspace_mount_lost")
                return record
            for path in self.root.glob("*.json"):
                previous = json.loads(path.read_text())
                if previous["cleanup_status"] != "success":
                    raise ManagerError("busy")
            record = self._new_record(request)
            if workspace is not None:
                record["workspace"] = workspace
            self._save(record)
            try:
                await self._create(record)
                record["state"] = "ready"
                self._save(record)
            except (DockerError, ManagerError, OSError, ValueError) as error:
                if workspace:
                    await self._fail_mounted(record, "provision_failed")
                    raise ManagerError("provision_failed", 502) from error
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
            return record

    async def _fail_mounted(self, record: dict, code: str) -> None:
        record["state"] = "failed"
        record["errors"].append(
            {
                "stage": "provision",
                "code": code,
                "message": "Готовность /work не подтверждена",
            }
        )
        try:
            self._save(record)
        finally:
            await self._release_record(record, terminal_state="failed")

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
        if record.get("workspace"):
            await self._create_mounted(record)
            return
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
        await self._start_container(record, mounts)
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

    async def _create_mounted(self, record: dict) -> None:
        if self.workspace_driver is None:
            raise DockerError("workspace_driver_unavailable")
        workspace = record["workspace"]
        volume_name = record["name"] + RESOURCE_SUFFIXES["workspace"]
        await self.docker.command(
            [
                "volume",
                "create",
                "--driver",
                self.workspace_driver.name,
                *self._label_args(record, "workspace"),
                "--opt",
                f"workspace_ref={workspace['workspace_ref']}",
                "--opt",
                f"mode={workspace['mode']}",
                volume_name,
            ],
        )
        if await self._owned(record, "workspace", "volume") is None:
            raise DockerError("workspace_volume_missing")
        readonly = ",readonly" if workspace["mode"] == "ro" else ""
        await self._start_container(
            record,
            [
                "--mount",
                f"type=volume,src={volume_name},dst=/work,volume-nocopy{readonly},"
                f"volume-driver={self.workspace_driver.name}",
            ],
        )
        if not await self._mounted_ready(record):
            raise DockerError("workspace_mount_unconfirmed")
        sandbox = await self._owned(record, "sandbox")
        if sandbox is None:
            raise DockerError("sandbox_missing")
        record["mounted_sandbox_id"] = sandbox["Id"]

    async def _mounted_ready(self, record: dict) -> bool:
        volume = await self._owned(record, "workspace", "volume")
        sandbox = await self._owned(record, "sandbox")
        if not (
            volume
            and (volume.get("Status") or {}).get("Ready") is True
            and sandbox
            and (sandbox.get("State") or {}).get("Running") is True
            and (sandbox.get("State") or {}).get("Paused") is False
            and sandbox.get("Id") == record.get("mounted_sandbox_id", sandbox.get("Id"))
        ):
            return False
        return any(
            mount.get("Destination") == "/work"
            and mount.get("Name") == record["name"] + RESOURCE_SUFFIXES["workspace"]
            and mount.get("RW") is (record["workspace"]["mode"] == "rw")
            for mount in sandbox["Mounts"]
        )

    async def _start_container(self, record: dict, mounts: list[str]) -> None:
        await self.docker.command(
            [
                "create",
                "--pull=never",
                "--name",
                record["name"],
                *self._label_args(record, "sandbox"),
                *self.limits.docker_args(mounted=bool(record.get("workspace"))),
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
        await self.docker.command(
            ["start", record["name"]],
            timeout=ENGINE_RPC_TIMEOUT_S + 5 if record.get("workspace") else None,
        )

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
            self._require_legacy(record)
            if (
                self.closing
                or record["state"] != "ready"
                or record["execution_started"]
            ):
                raise ManagerError("execution_cannot_replay")
            if await self._owned(record, "sandbox") is None:
                raise ManagerError("sandbox_missing", 502)
            return await self._open_executor(record)

    async def connect_mounted(
        self, handle: str, run_id: str, project: str, session_id: str
    ) -> MountedExecutor:
        async with self.lock:
            record = self._mounted_run_record(handle, session_id, run_id, project)
            if self.persistence_failed:
                raise ManagerError("storage_budget_exceeded", 507)
            if (
                self.closing
                or record["state"] != "ready"
                or record["execution_started"]
            ):
                raise ManagerError("execution_cannot_replay")
            if not await self._mounted_ready(record):
                raise ManagerError("workspace_mount_lost", 502)
            record.update(state="executing", execution_started=True)
            self._save(record)
            marker = secrets.token_bytes(24)
            process = await self._spawn_executor(
                record,
                [PYTHON, "-I", "-S", "-c", MOUNTED_EXEC_WRAPPER, marker.hex(), CODEX],
            )
            link = MountedExecutor(
                session_id, run_id, process, record["mounted_turn"]["timeout_s"]
            )
            self.mounted_executors[(session_id, run_id)] = link
            assert process.stdout is not None
            try:
                async with asyncio.timeout(8):
                    if await process.stdout.readexactly(len(marker)) != marker:
                        raise ManagerError("executor_entry_unconfirmed", 502)
            except (TimeoutError, asyncio.IncompleteReadError, ManagerError) as error:
                link.stop_requested.set()
                raise ManagerError("executor_entry_unconfirmed", 502) from error
            link.entered.set()
            return link

    async def _open_executor(self, record: dict) -> asyncio.subprocess.Process:
        record.update(state="executing", execution_started=True)
        self._save(record)
        process = await self._spawn_executor(
            record, [CODEX, "exec-server", "--listen", "stdio"]
        )
        self.tunnels[record["handle"]] = process
        return process

    async def _spawn_executor(
        self, record: dict, command: list[str]
    ) -> asyncio.subprocess.Process:
        return await self.docker.spawn(
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
                *command,
            ]
        )

    async def _stop_executor(self, handle: str) -> None:
        if process := self.tunnels.get(handle):
            if not await stop_group(process):
                raise DockerError("relay_remains")
            self.tunnels.pop(handle, None)

    async def close_mounted_executor(self, link: MountedExecutor) -> None:
        async with self.lock:
            if (
                self.mounted_executors.get((link.session_id, link.run_id)) is link
                and not link.drained.is_set()
            ):
                if not await stop_group(link.process, timeout=3):
                    raise DockerError("relay_remains")
                link.drained.set()

    async def _quiesce_mounted(self, record: dict) -> None:
        if "mounted_sandbox_id" not in record or not await self._mounted_ready(record):
            raise ManagerError("workspace_mount_lost", 502)
        engine_id = record["mounted_sandbox_id"]
        try:
            await self.docker.command(["pause", engine_id], timeout=8)
            sandbox = await self._owned(record, "sandbox")
            if (
                sandbox is None
                or sandbox["Id"] != engine_id
                or sandbox["State"].get("Paused") is not True
            ):
                raise DockerError("sandbox_pause_unconfirmed")
            await self._trusted_collection(record, "kill_frozen")
        finally:
            current = await self._owned(record, "sandbox")
            if (
                current
                and current["Id"] == engine_id
                and current["State"].get("Paused") is True
            ):
                await self.docker.command(["unpause", engine_id], timeout=8)
        if not await self._mounted_ready(record):
            raise ManagerError("workspace_mount_lost", 502)
        await self._trusted_collection(record, "verify_quiescent")

    async def collect(self, request: dict) -> dict:
        validate("collect", request)
        async with self.lock:
            record = self._by_handle(
                request["handle"], request["run_id"], request["project_id"]
            )
            self._require_legacy(record)
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
            self._require_legacy(record)
            await self._release_record(record)
            return self.public(record)

    async def _release_record(
        self, record: dict, *, terminal_state: str = "released"
    ) -> None:
        turn = record.get("mounted_turn")
        pair = (record["run_id"], turn["run_id"]) if turn is not None else None
        link = self.mounted_executors.get(pair) if pair is not None else None
        if link is not None:
            link.stop_requested.set()
        try:
            for role in ("collector", "sandbox", "prepare"):
                info = await self._owned(record, role)
                if info:
                    await self.docker.command(["rm", "-f", info["Id"]])
                    if await self._owned(record, role) is not None:
                        raise DockerError("resource_remains")
            volume_role = "workspace" if record.get("workspace") else "inputs"
            if await self._owned(record, volume_role, "volume"):
                await self.docker.command(
                    ["volume", "rm", record["name"] + RESOURCE_SUFFIXES[volume_role]]
                )
                if await self._owned(record, volume_role, "volume") is not None:
                    raise DockerError("volume_remains")
            if link is not None:
                if not link.drained.is_set() and not await stop_group(
                    link.process, timeout=3
                ):
                    raise DockerError("relay_remains")
                self.mounted_executors.pop(pair, None)
            else:
                await self._stop_executor(record["handle"])
            record.update(state=terminal_state, cleanup_status="success")
        except (DockerError, OSError):
            record["cleanup_status"] = "failed"
            record["errors"].append(
                {
                    "stage": "cleanup",
                    "code": "cleanup_failed",
                    "message": "Удаление ресурсов не подтверждено",
                }
            )
        if record.get("workspace"):
            if record["cleanup_status"] == "success":
                self.unclean_workspaces.discard(record["run_id"])
            else:
                self.unclean_workspaces.add(record["run_id"])
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
            self._require_legacy(record)
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
            if self.workspace_driver is not None:
                await self.workspace_driver.stop()

    async def _recover_record(self, record: dict) -> None:
        if record.get("workspace"):
            async with asyncio.timeout(30):
                await self._release_record(record)
            return
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
