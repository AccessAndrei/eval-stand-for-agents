"""Реальные ограниченные атаки на сборщик; ни модель, ни auth-store не нужны."""

import argparse
import base64
import hashlib
import json
import signal
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from tests.infra.native_probe import Probe

API_CLIENT = """
import json,os,sys,urllib.request,urllib.error
d=json.load(sys.stdin)
r=urllib.request.Request('http://127.0.0.1:8081'+d['path'],method=d['method'],
 data=None if d.get('body') is None else json.dumps(d['body']).encode(),
 headers={'Authorization':'Bearer '+os.environ['MANAGER_TOKEN'],
          'X-Project-ID':os.environ['PROJECT_ID'],'Content-Type':'application/json'})
try:
 with urllib.request.urlopen(r,timeout=40) as response:
  print(json.dumps({'status':response.status,'body':json.load(response)}))
except urllib.error.HTTPError as error:
 print(json.dumps({'status':error.code,'body':error.read(8192).decode(errors='replace')}))
"""

PID_INSPECTOR = """
import json,os
from pathlib import Path
pids=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit(): continue
 try: lines=(p/'status').read_text().splitlines()
 except OSError: continue
 uid=next(x for x in lines if x.startswith('Uid:')).split()[1]
 state=next(x for x in lines if x.startswith('State:'))
 if uid=='1000' and 'Z (zombie)' not in state: pids.append(int(p.name))
print(json.dumps(pids))
"""

WRITER = """
import os,time
from pathlib import Path
limit=time.monotonic()+30
i=0
while time.monotonic()<limit:
 i+=1
 Path('/work/tick.tmp').write_text(str(i)+chr(10))
 os.replace('/work/tick.tmp','/work/tick.txt')
 time.sleep(.002)
"""

SWAPPER = """
import os,time
from pathlib import Path
limit=time.monotonic()+30
p=Path('/work/slot')
while time.monotonic()<limit:
 p.unlink(missing_ok=True)
 p.symlink_to('/proc/1/root/tmp/collector-canary')
 time.sleep(.002)
 p.unlink(missing_ok=True)
 p.write_text('safe'+chr(10))
 time.sleep(.002)
"""


