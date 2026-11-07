"""Офлайн-прогон через gateway с настоящими Codex и Docker-исполнителем."""

import argparse
import base64
import json
import os
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from contracts import validate


def local_settings() -> dict[str, str]:
    settings = dict(os.environ)
    for line in Path(".env").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            settings.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return settings


class Probe:
    def __init__(self, output: Path, timeout: int = 300) -> None:
        self.settings = local_settings()
        self.secrets = tuple(
            value
            for key, value in self.settings.items()
            if key.endswith("_TOKEN") and value
        )
        self.base = self.settings.get(
            "GATEWAY_URL",
            f"http://127.0.0.1:{self.settings.get('GATEWAY_PORT', '8080')}",
        )
        self.run_id = "native-" + uuid.uuid4().hex
        self.marker = "sandbox-" + uuid.uuid4().hex
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.timeout = timeout
        self.secret_leak = False
        self.observed: dict = {}
        self.action_done = False
        self.control_value = "control-" + uuid.uuid4().hex
        self.control_unchanged = False
        self.collector_observed = False
        self.stopped_proxy: str | None = None

    def observe_collector(self) -> None:
        if self.collector_observed:
            return
        names = (
            self.docker(
                "ps",
                "-a",
                "--filter",
                "label=io.eval-stand.run=" + self.run_id,
                "--filter",
                "label=io.eval-stand.role=collector",
                "--format",
                "{{.Names}}",
            )
            .decode()
            .splitlines()
        )
        if not names:
            return
        try:
            info = json.loads(self.docker("inspect", names[0]))[0]
        except subprocess.CalledProcessError:
            return
        host = info["HostConfig"]
        self.save(
            "collector.json",
            {
                "image_digest": info["Image"],
                "user": info["Config"]["User"],
                "pid_namespace": host["PidMode"],
                "network": host["NetworkMode"],
                "readonly_root": host["ReadonlyRootfs"],
                "privileged": host["Privileged"],
                "cap_drop": host["CapDrop"],
                "cap_add": host["CapAdd"],
                "memory": host["Memory"],
                "nano_cpus": host["NanoCpus"],
            },
        )
        self.collector_observed = True

    def control(self, write: bool = False) -> str:
        source = "from pathlib import Path;p=Path('/tmp/native-control.txt');"
        if write:
            source += f"p.write_text({self.control_value!r})"
        else:
            source += "print(p.read_text(),end='')"
        return self.docker(
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker/compose.mock.yml",
            "exec",
            "-T",
            "harness",
            "python",
            "-c",
            source,
        ).decode()

    def upstream_counters(self, profile: str) -> dict:
        service = "mock-provider-alt" if profile == "mock-alt" else "mock-provider"
        source = (
            "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen("
            + repr("http://127.0.0.1:8090/stats/" + self.run_id)
            + "))))"
        )
        return json.loads(
            self.docker(
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker/compose.mock.yml",
                "exec",
                "-T",
                service,
                "python",
                "-c",
                source,
            )
        )

    def budget_counters(self, profile: str) -> dict:
        counters = self.upstream_counters(profile)
        source = (
            "import json,os,urllib.request;r=urllib.request.Request("
            + repr("http://127.0.0.1:8000/v0/runs/" + self.run_id)
            + ",headers={'Authorization':'Bearer '+os.environ['INTERNAL_TOKEN']});d=json.load(urllib.request.urlopen(r));print(json.dumps({'requests':d['requests'],'error':d['error']}))"
        )
        ledger = json.loads(
            self.docker(
                "compose",
                "-f",
                "docker-compose.yml",
                "-f",
                "docker/compose.mock.yml",
                "exec",
                "-T",
                "llm-proxy",
                "python",
                "-c",
                source,
            )
        )
        return {
            **counters,
            "proxy_requests": ledger["requests"],
            "proxy_error": ledger["error"],
        }

    def inject(self, scenario: str, launch: dict) -> None:
        if (
            self.action_done
            or not self.observed
            or scenario
            not in {
                "cancel",
                "executor-failure",
                "recovery",
                "harness-recovery",
                "proxy-failure",
            }
        ):
            return
        try:
            source = (
                "from pathlib import Path;"
                "\nfor p in Path('/proc').iterdir():"
                "\n if p.name.isdigit():"
                "\n  try: value=(p/'cmdline').read_bytes().replace(bytes([0]),b' ').decode(errors='replace')"
                "\n  except OSError: continue"
                "\n  print(p.name,value)"
            )
            processes = self.docker(
                "exec",
                "--user",
                "1001:1001",
                self.observed["name"],
                "/usr/local/bin/python3",
                "-I",
                "-S",
                "-c",
                source,
            ).decode()
            self.save("processes.json", {"commands": processes})
        except subprocess.CalledProcessError:
            return
        sleep = (
            "sleep 60"
            if scenario in {"cancel", "recovery", "harness-recovery"}
            else "sleep 15"
        )
        if sleep not in processes:
            return
        self.action_done = True
        if scenario == "proxy-failure":
            proxy = (
                self.docker(
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "docker/compose.mock.yml",
                    "ps",
                    "-q",
                    "llm-proxy",
                )
                .decode()
                .strip()
            )
            if not proxy:
                raise RuntimeError("proxy_not_identified")
            self.stopped_proxy = proxy
            self.docker("kill", "--signal", "KILL", proxy)
            self.save(
                "proxy-killed.json", {"container_id": proxy, "run_id": self.run_id}
            )
        elif scenario in {"recovery", "harness-recovery"}:
            if scenario == "harness-recovery":
                source = (
                    "import json,time;from pathlib import Path;from services.harness.recovery import HarnessRecovery;"
                    "r=HarnessRecovery(Path('/state'));d=r._load("
                    + repr(self.run_id)
                    + ");"
                    "p=r._scan(r._home(d['run_id']),d['owner_nonce'],time.monotonic()+5);"
                    "print(json.dumps({'matching':sum(x.matching for x in p),'anchors':sum(x.anchor for x in p),'foreign_home':any(x.foreign_home for x in p)}))"
                )
                ownership = json.loads(
                    self.docker(
                        "compose",
                        "-f",
                        "docker-compose.yml",
                        "-f",
                        "docker/compose.mock.yml",
                        "exec",
                        "-T",
                        "harness",
                        "python",
                        "-c",
                        source,
                    )
                )
                self.save("harness-ownership.json", ownership)
                if (
                    ownership["matching"] < 2
                    or ownership["anchors"] < 2
                    or ownership["foreign_home"]
                ):
                    raise RuntimeError("harness_markers_unconfirmed")
            self.crash_and_recover(
                "harness" if scenario == "harness-recovery" else "sandbox-manager"
            )
        elif scenario == "cancel":
            code, value = self.request("POST", f"/v0/runs/{self.run_id}/cancel")
            next_request = {**launch, "run_id": "native-next-" + uuid.uuid4().hex}
            next_code, next_value = self.request("POST", "/v0/runs", next_request)
            self.save(
                "cancel.json",
                {
                    "status": code,
                    "body": value,
                    "next_status": next_code,
                    "next_body": next_value,
                },
            )
            if next_code != 409:
                self.request("POST", f"/v0/runs/{next_request['run_id']}/cancel")
                raise RuntimeError("next_attempt_not_busy")
        else:
            source = (
                "import os,signal;from pathlib import Path;found=[];"
                "\nfor p in Path('/proc').iterdir():"
                "\n if p.name.isdigit():"
                "\n  try: a=(p/'cmdline').read_bytes().split(b'\\0')"
                "\n  except (OSError,ProcessLookupError): continue"
                "\n  if len(a)>1 and a[0].endswith(b'/codex') and a[1]==b'exec-server': found.append(int(p.name))"
                "\nfor pid in found: os.kill(pid,signal.SIGKILL)"
                "\nprint(len(found))"
            )
            killed = (
                self.docker(
                    "exec",
                    "--user",
                    "1000:1000",
                    self.observed["name"],
                    "/usr/local/bin/python3",
                    "-I",
                    "-S",
                    "-c",
                    source,
                )
                .decode()
                .strip()
            )
            self.save("executor-killed.json", {"exec_servers_killed": int(killed)})
            if killed != "1":
                raise RuntimeError("executor_not_identified")

    def crash_and_recover(self, component: str) -> None:
        ids = {}
        for service in ("gateway", component):
            ids[service] = (
                self.docker(
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "docker/compose.mock.yml",
                    "ps",
                    "-q",
                    service,
                )
                .decode()
                .strip()
            )
            if not ids[service]:
                raise RuntimeError("service_not_found")
        for service in ("gateway", component):
            self.docker("kill", "--signal", "KILL", ids[service])
        self.save(
            "crash.json",
            {"killed_containers": ids, "restart_order": [component, "gateway"]},
        )
        for service in (component, "gateway"):
            self.docker("start", ids[service])
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                health = (
                    self.docker(
                        "inspect", "--format", "{{.State.Health.Status}}", ids[service]
                    )
                    .decode()
                    .strip()
                )
                if health == "healthy":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("recovery_health_timeout")

    def restore_proxy(self) -> bool:
        if not self.stopped_proxy:
            return True
        self.docker("start", self.stopped_proxy)
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            state = (
                self.docker(
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    self.stopped_proxy,
                )
                .decode()
                .strip()
            )
            if state == "healthy":
                self.stopped_proxy = None
                return True
            time.sleep(1)
        return False

    @staticmethod
    def proxy_failure_passed(
        result: dict,
        counters: dict,
        files: dict,
        injected: bool,
        control_unchanged: bool,
        deadline: int,
    ) -> bool:
        return (
            injected
            and counters["requests_received"] == 1
            and "proxy-fallback.txt" not in files
            and control_unchanged
            and result["execution_status"]
            in {"provider_error", "transport_error", "timeout"}
            and result["duration_s"] <= deadline + 120
            and result["collection_status"] == "complete"
            and result["cleanup_status"] == "success"
            and result["tokens"]["actual"] is None
            and result["tokens"]["reserved"] == 30000
        )

    def docker_environment(self) -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in (
                "PATH",
                "HOME",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            )
            if key in os.environ
        }
        environment.update(
            {
                key: self.settings.get(key, "eval-stand")
                for key in ("COMPOSE_PROJECT_NAME", "PROJECT_ID")
            }
        )
        environment["DOCKER_DEFAULT_PLATFORM"] = self.settings.get(
            "DOCKER_PLATFORM", "linux/arm64"
        )
        return environment

    def docker(self, *args: str) -> bytes:
        return subprocess.run(
            ["docker", *args],
            check=True,
            capture_output=True,
            timeout=20,
            env=self.docker_environment(),
        ).stdout

    def observe(self) -> None:
        if self.observed:
            return
        names = (
            self.docker(
                "ps",
                "--filter",
                "label=io.eval-stand.run=" + self.run_id,
                "--filter",
                "label=io.eval-stand.role=sandbox",
                "--format",
                "{{.Names}}",
            )
            .decode()
            .splitlines()
        )
        if not names:
            return
        info = json.loads(self.docker("inspect", names[0]))[0]
        config, host = info["Config"], info["HostConfig"]
        self.observed = {
            "name": names[0],
            "id": info["Id"],
            "image_digest": info["Image"],
            "user": config["User"],
            "environment_keys": sorted(
                value.split("=", 1)[0] for value in config.get("Env", [])
            ),
            "network": host["NetworkMode"],
            "readonly_root": host["ReadonlyRootfs"],
            "cap_drop": host["CapDrop"],
            "cap_add": host["CapAdd"],
            "security_opt": host["SecurityOpt"],
            "memory": host["Memory"],
            "nano_cpus": host["NanoCpus"],
            "pids_limit": host["PidsLimit"],
            "tmpfs": host["Tmpfs"],
            "mounts": [
                {
                    "destination": mount["Destination"],
                    "readonly": not mount["RW"],
                    "type": mount["Type"],
                }
                for mount in info.get("Mounts", [])
            ],
        }
        if any(secret in str(config.get("Env", [])) for secret in self.secrets):
            self.secret_leak = True
        self.save("sandbox.json", self.observed)

    def redact(self, value: object) -> object:
        encoded = json.dumps(value, ensure_ascii=False)
        for secret in self.secrets:
            if secret in encoded:
                self.secret_leak = True
                encoded = encoded.replace(secret, "[REDACTED]")
        return json.loads(encoded)

    def save(self, name: str, value: object) -> None:
        (self.output / name).write_text(
            json.dumps(self.redact(value), ensure_ascii=False, indent=2) + "\n"
        )

    def request(self, method: str, path: str, body: object = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.settings["CLIENT_TOKEN"],
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = error.read(65536).decode(errors="replace")
            try:
                return error.code, json.loads(payload)
            except ValueError:
                return error.code, {"error": payload}

    def run(self, profile: str = "mock", scenario: str = "route") -> dict:
        self.control(write=True)
        launch = {
            "version": "v0",
            "run_id": self.run_id,
            "task_id": "B1.1",
            "provider_profile": profile,
            "prompt": "[native-probe:route] Выполни инструменты проверки маршрута в выбранной среде.",
            "files": [{"path": "container-marker.txt", "content": self.marker + "\n"}],
            "timeout_s": self.timeout,
        }
        task = None
        if scenario == "b5":
            from baf.runner.batch import launch_request
            from baf.runner.catalog import load_checkpoint

            task = load_checkpoint()[-1]
            launch = launch_request(task, self.run_id, profile)
            launch["timeout_s"] = self.timeout
        self.save("request.json", launch)
        code, accepted = self.request("POST", "/v0/runs", launch)
        self.save("accepted.json", {"status": code, "body": accepted})
        if code not in {200, 202}:
            return {"run_id": self.run_id, "accepted": False, "http_status": code}
        deadline = time.monotonic() + self.timeout + 120
        status = accepted
        while time.monotonic() < deadline:
            code, status = self.request("GET", f"/v0/runs/{self.run_id}")
            self.save("status.json", {"status": code, "body": status})
            if status.get("state") == "terminal":
                break
            self.observe()
            self.observe_collector()
            self.inject(scenario, launch)
            time.sleep(0.25 if status.get("state") == "collecting" else 1)
        else:
            self.request("POST", f"/v0/runs/{self.run_id}/cancel")
            return {"run_id": self.run_id, "terminal": False}
        code, artifacts = self.request("GET", f"/v0/runs/{self.run_id}/artifacts")
        self.save("artifacts.json", {"status": code, "body": artifacts})
        result = status["result"]
        validate("result", result)
        control_reset = False
        if scenario == "harness-recovery":
            source = "from pathlib import Path;print(Path('/tmp/native-control.txt').exists())"
            control_reset = (
                self.docker(
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "docker/compose.mock.yml",
                    "exec",
                    "-T",
                    "harness",
                    "python",
                    "-c",
                    source,
                )
                .decode()
                .strip()
                == "False"
            )
        else:
            self.control_unchanged = self.control() == self.control_value
        outputs = "\n".join(
            event.get("output", "") for event in artifacts.get("events", [])
        )
        forbidden = {
            "CLIENT_TOKEN",
            "MANAGER_TOKEN",
            "HARNESS_TOKEN",
            "PROXY_TOKEN",
            "STAND_INFERENCE_TOKEN",
            "EVAL_STAND_ATTEMPT_OWNER",
            "CODEX_HOME",
            "OPENAI_API_KEY",
        }
        exposed_keys = sorted(
            {line.split("=", 1)[0] for line in outputs.splitlines() if "=" in line}
            & forbidden
        )
        self.secret_leak |= bool(exposed_keys) or "[REDACTED]" in outputs
        files = {
            item["path"]: base64.b64decode(item["content_base64"]).decode(
                errors="replace"
            )
            for item in artifacts.get("files", [])
        }
        summary = {
            "run_id": self.run_id,
            "profile": profile,
            "model": result["model"],
            "provider_snapshot": result.get("provider_snapshot"),
            "scenario": scenario,
            "native": True,
            "execution_status": result["execution_status"],
            "collection_status": result["collection_status"],
            "cleanup_status": result["cleanup_status"],
            "errors": result["errors"],
            "marker_observed": self.marker in outputs,
            "native_uid_1000": "native_uid=1000\n" in outputs,
            "file_change_observed": any(
                event["type"] == "file_change_completed"
                for event in artifacts.get("events", [])
            ),
            "collected_patch": files.get("route-proof.txt"),
            "secret_leak": self.secret_leak,
            "exposed_control_environment_keys": exposed_keys,
            "sandbox_observed": bool(self.observed),
            "control_unchanged": self.control_unchanged,
            "control_reset_after_harness_restart": control_reset,
            "collector_observed": self.collector_observed,
            "tokens": result["tokens"],
            "provenance": result["provenance"],
        }
        summary["passed"] = (
            all(
                summary[key] == "success"
                for key in ("execution_status", "cleanup_status")
            )
            and summary["collection_status"] == "complete"
            and summary["marker_observed"]
            and summary["file_change_observed"]
            and summary["collected_patch"] == "из сандбокса\n"
            and not summary["secret_leak"]
            and not summary["errors"]
            and self.control_unchanged
        )
        if scenario == "b5":
            from baf.runner.artifacts import decode_files
            from baf.runner.checkers import run_checker

            destination = self.output / "collected"
            destination.mkdir()
            for name, data in decode_files(artifacts, result).items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            checker = run_checker(
                task,
                destination,
                run_id=self.run_id,
                collection_status=result["collection_status"],
                evidence=artifacts.get("evidence"),
            )
            self.save("checker.json", checker)
            summary.update(
                checker=checker,
                protected_inputs=artifacts.get("evidence", {}).get("protected_inputs"),
            )
            summary["passed"] = (
                checker["checker_status"] == "passed"
                and result["cleanup_status"] == "success"
                and result["execution_status"] == "success"
                and self.control_unchanged
                and not self.secret_leak
            )
        elif scenario == "cancel":
            summary.update(action_done=self.action_done)
            summary["passed"] = (
                self.action_done
                and result["execution_status"] == "cancelled"
                and result["cleanup_status"] == "success"
                and self.control_unchanged
            )
        elif scenario in {"recovery", "harness-recovery"}:
            summary.update(
                action_done=self.action_done,
                recovery_marker=files.get("recovery-marker.txt"),
            )
            summary["passed"] = (
                self.action_done
                and result["execution_status"] == "interrupted"
                and result["collection_status"] == "complete"
                and result["cleanup_status"] == "success"
                and files.get("recovery-marker.txt") == "survived\n"
                and (
                    control_reset
                    if scenario == "harness-recovery"
                    else self.control_unchanged
                )
                and result["model"] is not None
                and result.get("provider_snapshot") is not None
            )
        elif scenario == "budget":
            counters = self.budget_counters(profile)
            lines = files.get("budget-log.txt", "").splitlines()
            self.save("budget-counters.json", counters)
            summary.update(counters=counters, executed_steps=lines)
            summary["passed"] = (
                result["execution_status"] == "budget_exceeded"
                and result["collection_status"] == "complete"
                and result["cleanup_status"] == "success"
                and result["tokens"]["actual"] is not None
                and result["tokens"]["actual"] + result["tokens"]["reserved"] <= 30000
                and counters["proxy_requests"]
                == counters["requests_received"]
                == counters["tool_calls"]
                == len(lines)
                and lines == [f"step-{i}" for i in range(1, len(lines) + 1)]
                and counters["proxy_error"] == "budget_exceeded"
            )
        elif scenario == "executor-failure":
            summary.update(
                action_done=self.action_done,
                fallback_created="fallback-created.txt" in files,
            )
            summary["passed"] = (
                self.action_done
                and "fallback-created.txt" not in files
                and self.control_unchanged
                and result["cleanup_status"] == "success"
                and result["execution_status"] == "transport_error"
            )
        elif scenario == "proxy-failure":
            counters = self.upstream_counters(profile)
            self.save("upstream-counters.json", counters)
            summary.update(
                action_done=self.action_done,
                upstream=counters,
                fallback_created="proxy-fallback.txt" in files,
                attempt_deadline_s=self.timeout,
                observed_duration_s=result["duration_s"],
                cleanup_allowance_s=120,
            )
            summary["passed"] = self.proxy_failure_passed(
                result,
                counters,
                files,
                self.action_done,
                self.control_unchanged,
                self.timeout,
            )
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("mock", "mock-alt"), default="mock")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--scenario",
        choices=(
            "route",
            "b5",
            "cancel",
            "executor-failure",
            "recovery",
            "harness-recovery",
            "budget",
            "proxy-failure",
        ),
        default="route",
    )
    args = parser.parse_args()
    path = Path("results") / (
        "native-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )
    probe = Probe(path, args.timeout)
    summary = {"run_id": probe.run_id, "passed": False}

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt("terminated")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        summary = probe.run(args.profile, args.scenario)
    except KeyboardInterrupt:
        summary = {
            "run_id": probe.run_id,
            "passed": False,
            "probe_error": "interrupted",
        }
        try:
            probe.request("POST", f"/v0/runs/{probe.run_id}/cancel")
        except (OSError, ValueError, RuntimeError):
            summary["cancel_unconfirmed"] = True
    except (
        OSError,
        URLError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        summary = {
            "run_id": probe.run_id,
            "passed": False,
            "probe_error": type(error).__name__,
        }
    finally:
        try:
            if probe.stopped_proxy:
                try:
                    summary["proxy_restored"] = probe.restore_proxy()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    summary["proxy_restored"] = False
                summary["passed"] = (
                    summary.get("passed", False) and summary["proxy_restored"]
                )
            try:
                probe.save("summary.json", summary)
            except OSError:
                summary.update(passed=False, evidence_write_failed=True)
        finally:
            signal.signal(signal.SIGTERM, previous)
    print(
        json.dumps(probe.redact({"output": str(path), **summary}), ensure_ascii=False)
    )
    return 0 if summary.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
