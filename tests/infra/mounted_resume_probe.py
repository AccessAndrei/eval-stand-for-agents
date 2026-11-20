"""Проверка продолжения смонтированного чата на запущенном mock-стеке."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import runpy
import secrets
import subprocess
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from client.session import ClientError, MountedClient, MountedSession
from scripts.configure_local import assignments
from services.sandbox_manager.config import OWNER, PREFIX

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "docker-compose.yml",
    "docker/compose.mock.yml",
    "docker/compose.single-cpu.yml",
    "docker/compose.resume-probe.yml",
)
MOCK = Path(__file__).with_name("mock-scripts") / "resume_probe.py"

# Устаревшее полномочие остаётся в tmpfs Gateway. На хост уходят только
# безопасные статусы. У операций нет доступа к внешнему API.
TRUSTED = r"""
import asyncio
import json
import os
import re
import secrets
import sys
from pathlib import Path

import aiohttp

action, session, run_a, run_b = sys.argv[1:]
assert all(re.fullmatch(r"[0-9a-f]{32}", item) for item in (session, run_a, run_b))
slot = Path("/tmp") / ("eval-resume-grant-" + run_a)


def receipt(run):
    value = json.loads((Path(os.environ["GATEWAY_STATE"]) / ".mounted-runs" /
                        run / "receipt.json").read_text())
    assert value["run_id"] == run and value["session_id"] == session
    assert value["profile"] == "mock"
    return value


async def old_connect(handle):
    manager = "sandbox-manager"
    async with asyncio.timeout(8):
        reader, writer = await asyncio.open_connection(manager, 8082)
        try:
            writer.write((
                f"CONNECT /v1/environments/{handle} HTTP/1.1\r\n"
                f"Host: {manager}\r\n"
                f"Authorization: Bearer {os.environ['MANAGER_TOKEN']}\r\n"
                f"X-Project-ID: {os.environ['PROJECT_ID']}\r\n"
                f"X-Session-ID: {session}\r\nX-Run-ID: {run_a}\r\n\r\n"
            ).encode())
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            return int(header.split(b" ", 2)[1])
        finally:
            writer.close()
            await writer.wait_closed()


async def strict_budget(http):
    ref = secrets.token_hex(24)
    session_id, run_id = secrets.token_hex(16), secrets.token_hex(16)
    base = os.environ["PROXY_URL"] + "/v1/runs/" + ref
    management = {"Authorization": "Bearer " + os.environ["PROXY_TOKEN"]}
    mock_stats = "http://mock-provider-alt:8090/stats/" + ref

    async def received():
        async with http.get(mock_stats, allow_redirects=False) as reply:
            reply.raise_for_status()
            return (await reply.json())["requests_received"]

    def request(size):
        return {
            "model": "mock-codex-alt", "stream": True, "input": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "x" * size}],
            }],
        }

    opening = {
        "version": "v1", "session_id": session_id, "run_id": run_id,
        "provider_profile": "mock-alt",
    }
    try:
        async with http.post(base, json=opening, headers=management, allow_redirects=False) as reply:
            reply.raise_for_status()
            grant = await reply.json()
        assert grant["tokens"]["mode"] == "strict" and grant["tokens"]["limit"] == 30_000
        assert grant["tokens"]["actual"] == 0 and grant["closed"] is False
        inference = {"Authorization": "Bearer " + grant["inference_token"]}
        async with http.post(
            base + "/responses", json=request(56_000),
            headers=inference, allow_redirects=False,
        ) as reply:
            assert reply.status == 200 and b"response.completed" in await reply.read()
        async with http.get(base, headers=management, allow_redirects=False) as reply:
            reply.raise_for_status()
            used = (await reply.json())["tokens"]
        assert 0 < used["actual"] < 30_000 and used["reserved"] == 0
        before = await received()
        async with http.post(
            base + "/responses", json=request(67_000),
            headers=inference, allow_redirects=False,
        ) as reply:
            assert reply.status == 429 and (await reply.json())["error"] == "budget_exceeded"
        after = await received()
        assert before == after == 1
    finally:
        async with http.post(base + "/close", headers=management, allow_redirects=False) as reply:
            reply.raise_for_status()
            closed = await reply.json()
        assert closed["closed"] is True
    assert closed["tokens"]["actual"] == used["actual"]
    return {
        "status": 429, "error": "budget_exceeded", "actual": used["actual"],
        "upstream_requests": after, "closed": True,
    }




