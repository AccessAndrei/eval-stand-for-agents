"""Один воспроизводимый offline-набор после подготовки зависимостей и образов.

Стек должен быть остановлен: набор владеет только своим запуском Compose,
сохраняет все результаты, проверяет обе формы down и не обращается к Luna.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from tests.infra.native_probe import local_settings

COMPOSE = [
    "docker",
    "compose",
    "-f",
    "docker-compose.yml",
    "-f",
    "docker/compose.mock.yml",
    "-f",
    "docker/compose.single-cpu.yml",
]
NATIVE = (
    ("route", "mock", "route"),
    ("route-alt", "mock-alt", "route"),
    ("cancel", "mock", "cancel"),
    ("after-cancel", "mock-alt", "route"),
    ("executor-failure", "mock", "executor-failure"),
    ("after-executor", "mock-alt", "route"),
    ("budget", "mock", "budget"),
    ("after-budget", "mock-alt", "route"),
    ("proxy-failure", "mock", "proxy-failure"),
    ("after-proxy", "mock-alt", "route"),
    ("manager-recovery", "mock", "recovery"),
    ("harness-recovery", "mock", "harness-recovery"),
    ("b5", "mock", "b5"),
)
COLLECTOR = ("writer", "interference", "link-swap", "symlink", "size-limit")


class SuiteFailure(RuntimeError):
    pass


def verify_offline_profiles(path: Path = Path("config/profiles.json")) -> None:
    profiles = json.loads(path.read_text())
    for name, model, host in (
        ("mock", "mock-codex", "mock-provider"),
        ("mock-alt", "mock-codex-alt", "mock-provider-alt"),
    ):
        profile = profiles.get(name, {})
        if (
            profile.get("auth") != "none"
            or profile.get("protocol") != "responses"
            or profile.get("model") != model
            or profile.get("endpoint", "").rstrip("/") != f"http://{host}:8090"
            or profile.get("budget_mechanism") != "mock-utf8-quarter-v1"
        ):
            raise SuiteFailure("offline_profiles_required_no_live_calls")


class Suite:
    def __init__(self, output: Path) -> None:
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        settings = local_settings()
        self.env = {
            **os.environ,
            "COMPOSE_PROJECT_NAME": settings.get("COMPOSE_PROJECT_NAME", "eval-stand"),
            "PROJECT_ID": settings.get("PROJECT_ID", "eval-stand"),
            "GATEWAY_URL": f"http://127.0.0.1:{settings.get('GATEWAY_PORT', '8080')}",
            "DOCKER_PLATFORM": settings.get("DOCKER_PLATFORM", "linux/arm64"),
            "DOCKER_DEFAULT_PLATFORM": settings.get("DOCKER_PLATFORM", "linux/arm64"),
            "COMPOSE_PARALLEL_LIMIT": "1",
        }
        self.secrets = tuple(
            value
            for key, value in settings.items()
            if "TOKEN" in key and len(value) >= 8
        )
        self.steps: list[dict] = []
        self.started = False

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def command(
        self,
        name: str,
        command: list[str],
        *,
        env: dict | None = None,
        timeout: int = 300,
        required: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**self.env, **(env or {})},
            )
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            stdout = (
                error.stdout.decode(errors="replace")
                if isinstance(error.stdout, bytes)
                else (error.stdout or "")
            )
            stderr, code = "command_timeout", 124
            result = subprocess.CompletedProcess(command, code, stdout, stderr)
        except OSError as error:
            stdout, stderr, code = "", type(error).__name__, 127
            result = subprocess.CompletedProcess(command, code, stdout, stderr)
        log = self.output / (str(len(self.steps)).zfill(2) + "-" + name + ".log")
        log.write_text(self.redact(stdout + "\n" + stderr))
        step = {
            "name": name,
            "returncode": code,
            "log": str(log),
            "passed": code == 0,
            "required": required,
        }
        if stdout.strip().startswith("{"):
            try:
                parsed = json.loads(stdout)
                step["evidence"] = (
                    self.redact(parsed.get("output", ""))
                    if isinstance(parsed, dict)
                    else None
                )
            except ValueError:
                pass
        self.steps.append(step)
        if code and required:
            raise SuiteFailure(name)
        return result

    def prerequisites(self) -> None:
        verify_offline_profiles()
        running = self.command(
            "existing-stack", COMPOSE + ["ps", "--status", "running", "--services"]
        )
        if running.stdout.strip():
            raise SuiteFailure("existing_stack_running_no_changes_made")
        builder = self.command(
            "builder-state",
            [
                "docker",
                "inspect",
                "buildx_buildkit_eval-stand-codex-source0",
                "--format",
                "{{.State.Running}}",
            ],
            required=False,
        )
        if builder.returncode == 0 and builder.stdout.strip() == "true":
            raise SuiteFailure("builder_running_no_changes_made")
        self.command(
            "preflight",
            [
                sys.executable,
                "-m",
                "scripts.preflight",
                "--shared-cpu",
                "--output",
                str(self.output / "preflight.json"),
            ],
        )
        images = self.command(
            "images", COMPOSE + ["--profile", "tools", "config", "--images"]
        )
        for index, image in enumerate(sorted(set(images.stdout.splitlines()))):
            self.command(
                f"image-{index}",
                ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            )

    def up(self) -> None:
        self.started = True
        self.command(
            "up",
            COMPOSE
            + [
                "up",
                "--no-build",
                "--pull",
                "never",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
            ],
            env={"MOCK_SCRIPT_PATH": "/mock-scripts/route.json"},
            timeout=210,
        )

    def script(self, profile: str, scenario: str) -> None:
        fixture = "recovery" if scenario == "harness-recovery" else scenario
        service = "mock-provider-alt" if profile == "mock-alt" else "mock-provider"
        self.command(
            "select-" + fixture + "-" + profile,
            COMPOSE
            + [
                "up",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "--force-recreate",
                "-d",
                "--wait",
                "--wait-timeout",
                "120",
                service,
            ],
            env={"MOCK_SCRIPT_PATH": f"/mock-scripts/{fixture}.json"},
            timeout=150,
        )

    def run(self) -> dict:
        error = None
        try:
            self.prerequisites()
            self.command(
                "unit-http", [sys.executable, "-m", "pytest", "-q"], timeout=300
            )
            self.up()
            for name, profile, scenario in NATIVE:
                self.script(profile, scenario)
                self.command(
                    name,
                    [
                        sys.executable,
                        "-m",
                        "tests.infra.native_probe",
                        "--scenario",
                        scenario,
                        "--profile",
                        profile,
                        "--timeout",
                        "30" if scenario == "proxy-failure" else "180",
                    ],
                    timeout=360,
                )
            for case in COLLECTOR:
                self.command(
                    "collector-" + case,
                    [
                        sys.executable,
                        "-m",
                        "tests.infra.collector_probe",
                        "--case",
                        case,
                    ],
                    timeout=180,
                )
            self.command(
                "make-down",
                [sys.executable, "tests/infra/native_shutdown.py", "make"],
                timeout=220,
            )
            self.started = False
            self.up()
            self.command(
                "compose-down",
                [sys.executable, "tests/infra/native_shutdown.py", "compose"],
                timeout=220,
            )
            self.started = False
        except KeyboardInterrupt:
            error = "interrupted"
        except (
            SuiteFailure,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ) as failure:
            error = (
                str(failure)
                if isinstance(failure, SuiteFailure)
                else type(failure).__name__
            )
        finally:
            if self.started:
                self.command(
                    "failure-cleanup",
                    [sys.executable, "-m", "scripts.shutdown", "--down"],
                    required=False,
                    timeout=180,
                )
        planned_native = (
            [name for name, _, _ in NATIVE]
            + ["collector-" + case for case in COLLECTOR]
            + ["make-down", "compose-down"]
        )
        executed_native = [
            step["name"] for step in self.steps if step["name"] in planned_native
        ]
        report = {
            "version": "v0",
            "suite": "checkpoint1-offline",
            "passed": error is None
            and all(step["passed"] or not step["required"] for step in self.steps),
            "error": error,
            "steps": self.steps,
            "coverage": {
                "unit_http": [
                    "contracts",
                    "checkers",
                    "storage reservations and crash",
                    "lost provision/result responses",
                    "stream interruption and held usage",
                    "live events before terminal",
                    "PID ownership and escalation",
                ],
                "native_planned": planned_native,
                "native_executed": executed_native,
                "native_not_run": [
                    name for name in planned_native if name not in executed_native
                ],
            },
            "no_live_model_calls": True,
            "no_image_download_or_build": True,
        }
        (self.output / "summary.json").write_text(
            self.redact(json.dumps(report, ensure_ascii=False, indent=2)) + "\n"
        )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path("results") / (
        "offline-suite-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt("terminated")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        report = Suite(output).run()
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(
        json.dumps(
            {
                "output": str(output / "summary.json"),
                "passed": report["passed"],
                "error": report["error"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