class CollectorProbe(Probe):
    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker/compose.mock.yml",
                "exec",
                "-T",
                "sandbox-manager",
                "python",
                "-c",
                API_CLIENT,
            ],
            input=json.dumps({"method": method, "path": path, "body": body}).encode(),
            capture_output=True,
            check=True,
            timeout=50,
            env=self.docker_environment(),
        )
        return json.loads(result.stdout)

    def command(
        self, container: str, user: str, source: str, detached: bool = False
    ) -> bytes:
        return self.docker(
            "exec",
            *(["-d"] if detached else []),
            "--user",
            user,
            container,
            "/usr/local/bin/python3",
            "-I",
            "-S",
            "-c",
            source,
        )

    def run_case(self, case: str) -> dict:
        project = self.settings.get("PROJECT_ID", "eval-stand")
        run_id = self.run_id
        request = {
            "version": "v0",
            "project_id": project,
            "run_id": run_id,
            "task_id": "B1.1",
            "fingerprint": hashlib.sha256(run_id.encode()).hexdigest(),
            "files": [{"path": "tick.txt", "content": "0\n"}]
            + [
                {"path": f"fixture/{i:03}.txt", "content": "x" * 4096}
                for i in range(180)
            ],
            "read_only_paths": [],
            "timeout_s": 60,
        }
        control = None
        sentinel = None
        self.sentinel_name = None
        summary = {"case": case, "run_id": run_id, "passed": False}
        canary = "collector-canary-" + uuid.uuid4().hex
        try:
            opened = self.api("POST", "/v0/environments", request)
            self.save("provision.json", opened)
            if opened["status"] != 200:
                raise RuntimeError("provision_failed")
            control = self.control_message(opened["body"])
            names = (
                self.docker(
                    "ps",
                    "--filter",
                    "label=io.eval-stand.run=" + run_id,
                    "--filter",
                    "label=io.eval-stand.role=sandbox",
                    "--filter",
                    "label=io.eval-stand.owner=eval-stand-for-agents",
                    "--filter",
                    "label=io.eval-stand.project=" + project,
                    "--filter",
                    "label=io.eval-stand.handle=" + control["handle"],
                    "--format",
                    "{{.ID}}",
                )
                .decode()
                .splitlines()
            )
            if len(names) != 1:
                raise RuntimeError("sandbox_identity_unconfirmed")
            container = json.loads(self.docker("inspect", names[0]))[0]["Id"]
            self.command(
                container,
                "1001:1001",
                "from pathlib import Path;p=Path('/tmp/collector-canary');p.write_text("
                + repr(canary)
                + ");p.chmod(0o600)",
            )
            if case in {"writer", "interference", "link-swap"}:
                self.command(
                    container,
                    "1000:1000",
                    SWAPPER if case == "link-swap" else WRITER,
                    detached=True,
                )
                before = json.loads(self.command(container, "1001:1001", PID_INSPECTOR))
                if not before:
                    raise RuntimeError("writer_not_running")
                summary["agent_pids_before"] = before
            elif case == "symlink":
                self.command(
                    container,
                    "1000:1000",
                    "from pathlib import Path;Path('/work/slot').symlink_to('/proc/1/root/tmp/collector-canary')",
                )
            elif case == "size-limit":
                self.command(
                    container,
                    "1000:1000",
                    "from pathlib import Path;Path('/work/big.bin').write_bytes(bytes(1048577))",
                )
            if case == "interference":
                sentinel, isolation = self.interference(container)
                self.save("collector-interference.json", isolation)
                summary["interference_denied"] = (
                    isolation["signal_denied"]
                    and isolation["fd_denied"]
                    and isolation["memory_denied"]
                    and isolation["uid"] == 1000
                )
                if not summary["interference_denied"]:
                    raise RuntimeError("collector_interference_not_blocked")
            collected, actual = self.collect_observed(control, container)
            self.save("actual-collector.json", actual)
            self.save("collection.json", collected)
            if collected["status"] != 200:
                raise RuntimeError("collection_api_failed")
            value = collected["body"]
            after = json.loads(self.command(container, "1001:1001", PID_INSPECTOR))
            contents = {
                f["path"]: base64.b64decode(f["content_base64"]) for f in value["files"]
            }
            leaked = any(canary.encode() in content for content in contents.values())
            if sentinel:
                summary["collector_sentinel_survived"] = json.loads(
                    self.docker("inspect", sentinel)
                )[0]["State"]["Running"]
            summary.update(
                collection_status=value["status"],
                agent_pids_after=after,
                canary_leaked=leaked,
                files=list(contents),
                trusted_collection_used=actual.get("verified", False),
            )
            if case in {"writer", "interference"}:
                summary["writer_progress"] = int(
                    contents.get("tick.txt", b"0").strip() or b"0"
                )
            expected = (
                {"complete"}
                if case in {"writer", "interference"}
                else {"incomplete"}
                if case in {"symlink", "size-limit"}
                else {"complete", "incomplete"}
            )
            summary["passed"] = (
                value["status"] in expected
                and not after
                and not leaked
                and actual.get("verified") is True
                and (
                    case not in {"writer", "interference"}
                    or summary["writer_progress"] > 0
                )
                and (not sentinel or summary["collector_sentinel_survived"])
            )
        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
            KeyError,
            subprocess.SubprocessError,
        ) as error:
            summary["error"] = type(error).__name__
        except KeyboardInterrupt:
            summary["error"] = "interrupted"
        finally:
            cleanup = self.cleanup_resources(control)
            summary.update(cleanup)
            summary["passed"] &= cleanup["cleanup_status"] == "success"
        return summary

    def control_message(self, body: dict) -> dict:
        from contracts import validate

        if not isinstance(body, dict):
            raise ValueError("invalid_control_body")

        control = {
            key: body[key] for key in ("version", "project_id", "run_id", "handle")
        }
        validate("release", control)
        if control["run_id"] != self.run_id or control[
            "project_id"
        ] != self.settings.get("PROJECT_ID", "eval-stand"):
            raise ValueError("foreign_control")
        return control

    def cleanup_resources(self, control: dict | None) -> dict:
        errors = []
        if self.sentinel_name:
            try:
                info = json.loads(self.docker("inspect", self.sentinel_name))[0]
                if (
                    info["Config"]
                    .get("Labels", {})
                    .get("io.eval-stand.collector-probe")
                    != self.run_id
                ):
                    raise ValueError("foreign_sentinel")
                self.docker("rm", "-f", info["Id"])
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                errors.append("sentinel_cleanup_unconfirmed")
        try:
            if control is None:
                lookup = self.api("GET", "/v0/environments/by-run/" + self.run_id)
                self.save("recovery-lookup.json", lookup)
                if lookup["status"] == 404:
                    return {
                        "cleanup_status": "failed" if errors else "success",
                        "cleanup_errors": errors,
                        "environment_not_created": True,
                    }
                if lookup["status"] != 200:
                    raise RuntimeError("lookup_failed")
                control = self.control_message(lookup["body"])
            released = self.api(
                "POST", f"/v0/environments/{control['handle']}/release", control
            )
            self.save("release.json", released)
            if (
                released["status"] != 200
                or released["body"]["cleanup_status"] != "success"
            ):
                raise RuntimeError("release_failed")
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
        ):
            errors.append("environment_cleanup_unconfirmed")
        return {
            "cleanup_status": "failed" if errors else "success",
            "cleanup_errors": errors,
        }

    def collect_observed(self, control: dict, container: str) -> tuple[dict, dict]:
        observed = {"verified": False}
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(
                self.api,
                "POST",
                f"/v0/environments/{control['handle']}/collect",
                control,
            )
            while not future.done():
                ids = (
                    self.docker(
                        "ps",
                        "-a",
                        "--filter",
                        "label=io.eval-stand.run=" + self.run_id,
                        "--filter",
                        "label=io.eval-stand.role=collector",
                        "--filter",
                        "label=io.eval-stand.owner=eval-stand-for-agents",
                        "--filter",
                        "label=io.eval-stand.project=" + control["project_id"],
                        "--filter",
                        "label=io.eval-stand.handle=" + control["handle"],
                        "--format",
                        "{{.ID}}",
                    )
                    .decode()
                    .splitlines()
                )
                if ids:
                    try:
                        info = json.loads(self.docker("inspect", ids[0]))[0]
                        observed = self.collector_identity(info, container)
                        break
                    except subprocess.CalledProcessError:
                        pass
                time.sleep(0.05)
            return future.result(timeout=50), observed

    @staticmethod
    def collector_identity(info: dict, container: str) -> dict:
        host, config = info["HostConfig"], info["Config"]
        caps = {value.removeprefix("CAP_") for value in (host.get("CapAdd") or [])}
        observed = {
            "id": info["Id"],
            "image": info["Image"],
            "user": config["User"],
            "pid_namespace": host["PidMode"],
            "cap_add": sorted(caps),
            "cap_drop": host["CapDrop"],
            "readonly_root": host["ReadonlyRootfs"],
            "network": host["NetworkMode"],
            "privileged": host["Privileged"],
        }
        observed["verified"] = (
            config["User"] == "0:0"
            and host["PidMode"] == "container:" + container
            and caps == {"KILL", "SYS_PTRACE", "DAC_READ_SEARCH"}
            and host["CapDrop"] == ["ALL"]
            and host["ReadonlyRootfs"]
            and host["NetworkMode"] == "none"
            and not host["Privileged"]
            and config["Entrypoint"] == ["/usr/local/bin/python3"]
            and config["Cmd"][-3] == "collect_trusted"
        )
        return observed

    def interference(self, container: str) -> tuple[str, dict]:
        name = "eval-collector-sentinel-" + uuid.uuid4().hex[:12]
        self.sentinel_name = name
        sentinel = (
            self.docker(
                "run",
                "-d",
                "--name",
                name,
                "--pull",
                "never",
                "--platform",
                self.settings.get("DOCKER_PLATFORM", "linux/arm64"),
                "--label",
                "com.docker.compose.project=collector-test",
                "--label",
                "com.docker.compose.service=sentinel",
                "--label",
                "io.eval-stand.collector-probe=" + self.run_id,
                "--network",
                "none",
                "--read-only",
                "--pid",
                "container:" + container,
                "--cap-drop",
                "ALL",
                "--cap-add",
                "KILL",
                "--cap-add",
                "SYS_PTRACE",
                "--cap-add",
                "DAC_READ_SEARCH",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "0:0",
                "--cpus",
                "0.05",
                "--memory",
                "32000000",
                "--memory-swap",
                "32000000",
                "--pids-limit",
                "8",
                "--entrypoint",
                "/usr/local/bin/python3",
                "eval-stand-sandbox:0.159.3",
                "-I",
                "-S",
                "-c",
                "import os,time;print(os.getpid(),flush=True);time.sleep(60)",
            )
            .decode()
            .strip()
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            output = self.docker("logs", sentinel).decode().strip()
            if output:
                break
            time.sleep(0.1)
        pid = int(output)
        source = (
            "import os,signal,json;r={'uid':os.getuid()};p="
            + str(pid)
            + "\ntry: os.kill(p,signal.SIGTERM);r['signal_denied']=False\nexcept PermissionError: r['signal_denied']=True\nfor name,path in [('fd','/proc/%d/fd/1'%p),('memory','/proc/%d/mem'%p)]:\n try:\n  f=os.open(path,os.O_WRONLY);os.close(f);r[name+'_denied']=False\n except PermissionError:r[name+'_denied']=True\nprint(json.dumps(r))"
        )
        result = json.loads(self.command(container, "1000:1000", source))
        result["sentinel_pid"] = pid
        return sentinel, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=("writer", "interference", "link-swap", "symlink", "size-limit"),
        required=True,
    )
    args = parser.parse_args()
    output = Path("results") / (
        "native-collector-"
        + args.case
        + "-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    probe = CollectorProbe(output)

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt("terminated")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        summary = probe.run_case(args.case)
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        summary = {
            "case": args.case,
            "run_id": probe.run_id,
            "passed": False,
            "error": type(error).__name__,
        }
    finally:
        signal.signal(signal.SIGTERM, previous)
    probe.save("summary.json", summary)
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
