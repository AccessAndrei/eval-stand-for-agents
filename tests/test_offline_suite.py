"""Оркестратор не прерывает занятый стенд и сохраняет неуспех/teardown."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.infra import native_probe, offline_suite
from tests.infra.collector_probe import CollectorProbe


def test_running_stack_is_rejected_without_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "llm-proxy\n", "")

    monkeypatch.setattr(offline_suite, "local_settings", lambda: {})
    monkeypatch.setattr(subprocess, "run", run)
    report = offline_suite.Suite(tmp_path / "suite").run()
    assert not report["passed"]
    assert report["error"] == "existing_stack_running_no_changes_made"
    assert len(calls) == 1
    assert calls[0][-4:] == ["ps", "--status", "running", "--services"]
    assert report["coverage"]["native_executed"] == []


def test_failed_native_step_stops_suite_and_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(command)
        code = 1 if "tests.infra.native_probe" in command else 0
        output = (
            json.dumps({"output": "native-failed", "passed": False}) if code else "ok"
        )
        return subprocess.CompletedProcess(command, code, output, "")

    monkeypatch.setattr(offline_suite, "local_settings", lambda: {})
    monkeypatch.setattr(offline_suite.Suite, "prerequisites", lambda self: None)
    monkeypatch.setattr(subprocess, "run", run)
    report = offline_suite.Suite(tmp_path / "suite").run()
    assert not report["passed"] and report["error"] == "route"
    assert sum("tests.infra.native_probe" in call for call in calls) == 1
    assert sum("scripts.shutdown" in call for call in calls) == 1
    assert report["coverage"]["native_executed"] == ["route"]
    assert "harness-recovery" in report["coverage"]["native_not_run"]
    assert (
        json.loads((tmp_path / "suite" / "summary.json").read_text())["passed"] is False
    )


def test_optional_missing_builder_does_not_turn_successful_checks_into_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        offline_suite,
        "local_settings",
        lambda: {"CLIENT_TOKEN": "private-management-token"},
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 1, "", "private-management-token")

    monkeypatch.setattr(subprocess, "run", run)
    suite = offline_suite.Suite(tmp_path / "suite")
    suite.command("optional", ["docker", "inspect", "missing"], required=False)
    assert suite.steps[0]["required"] is False
    assert "private-management-token" not in Path(suite.steps[0]["log"]).read_text()


def test_collector_probe_releases_if_docker_lookup_loses_resource() -> None:
    probe = object.__new__(CollectorProbe)
    probe.settings = {"PROJECT_ID": "test"}
    probe.run_id = "collector-test"
    calls = []

    def api(method: str, path: str, body: dict | None = None) -> dict:
        calls.append(path)
        return {
            "status": 200,
            "body": {
                "version": "v0",
                "project_id": "test",
                "run_id": probe.run_id,
                "handle": "handle",
                "cleanup_status": "success",
            },
        }

    probe.api = api
    probe.docker = lambda *args: b""
    probe.save = lambda *args: None
    result = probe.run_case("writer")
    assert result["error"] == "RuntimeError" and not result["passed"]
    assert result["cleanup_status"] == "success"
    assert calls[-1] == "/v0/environments/handle/release"


def test_collector_probe_recovers_lost_provision_by_lookup_without_replay() -> None:
    probe = object.__new__(CollectorProbe)
    probe.settings, probe.run_id = {"PROJECT_ID": "test"}, "lost-provision"
    calls = []

    def api(method: str, path: str, body: dict | None = None) -> dict:
        calls.append((method, path))
        if path == "/v0/environments":
            raise TimeoutError("response lost")
        return {
            "status": 200,
            "body": {
                "version": "v0",
                "project_id": "test",
                "run_id": probe.run_id,
                "handle": "recovered",
                "cleanup_status": "success",
            },
        }

    probe.api, probe.save = api, lambda *args: None
    result = probe.run_case("writer")
    assert not result["passed"] and result["cleanup_status"] == "success"
    assert calls == [
        ("POST", "/v0/environments"),
        ("GET", "/v0/environments/by-run/lost-provision"),
        ("POST", "/v0/environments/recovered/release"),
    ]


def test_sentinel_cleanup_failure_does_not_skip_environment_release() -> None:
    probe = object.__new__(CollectorProbe)
    probe.run_id, probe.sentinel_name = "test", "sentinel"
    calls = []

    def docker(*args: str) -> bytes:
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": "sentinel-id",
                        "Config": {"Labels": {"io.eval-stand.collector-probe": "test"}},
                    }
                ]
            ).encode()
        raise TimeoutError("rm failed")

    def api(method: str, path: str, body: dict | None = None) -> dict:
        calls.append(path)
        return {"status": 200, "body": {"cleanup_status": "success"}}

    probe.docker, probe.api, probe.save = docker, api, lambda *args: None
    result = probe.cleanup_resources({"handle": "owned"})
    assert calls == ["/v0/environments/owned/release"]
    assert result["cleanup_errors"] == ["sentinel_cleanup_unconfirmed"]


def test_actual_collector_identity_cannot_pass_with_agent_uid() -> None:
    info = {
        "Id": "collector",
        "Image": "image",
        "Config": {
            "User": "0:0",
            "Entrypoint": ["/usr/local/bin/python3"],
            "Cmd": ["collect_trusted", "1048576", "256"],
        },
        "HostConfig": {
            "CapAdd": ["CAP_KILL", "CAP_SYS_PTRACE", "CAP_DAC_READ_SEARCH"],
            "CapDrop": ["ALL"],
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "Privileged": False,
            "PidMode": "container:sandbox",
        },
    }
    assert CollectorProbe.collector_identity(info, "sandbox")["verified"]
    info["Config"]["User"] = "1000:1000"
    assert not CollectorProbe.collector_identity(info, "sandbox")["verified"]


def test_proxy_restoration_and_summary_survive_interruption_and_restore_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, saved = [], []

    class FakeProbe:
        def __init__(self, output: Path, timeout: int) -> None:
            self.run_id, self.stopped_proxy = "test", "owned-proxy"

        def run(self, profile: str, scenario: str) -> dict:
            raise KeyboardInterrupt()

        def request(self, method: str, path: str) -> tuple[int, dict]:
            calls.append("cancel")
            return 200, {}

        def restore_proxy(self) -> bool:
            calls.append("restore")
            raise RuntimeError("restore failed")

        def save(self, name: str, value: dict) -> None:
            saved.append(dict(value))

        def redact(self, value: object) -> object:
            return value

    monkeypatch.setattr(native_probe, "Probe", FakeProbe)
    monkeypatch.setattr(sys, "argv", ["native_probe", "--scenario", "proxy-failure"])
    monkeypatch.setattr(native_probe.signal, "signal", lambda *args: None)
    assert native_probe.main() == 1
    assert calls == ["cancel", "restore"]
    assert saved[-1]["probe_error"] == "interrupted"
    assert saved[-1]["proxy_restored"] is False


def test_shutdown_filters_follow_configured_project_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specification = importlib.util.spec_from_file_location(
        "native_shutdown_test", "tests/infra/native_shutdown.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    path = tmp_path / ".env"
    path.write_text("COMPOSE_PROJECT_NAME=custom-stack\nPROJECT_ID=custom-runtime\n")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    assert module.configured_projects(path) == ("custom-stack", "custom-runtime")
    monkeypatch.setenv("PROJECT_ID", "explicit-runtime")
    assert module.configured_projects(path) == ("custom-stack", "explicit-runtime")


def test_offline_suite_rejects_repurposed_live_profile(tmp_path: Path) -> None:
    profiles = json.loads(Path("config/profiles.json").read_text())
    profiles["mock"]["endpoint"] = "https://chatgpt.com/backend-api/codex"
    profiles["mock"]["auth"] = "chatgpt"
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles))
    with pytest.raises(offline_suite.SuiteFailure, match="no_live_calls"):
        offline_suite.verify_offline_profiles(path)


def test_suite_pins_gateway_to_its_local_published_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        offline_suite,
        "local_settings",
        lambda: {"GATEWAY_PORT": "8087", "GATEWAY_URL": "https://unrelated.example"},
    )
    suite = offline_suite.Suite(tmp_path / "suite")
    assert suite.env["GATEWAY_URL"] == "http://127.0.0.1:8087"


def test_proxy_loss_accepts_bounded_timeout_without_replay() -> None:
    result = {
        "execution_status": "timeout",
        "duration_s": 32.4,
        "collection_status": "complete",
        "cleanup_status": "success",
        "tokens": {"actual": None, "reserved": 30000},
    }
    counters = {"requests_received": 1}
    check = native_probe.Probe.proxy_failure_passed
    assert check(result, counters, {}, True, True, 30)
    assert not check({**result, "duration_s": 151}, counters, {}, True, True, 30)
    assert not check(result, {"requests_received": 2}, {}, True, True, 30)
    assert not check(
        {**result, "execution_status": "success"}, counters, {}, True, True, 30
    )
