"""Проверка SSHFS на запущенном mock-стеке с записью route.json."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from client.session import ClientError, MountedClient, MountedSession
from services.sandbox_manager.config import AGENT_UID, OWNER, PREFIX

MARKER = b"from-client-\xf1\xe1\r\n"
UPDATED_MARKER = b"client-edited-\x00\xfe\n"
SANDBOX_BYTES = b"from-sandbox-\x00\xff\n"


class ProbeError(RuntimeError):
    pass


def _require(condition: bool, name: str) -> None:
    if not condition:
        raise ProbeError(name)


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    reply = subprocess.run(
        ["docker", *arguments], capture_output=True, timeout=20, check=False
    )
    if check:
        _require(reply.returncode == 0, f"docker_{arguments[0]}_failed")
    return reply


def _labels(project: str, session_id: str, role: str) -> dict[str, str]:
    return {
        f"{PREFIX}.owner": OWNER,
        f"{PREFIX}.project": project,
        f"{PREFIX}.run": session_id,
        f"{PREFIX}.role": role,
    }


def _filters(project: str, session_id: str, role: str) -> list[str]:
    return [
        part
        for name, value in _labels(project, session_id, role).items()
        for part in ("--filter", f"label={name}={value}")
    ]


def _resources(project: str, session_id: str) -> tuple[list[str], list[str]]:
    containers = (
        _docker("ps", "-aq", "--no-trunc", *_filters(project, session_id, "sandbox"))
        .stdout.decode()
        .splitlines()
    )
    volumes = (
        _docker("volume", "ls", "-q", *_filters(project, session_id, "workspace"))
        .stdout.decode()
        .splitlines()
    )
    return containers, volumes


def _agent(
    container: str, *command: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return _docker("exec", "--user", AGENT_UID, container, *command, check=check)


def _inspect_mount(project: str, session_id: str, mode: str) -> str:
    containers, volumes = _resources(project, session_id)
    _require(len(containers) == len(volumes) == 1, "workspace_resource_count")
    [container], [volume] = containers, volumes
    [info] = json.loads(_docker("container", "inspect", container).stdout)
    [volume_info] = json.loads(_docker("volume", "inspect", volume).stdout)
    for role, labels in (
        ("sandbox", info["Config"]["Labels"]),
        ("workspace", volume_info["Labels"]),
    ):
        _require(
            all(
                labels.get(name) == val
                for name, val in _labels(project, session_id, role).items()
            ),
            "workspace_identity_mismatch",
        )
    options = volume_info.get("Options") or {}
    reference = options.get("workspace_ref", "")
    _require(
        set(options) == {"mode", "workspace_ref"}
        and options["mode"] == mode
        and isinstance(reference, str)
        and re.fullmatch(r"[0-9a-f]{32}", reference) is not None
        and all(
            labels.get(f"{PREFIX}.workspace_ref") == reference
            and labels.get(f"{PREFIX}.workspace_mode") == mode
            for labels in (info["Config"]["Labels"], volume_info["Labels"])
        ),
        "workspace_volume_options",
    )
    mount = [item for item in info["Mounts"] if item["Destination"] == "/work"]
    _require(
        len(mount) == 1
        and mount[0]["Type"] == "volume"
        and mount[0]["Name"] == volume
        and mount[0]["Driver"] == volume_info["Driver"]
        and mount[0]["RW"] is (mode == "rw"),
        "workspace_attach_mode",
    )
    host = info["HostConfig"]
    security_options = host.get("SecurityOpt") or []
    _require(
        host["NetworkMode"] == "none"
        and host["ReadonlyRootfs"] is True
        and host["Privileged"] is False
        and host["CapDrop"] == ["ALL"]
        and not host.get("CapAdd")
        and not host.get("Devices")
        and any(
            option in ("no-new-privileges", "no-new-privileges:true")
            for option in security_options
        ),
        "agent_host_config",
    )
    mountinfo = _agent(container, "/bin/cat", "/proc/self/mountinfo").stdout
    mounted = [line for line in mountinfo.splitlines() if line.split()[4] == b"/work"]
    _require(
        len(mounted) == 1
        and b" - fuse.sshfs " in mounted[0]
        and mode.encode() in mounted[0].split()[5].split(b","),
        "agent_actual_sshfs_mount",
    )
    _agent(
        container,
        "/bin/sh",
        "-ec",
        "test ! -e /work/.git && test ! -e /dev/fuse && test ! -e /var/run/docker.sock",
    )
    _require(
        b".git" not in _agent(container, "/bin/ls", "-a", "/work").stdout.splitlines(),
        "git_exported",
    )
    _require(
        _agent(container, "/bin/cat", "/work/container-marker.txt").stdout == MARKER,
        "client_fixture_mismatch",
    )
    return container


def _write_as_agent(container: str, folder: Path, mode: str) -> None:
    if mode == "ro":
        attempted = _agent(
            container, "/bin/sh", "-ec", "printf x > /work/forbidden.txt", check=False
        )
        _require(
            attempted.returncode != 0
            and b"Read-only file system" in attempted.stderr
            and not (folder / "forbidden.txt").exists(),
            "readonly_write_not_refused",
        )
        return

    (folder / "container-marker.txt").write_bytes(UPDATED_MARKER)
    _require(
        _agent(container, "/bin/cat", "/work/container-marker.txt").stdout
        == UPDATED_MARKER,
        "local_edit_not_visible",
    )
    _agent(
        container,
        "/usr/local/bin/python3",
        "-I",
        "-S",
        "-c",
        f"from pathlib import Path; Path('/work/from-sandbox.bin').write_bytes({SANDBOX_BYTES!r})",
    )
    _require(
        (folder / "from-sandbox.bin").read_bytes() == SANDBOX_BYTES,
        "sandbox_edit_not_visible",
    )


async def _verify_auth(gateway: str, token: str, session_id: str) -> None:
    endpoint = f"{gateway.rstrip('/')}/v1/sessions/{session_id}"
    foreign = secrets.token_hex(32)
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
    ) as http:
        for credential, status, error in (
            (secrets.token_urlsafe(32), 401, "invalid_token_accepted"),
            (token, 404, "foreign_owner_accepted"),
        ):
            async with http.get(
                endpoint,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "X-Session-Proof": foreign,
                },
                allow_redirects=False,
            ) as response:
                _require(response.status == status, error)


async def _route(session: MountedSession, folder: Path) -> None:
    turn = await session.start_run(
        "Прочитай маркер, создай route-proof.txt и ответь.", timeout_s=100
    )
    events = [event async for event in session.events(turn["run_id"], timeout=180)]
    results = [event["result"] for event in events if event["type"] == "result"]
    _require(
        len(results) == 1
        and results[0]["execution_status"] == "success"
        and results[0]["session_id"] == session.session_id
        and results[0]["run_id"] == turn["run_id"]
        and results[0]["collection_status"] == "complete"
        and results[0]["writers_stopped"] is True
        and any(event["type"] == "message" and event["text"] for event in events)
        and any(event["type"] == "tool" for event in events),
        "mounted_turn_failed",
    )
    _require(
        (folder / "route-proof.txt").read_bytes() == "из сандбокса\n".encode(),
        "mounted_turn_file_missing",
    )


def _fixture(folder: Path) -> None:
    folder.mkdir()
    (folder / "container-marker.txt").write_bytes(MARKER)
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("local-only\n", encoding="utf-8")


async def _cleanup(session: MountedSession, project: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {"confirmed": False}
    close_ok = False
    try:
        close = await session.close()
        close_ok = close.ok
        evidence.update(
            tunnel_closed=close.tunnel_closed, export_drained=close.export_drained
        )
    except (OSError, RuntimeError, ValueError) as failure:
        evidence["close_error"] = type(failure).__name__
    try:
        containers, volumes = await asyncio.to_thread(
            _resources, project, session.session_id
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as failure:
        evidence["docker_error"] = type(failure).__name__
    else:
        evidence.update(
            sandbox_absent=not containers,
            volume_absent=not volumes,
            confirmed=close_ok and not containers and not volumes,
        )
    return evidence


async def _probe(
    gateway: str, token: str, project: str, report: dict[str, Any]
) -> None:
    with tempfile.TemporaryDirectory(prefix="eval-mounted-probe-") as temp:
        async with MountedClient(gateway, token, timeout=95) as client:
            for mode in ("rw", "ro"):
                folder = Path(temp) / mode
                _fixture(folder)
                session = client.session(folder, profile="mock", mode=mode)
                try:
                    snapshot = await session.mount()
                    _require(
                        snapshot["state"] == "ready" and snapshot["mode"] == mode,
                        "mount_not_ready",
                    )
                    container = await asyncio.to_thread(
                        _inspect_mount, project, session.session_id, mode
                    )
                    report["checks"].append(
                        f"sshfs_identity_and_agent_isolation_{mode}"
                    )
                    await asyncio.to_thread(_write_as_agent, container, folder, mode)
                    report["checks"].append(f"file_access_{mode}")
                    if mode == "rw":
                        await _verify_auth(gateway, token, session.session_id)
                        report["checks"].append(
                            "invalid_token_and_foreign_owner_denied"
                        )
                        await _route(session, folder)
                        report["checks"].append("mounted_turn_and_file")
                finally:
                    report["cleanup"][mode] = await _cleanup(session, project)
                _require(
                    report["cleanup"][mode]["confirmed"],
                    "workspace_cleanup_unconfirmed",
                )


def _loopback(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
        return host == "localhost" or (
            host is not None and ip_address(host).is_loopback
        )
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, help="URL локального mock Gateway")
    parser.add_argument("--project", required=True, help="PROJECT_ID mock-стека")
    parser.add_argument("--output", type=Path, help="Путь для JSON-отчёта")
    args = parser.parse_args()
    if not _loopback(args.gateway):
        parser.error("Нужен адрес loopback локального mock Gateway")
    token = os.environ.get("CLIENT_TOKEN")
    if not token:
        parser.error("Укажите тестовый CLIENT_TOKEN в окружении")
    if args.output and args.output.exists():
        parser.error("Файл отчёта уже существует")
    report: dict[str, Any] = {"checks": [], "cleanup": {}, "error": None}
    try:
        asyncio.run(_probe(args.gateway, token, args.project, report))
    except KeyboardInterrupt:
        report["error"] = "interrupted"
    except (ProbeError, ClientError) as failure:
        report["error"] = str(failure)
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        aiohttp.ClientError,
        subprocess.SubprocessError,
    ) as failure:
        report["error"] = type(failure).__name__
    report["passed"] = report["error"] is None
    if args.output:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        report["output"] = str(args.output)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
