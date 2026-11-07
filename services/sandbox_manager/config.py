"""Малые квоты среды являются частью общего бюджета, а не отдельными 8 ГБ."""

from dataclasses import dataclass

OWNER = "eval-stand-for-agents"
PREFIX = "io.eval-stand"
PYTHON = "/usr/local/bin/python3"
CODEX = "/usr/local/bin/codex"
AGENT_UID = "1000:1000"
SUPERVISOR_UID = "1001:1001"


@dataclass(frozen=True)
class Limits:
    cpu: float = 0.5
    memory: int = 512_000_000
    work: int = 64_000_000
    temporary: int = 8_000_000
    pids: int = 64
    collected: int = 1_048_576
    files: int = 256
    collector_cpu: float = 0.1
    collector_memory: int = 64_000_000

    def __post_init__(self) -> None:
        if not 0 < self.cpu <= 1 or not 0 < self.memory <= 8_000_000_000:
            raise ValueError("invalid_resource_limits")
        if min(self.work, self.temporary, self.pids, self.collected, self.files) <= 0:
            raise ValueError("invalid_resource_limits")
        if self.work + self.temporary >= self.memory:
            raise ValueError("tmpfs_exceeds_memory_budget")
        if (
            not 0 < self.collector_cpu < self.cpu
            or not 0 < self.collector_memory < self.memory
        ):
            raise ValueError("invalid_collector_budget")
        if self.work + self.temporary >= self.memory - self.collector_memory:
            raise ValueError("tmpfs_exceeds_sandbox_budget")

    def docker_args(self) -> list[str]:
        return [
            "--network",
            "none",
            "--ipc",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(self.cpu - self.collector_cpu),
            "--memory",
            str(self.memory - self.collector_memory),
            "--memory-swap",
            str(self.memory - self.collector_memory),
            "--pids-limit",
            str(self.pids),
            "--log-driver",
            "none",
            "--tmpfs",
            f"/work:rw,nosuid,nodev,size={self.work},uid=1000,gid=1000,mode=0700",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={self.temporary},mode=1777",
        ]

    def collector_args(self) -> list[str]:
        return [
            "--network",
            "none",
            "--ipc",
            "none",
            "--read-only",
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
            "--cpus",
            str(self.collector_cpu),
            "--memory",
            str(self.collector_memory),
            "--memory-swap",
            str(self.collector_memory),
            "--pids-limit",
            "8",
            "--log-driver",
            "none",
            "--user",
            "0:0",
        ]