async def main():
    if action == "forget":
        slot.unlink(missing_ok=True)
        return {"forgotten": not slot.exists()}
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=12)
    ) as http:
        if action == "budget":
            return await strict_budget(http)
        if action in {"status", "release"}:
            method, path = (
                ("GET", "probe-status") if action == "status" else ("POST", "probe-finish")
            )
            async with http.request(
                method, "http://mock-provider:8090/" + path, allow_redirects=False
            ) as reply:
                reply.raise_for_status()
                return await reply.json()
        first = receipt(run_a)
        base = os.environ["PROXY_URL"] + "/v1/runs/"
        reference_a = first["proxy_ref"]
        if action == "capture":
            body = {
                "version": "v1", "session_id": session, "run_id": run_a,
                "provider_profile": "mock",
            }
            async with http.post(
                base + reference_a, json=body, allow_redirects=False,
                headers={"Authorization": "Bearer " + os.environ["PROXY_TOKEN"]},
            ) as reply:
                grant = await reply.json()
                assert reply.status == 200 and grant["closed"] is False
                assert grant["session_id"] == session and grant["run_id"] == run_a
            with os.fdopen(os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as saved:
                saved.write(grant["inference_token"])
            return {"captured": True}
        assert action == "stale"
        second = receipt(run_b)
        try:
            grant = slot.read_text()
        finally:
            slot.unlink(missing_ok=True)
        statuses = []
        for reference in (reference_a, second["proxy_ref"]):
            async with http.post(
                base + reference + "/responses",
                json={"model": "mock-codex", "input": [], "stream": True},
                headers={"Authorization": "Bearer " + grant}, allow_redirects=False,
            ) as reply:
                statuses.append(reply.status)
        return {"proxy_status": statuses, "connect_status": await old_connect(first["handle"])}


try:
    print(json.dumps(asyncio.run(main())))
except Exception as error:
    print(json.dumps({"error": type(error).__name__}))
    raise SystemExit(2)
"""


class ProbeError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeError(code)


def command(parts: list[str]) -> bytes:
    reply = subprocess.run(
        parts, cwd=ROOT, capture_output=True, timeout=30, check=False
    )
    require(reply.returncode == 0, "docker_probe_command_failed")
    return reply.stdout


def resources(project: str, session: str) -> tuple[list[bytes], list[bytes]]:
    labels = (
        f"{PREFIX}.owner={OWNER}",
        f"{PREFIX}.project={project}",
        f"{PREFIX}.run={session}",
    )
    filters = [word for label in labels for word in ("--filter", "label=" + label)]
    return tuple(
        command(
            ["docker", *arguments, *filters, "--filter", f"label={PREFIX}.role={role}"]
        ).splitlines()
        for arguments, role in (
            (["ps", "-aq", "--no-trunc"], "sandbox"),
            (["volume", "ls", "-q"], "workspace"),
        )
    )


class ResumeProbe:
    def __init__(
        self, project: str, gateway: str, env_file: Path, context: str
    ) -> None:
        self.project = project
        self.gateway = gateway
        self.context = context
        self.runs = (secrets.token_hex(16), secrets.token_hex(16))
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            str(env_file.resolve()),
            "--project-name",
            project,
            *(word for file in FILES for word in ("-f", file)),
        ]
        self.fixture = runpy.run_path(str(MOCK))

    async def control(self, action: str, session: MountedSession) -> dict[str, Any]:
        result = await asyncio.to_thread(
            command,
            [
                *self.compose,
                "exec",
                "-T",
                "gateway",
                "/opt/venv/bin/python",
                "-c",
                TRUSTED,
                action,
                session.session_id,
                *self.runs,
            ],
        )
        return json.loads(result)

    async def wait_mock(self, session: MountedSession, field: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(45):
                while True:
                    value = await self.control("status", session)
                    if value[field] is True:
                        return value
                    await asyncio.sleep(0.15)
        except TimeoutError as error:
            raise ProbeError(f"mock_{field}_unconfirmed") from error

    async def result(self, session: MountedSession, run: str) -> dict[str, Any]:
        results = [
            event["result"]
            async for event in session.events(run, timeout=170)
            if event["type"] == "result"
        ]
        require(
            len(results) == 1
            and results[0]["run_id"] == run
            and results[0]["session_id"] == session.session_id,
            "terminal_result_identity",
        )
        return results[0]

    async def run(self, session: MountedSession, folder: Path) -> dict[str, Any]:
        identity = await asyncio.to_thread(resources, self.project, session.session_id)
        require(len(identity[0]) == len(identity[1]) == 1, "initial_mount_identity")
        run_a, run_b = self.runs
        await session.start_run(
            self.fixture["A_PROMPT"] + " " + self.context,
            timeout_s=150,
            run_id=run_a,
        )
        await self.wait_mock(session, "old_held")
        require(
            (await self.control("capture", session))["captured"],
            "active_grant_unconfirmed",
        )
        heartbeat = folder / self.fixture["HEARTBEAT"]
        try:
            async with asyncio.timeout(10):
                while not heartbeat.exists():
                    await asyncio.sleep(0.15)
                before = heartbeat.read_bytes()
                while heartbeat.read_bytes() == before:
                    await asyncio.sleep(0.15)
        except (FileNotFoundError, TimeoutError) as error:
            raise ProbeError("live_writer_heartbeat_unconfirmed") from error
        try:
            await session.start_run(
                self.fixture["B_PROMPT"], timeout_s=100, run_id=run_b
            )
        except ClientError as error:
            require(error.http_status == 409, "early_second_turn_status")
        else:
            raise ProbeError("second_turn_accepted_early")
        await session.cancel_run(run_a)
        cancelled = await self.result(session, run_a)
        require(
            cancelled["execution_status"] == "cancelled"
            and all(
                cancelled[field] is True
                for field in (
                    "continuation_ready",
                    "proxy_closed",
                    "writers_stopped",
                    "harness_released",
                )
            )
            and cancelled["tokens"]["actual"] is None,
            "cancel_barriers_unconfirmed",
        )
        await self.wait_mock(session, "old_closed")
        frozen = heartbeat.read_bytes()
        await session.start_run(self.fixture["B_PROMPT"], timeout_s=100, run_id=run_b)
        held = await self.wait_mock(session, "new_held")
        require(
            held["context_retained"] and held["resumed_output"], "old_context_missing"
        )
        stale = await self.control("stale", session)
        require(
            stale["proxy_status"][0] in (401, 429)
            and stale["proxy_status"][1] == 401
            and stale["connect_status"] == 404,
            "old_turn_authority_still_active",
        )
        counters = await self.control("status", session)
        require(
            all(held[key] == counters[key] for key in ("old_requests", "new_requests")),
            "stale_request_reached_provider",
        )
        require(
            (await self.control("release", session))["released"],
            "mock_release_unconfirmed",
        )
        resumed = await self.result(session, run_b)
        tokens = resumed["tokens"]
        require(
            resumed["execution_status"] == "success"
            and resumed["evidence"]["thread_id"] == cancelled["evidence"]["thread_id"]
            and resumed["evidence"]["turn_id"] != cancelled["evidence"]["turn_id"]
            and tokens["input"] > 0
            and tokens["output"] > 0
            and tokens["actual"] == tokens["input"] + tokens["output"]
            and (folder / self.fixture["B_FILE"]).read_text(encoding="utf-8")
            == self.fixture["VALUE"]
            and heartbeat.read_bytes() == frozen,
            "resumed_turn_unconfirmed",
        )
        require(
            await asyncio.to_thread(resources, self.project, session.session_id)
            == identity,
            "mounted_resource_changed",
        )
        require(
            (await session.run_status(run_a))["result"] == cancelled,
            "old_terminal_result_changed",
        )
        return {
            "session_id": session.session_id,
            "run_a": run_a,
            "run_b": run_b,
            "codex_thread_id": resumed["evidence"]["thread_id"],
            "turn_a": cancelled["evidence"]["turn_id"],
            "turn_b": resumed["evidence"]["turn_id"],
            "old_usage": cancelled["tokens"]["actual"],
            "new_input": tokens["input"],
            "new_output": tokens["output"],
            **stale,
        }

    async def execute(self, token: str) -> dict[str, Any]:
        published = await asyncio.to_thread(
            command, [*self.compose, "port", "gateway", "8080"]
        )
        endpoint = urlsplit(self.gateway)
        require(
            int(published.decode().strip().rsplit(":", 1)[1]) == endpoint.port,
            "wrong_compose_project_gateway",
        )
        with tempfile.TemporaryDirectory(prefix="eval-resume-probe-") as directory:
            folder = Path(directory)
            for name in ("PARENT", "CHILD"):
                (folder / self.fixture[name + "_NAME"]).write_text(
                    self.fixture[name], encoding="utf-8"
                )
            async with MountedClient(self.gateway, token, timeout=95) as client:
                session = client.session(folder, profile="mock", mode="rw")
                try:
                    require(
                        (await session.mount())["state"] == "ready",
                        "workspace_mount_unconfirmed",
                    )
                    result = await self.run(session, folder)
                    result["strict_budget"] = await self.control("budget", session)
                finally:
                    try:
                        require(
                            (await self.control("forget", session))["forgotten"],
                            "tmpfs_cleanup_unconfirmed",
                        )
                    finally:
                        close = await session.close()
                require(close.ok, "session_cleanup_unconfirmed")
                require(
                    await asyncio.to_thread(resources, self.project, session.session_id)
                    == ([], []),
                    "workspace_resources_left",
                )
        return {**result, "cleanup": "success", "passed": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        address = urlsplit(args.gateway)
        require(
            address.hostname == "localhost" or ip_address(address.hostname).is_loopback,
            "gateway_must_be_loopback",
        )
        settings = {**assignments(args.env_file.read_text().splitlines()), **os.environ}
        require(settings.get("PROJECT_ID") == args.project, "project_label_mismatch")
        context = settings.get("PROBE_CONTEXT", "")
        require(
            re.fullmatch(r"context-[0-9a-f]{32,64}", context) is not None,
            "invalid_probe_context",
        )
        require(bool(settings.get("CLIENT_TOKEN")), "client_token_required")
        report = asyncio.run(
            ResumeProbe(args.project, args.gateway, args.env_file, context).execute(
                settings["CLIENT_TOKEN"]
            )
        )
    except KeyboardInterrupt:
        report = {"passed": False, "error": "interrupted"}
    except (ProbeError, ClientError) as error:
        report = {"passed": False, "error": str(error)}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        report = {"passed": False, "error": type(error).__name__}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
