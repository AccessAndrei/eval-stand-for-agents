"""Порядок стадий одного запуска; ошибки последующих стадий независимы."""

import asyncio
import base64
import hashlib
import time

from aiohttp import ClientError

from contracts import validate
from services.common.storage import StorageBudgetExceeded
from services.gateway.clients import Client, RemoteError
from services.gateway.store import RunStore, now

IO_ERRORS = (
    StorageBudgetExceeded,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    ClientError,
    RemoteError,
    asyncio.TimeoutError,
)


class Runtime:
    def __init__(
        self,
        store: RunStore,
        manager: Client,
        harness: Client,
        proxy: Client,
        project_id: str,
        provenance: dict,
        auth: Client | None = None,
    ) -> None:
        self.store, self.manager, self.harness, self.proxy = (
            store,
            manager,
            harness,
            proxy,
        )
        self.project_id = project_id
        self.provenance = provenance
        self.auth = auth
        self.cancellations: set[str] = set()

    def result(self, launch: dict) -> dict:
        timestamp = now()
        return {
            "version": "v0",
            "run_id": launch["run_id"],
            "trace_id": launch["run_id"],
            "task_id": launch["task_id"],
            "provider_profile": launch["provider_profile"],
            "model": None,
            "provider_snapshot": None,
            "execution_status": "not_started",
            "collection_status": "not_started",
            "cleanup_status": "not_started",
            "evidence_status": "incomplete"
            if launch["task_id"] == "B5.1"
            else "not_required",
            "errors": [],
            "artifacts": [],
            "started_at": timestamp,
            "finished_at": timestamp,
            "duration_s": 0,
            "provenance": self.provenance,
            "tokens": {
                "limit": 30000,
                "actual": None,
                "unknown_reason": "not_started",
                "reserved": 0,
                "input": None,
                "output": None,
                "cached": None,
            },
        }

    @staticmethod
    def error(result: dict, stage: str, code: str) -> None:
        result["errors"].append({"stage": stage, "code": code, "message": code})

    async def prepare_profile(self, name: str) -> None:
        if name == "luna":
            if self.auth is None:
                raise RemoteError(401, "auth_error")
            reply = await self.auth.request(
                "POST", "/v0/auth/prepare", {"force_refresh": False}, 95
            )
            if reply.get("ready") is not True:
                raise RemoteError(401, "auth_error")
        await self.proxy.request("GET", "/v0/profiles/" + name)

    @staticmethod
    def apply_profile_status(result: dict, status: dict) -> None:
        candidate = {**result, "tokens": status.get("tokens", result["tokens"])}
        if status.get("provider_snapshot") is not None:
            candidate.update(
                model=status["model"], provider_snapshot=status["provider_snapshot"]
            )
        elif result.get("provider_snapshot") is None and "model" in status:
            candidate["model"] = status["model"]
        validate("result", candidate)
        result.update(candidate)

    @staticmethod
    def unknown_tokens(result: dict, reason: str) -> dict:
        snapshot = result.get("provider_snapshot") or {}
        mode = snapshot.get("budget_mode", "strict")
        output = snapshot.get("output_limit", 30000)
        return {
            "limit": 30000,
            "mode": mode,
            "output_limit": output,
            "over_target": None,
            "actual": None,
            "unknown_reason": reason,
            "reserved": output if mode == "observed_usage" else 30000,
            "input": None,
            "output": None,
            "cached": None,
        }

    async def execute(self, launch: dict, recover: bool = False) -> None:
        run_id = launch["run_id"]
        started = time.monotonic()
        result = self.result(launch)
        handle = None
        harness_started = False
        provision_attempted = recover
        stage = "provider"
        native: dict = {}
        try:
            if recover:
                result["execution_status"] = "interrupted"
                self.error(result, "execution", "interrupted")
                self.journal(result, "recovering")
                try:
                    profile = await self.proxy.request("GET", f"/v0/runs/{run_id}")
                    self.apply_profile_status(result, profile)
                    if profile.get("profile_unknown_reason"):
                        self.error(result, "recovery", "profile_metadata_unknown")
                except IO_ERRORS:
                    self.error(result, "recovery", "profile_metadata_unavailable")
                handle = (
                    await self.manager.request(
                        "GET", f"/v0/environments/by-run/{run_id}"
                    )
                )["handle"]
                harness_started = True
            else:
                profile = await self.proxy.request(
                    "POST",
                    f"/v0/runs/{run_id}",
                    {"provider_profile": launch["provider_profile"]},
                )
                self.apply_profile_status(result, profile)
                self.store.update(run_id, "provisioning")
                stage = "sandbox"
                request = {
                    "version": "v0",
                    "project_id": self.project_id,
                    "run_id": run_id,
                    "task_id": launch["task_id"],
                    "fingerprint": self.store.get(run_id)["fingerprint"],
                    "files": launch["files"],
                    "read_only_paths": launch.get("read_only_paths", []),
                    "timeout_s": launch["timeout_s"],
                }
                try:
                    provision_attempted = True
                    handle = (
                        await self.manager.request(
                            "POST", "/v0/environments", request, 60
                        )
                    )["handle"]
                except IO_ERRORS:
                    # Lookup восстанавливает ресурс; повтор provision не нужен.
                    handle = (
                        await self.manager.request(
                            "GET", f"/v0/environments/by-run/{run_id}"
                        )
                    )["handle"]
                    raise
                if run_id in self.cancellations:
                    result["execution_status"] = "cancelled"
                else:
                    stage = "execution"
                    harness_started = True
                    await self.harness.request(
                        "POST",
                        "/v0/runs",
                        {
                            "request": launch,
                            "project_id": self.project_id,
                            "handle": handle,
                            "model": profile["model"],
                            "inference_token": profile["inference_token"],
                            "proxy_url": self.proxy.url + f"/v0/runs/{run_id}",
                        },
                    )
                    self.store.update(run_id, "running")
                    native = await self.wait_harness(run_id, launch["timeout_s"] + 90)
                    result["execution_status"] = native["execution_status"]
                    if result["execution_status"] != "success":
                        self.error(result, "execution", result["execution_status"])
        except IO_ERRORS as error:
            if not recover:
                code = (
                    "provider_error"
                    if stage == "provider"
                    else "sandbox_error"
                    if stage == "sandbox"
                    else "transport_error"
                )
                if isinstance(error, asyncio.TimeoutError):
                    code = "timeout"
                elif isinstance(error, RemoteError) and (
                    error.status in {401, 403} or "auth" in error.code
                ):
                    code = "auth_error"
                result["execution_status"] = code
                self.error(result, "execution", code)
        finally:
            await self.finalize(
                launch, result, handle, harness_started, native, provision_attempted
            )
            if result["execution_status"] != "success" and not any(
                error["stage"] == "execution"
                and error["code"] == result["execution_status"]
                for error in result["errors"]
            ):
                self.error(result, "execution", result["execution_status"])
            result["duration_s"] = time.monotonic() - started
            result["finished_at"] = now()
            self.store.finish(result)
            self.cancellations.discard(run_id)

    async def wait_harness(self, run_id: str, timeout: float) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                status = await self.harness.request("GET", f"/v0/runs/{run_id}")
                if status.get("done"):
                    return status
                if run_id in self.cancellations:
                    await self.harness.request("POST", f"/v0/runs/{run_id}/cancel")
                await asyncio.sleep(0.1)

    async def finalize(
        self,
        launch: dict,
        result: dict,
        handle: str | None,
        harness_started: bool,
        native: dict,
        provision_attempted: bool,
    ) -> None:
        run_id = launch["run_id"]
        released = not harness_started
        if harness_started:
            try:
                await self.harness.request("POST", f"/v0/runs/{run_id}/cancel")
                native = native or await self.wait_harness(run_id, 90)
                released = native.get("released") is True
            except IO_ERRORS:
                released = False
        try:
            self.store.write(run_id, "events.json", self.events(launch, native))
        except (OSError, StorageBudgetExceeded):
            self.error(result, "collection", "event_storage_failed")
        if handle:
            control = {
                "version": "v0",
                "project_id": self.project_id,
                "run_id": run_id,
                "handle": handle,
            }
            self.journal(result, "collecting")
            try:
                collection = await self.manager.request(
                    "POST", f"/v0/environments/{handle}/collect", control, 20
                )
                result["collection_status"] = collection["status"]
                artifacts = self.collection(launch, native, collection)
                result["artifacts"] = artifacts["manifest"]
                if artifacts["evidence"] and artifacts["evidence"]["complete"]:
                    result["evidence_status"] = "complete"
                self.store.write(run_id, "artifacts.json", artifacts)
            except IO_ERRORS:
                result["collection_status"] = "incomplete"
            if result["collection_status"] != "complete":
                self.error(result, "collection", "collection_incomplete")
            self.journal(result, "cleaning")
            try:
                cleanup = await self.manager.request(
                    "POST", f"/v0/environments/{handle}/release", control, 30
                )
                released &= cleanup["cleanup_status"] == "success"
                for error in cleanup.get("errors", []):
                    if error not in result["errors"]:
                        result["errors"].append(error)
                    if (
                        error["stage"] == "execution"
                        and error["code"] == "transport_error"
                        and result["execution_status"] == "success"
                    ):
                        result["execution_status"] = "transport_error"
            except IO_ERRORS:
                released = False
        elif provision_attempted:
            # Потерянный provision без установленного handle не доказывает очистку.
            released = False
        result["cleanup_status"] = "success" if released else "failed"
        if not released:
            self.error(result, "cleanup", "cleanup_unconfirmed")
        try:
            proxy = await self.proxy.request("POST", f"/v0/runs/{run_id}/close")
            self.apply_profile_status(result, proxy)
            if proxy.get("error") == "budget_exceeded" and result[
                "execution_status"
            ] not in {"timeout", "cancelled", "interrupted"}:
                result["execution_status"] = "budget_exceeded"
                self.error(result, "execution", "budget_exceeded")
            if proxy.get("error") == "auth_error" and result["execution_status"] in {
                "success",
                "provider_error",
            }:
                result["execution_status"] = "auth_error"
                self.error(result, "execution", "auth_error")
            if proxy.get("error") in {
                "provider_budget_violation",
                "upstream_usage_unconfirmed",
            }:
                self.error(result, "execution", proxy["error"])
        except IO_ERRORS:
            self.error(result, "execution", "token_accounting_unavailable")
            try:
                proxy = await self.proxy.request("GET", f"/v0/runs/{run_id}")
                self.apply_profile_status(result, proxy)
            except IO_ERRORS:
                result["tokens"] = self.unknown_tokens(
                    result, "token_accounting_unavailable"
                )

    def journal(self, result: dict, state: str) -> None:
        try:
            self.store.update(result["run_id"], state)
        except (OSError, StorageBudgetExceeded):
            self.error(
                result,
                {"collecting": "collection", "recovering": "recovery"}.get(
                    state, "cleanup"
                ),
                "storage_budget_exceeded",
            )

    def collection(self, launch: dict, native: dict, collection: dict) -> dict:
        files = collection["files"]
        manifest = []
        total = 0
        for file in files:
            data = base64.b64decode(file["content_base64"], validate=True)
            total += len(data)
            if total > 1_048_576:
                raise ValueError("collection_too_large")
            manifest.append(
                {
                    "path": file["path"],
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        # Отказ по имени/манифесту относится к сбору, пока его можно завершить
        # неполным исходом; не переносим такой отказ на финальную запись результата.
        validate(
            "result",
            {
                **self.result(launch),
                "collection_status": "complete",
                "artifacts": manifest,
            },
        )
        evidence = None
        if launch["task_id"] == "B5.1":
            evidence = {
                "version": "v0",
                "codex_version": native.get("codex_version"),
                "run_id": launch["run_id"],
                "thread_id": native.get("thread_id"),
                "turn_id": native.get("turn_id"),
                "complete": native.get("evidence_complete", False),
                "protected_inputs": collection.get("protected_inputs", False),
                "input_manifest": collection.get("input_manifest", {}),
                "events": [
                    {**event, "run_id": launch["run_id"]}
                    for event in native.get("events", [])
                ],
            }
            try:
                validate("evidence", evidence)
            except ValueError:
                evidence["complete"] = False
        return {
            "files": files,
            "manifest": manifest,
            "evidence": evidence,
            "events": self.events(launch, native),
        }

    def events(self, launch: dict, native: dict) -> list[dict]:
        events = []
        traces = list(native.get("traces", []))
        if native.get("trace_truncated"):
            traces.append(
                {
                    "timestamp": now(),
                    "type": "journal_truncated",
                    "outcome": "truncated",
                    "output": "",
                    "output_truncated": True,
                }
            )
        for index, item in enumerate(traces):
            event = {
                "version": "v0",
                "run_id": launch["run_id"],
                "trace_id": launch["run_id"],
                "task_id": launch["task_id"],
                "sequence": index,
                "timestamp": item["timestamp"],
                "stage": "execution",
                "type": item["type"],
                "evidence_status": "complete"
                if native.get("evidence_complete")
                else "incomplete",
                "outcome": item["outcome"],
                "output": item["output"],
                "output_truncated": item["output_truncated"],
            }
            validate("event", event)
            events.append(event)
        return events
