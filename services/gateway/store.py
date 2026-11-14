"""Журнал принятия: идемпотентность сохраняется до явного удаления истории."""

import json
from datetime import UTC, datetime
from pathlib import Path

from contracts import fingerprint, validate
from services.common.storage import Storage

RESERVE_BYTES = 8 * 1024 * 1024
FINAL_BYTES = 512 * 1024


def now() -> str:
    return datetime.now(UTC).isoformat()


class Conflict(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class RunStore:
    def __init__(self, path: Path, limit: int) -> None:
        self.disk = Storage(path, limit)
        self.path = path

    def records(self) -> list[dict]:
        return [self.get(p.parent.name) for p in sorted(self.path.glob("*/state.json"))]

    def get(self, run_id: str) -> dict:
        validate("cancel", {"version": "v0", "run_id": run_id})
        state = json.loads((self.path / run_id / "state.json").read_text())
        terminal = self.path / run_id / "result.json"
        if terminal.exists():
            result = json.loads(terminal.read_text())
            state = {
                **state,
                "state": "terminal",
                "result": result,
                "updated_at": result["finished_at"],
            }
        return state

    def busy(self) -> bool:
        return any(
            r["state"] != "terminal" or r["result"]["cleanup_status"] != "success"
            for r in self.records()
        )

    def accept(self, launch: dict) -> tuple[dict, bool]:
        digest = fingerprint(launch)
        try:
            previous = self.get(launch["run_id"])
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if previous["fingerprint"] != digest:
                raise Conflict("conflict")
            return previous, False
        if self.busy():
            raise Busy("busy")
        # Общий резерв учитывает request, копии, журнал и итог; финальный файл
        # выделяется отдельно и остаётся доступен при исчерпании остальной квоты.
        directory = self.path / launch["run_id"]
        state = {
            "version": "v0",
            "run_id": launch["run_id"],
            "task_id": launch["task_id"],
            "fingerprint": digest,
            "state": "accepted",
            "updated_at": now(),
            "result": None,
        }
        if directory.exists():
            raise Busy("acceptance_recovery_required")
        reservations = []
        try:
            reservations.append(self.disk.reserve(launch["run_id"], RESERVE_BYTES))
            reservations.append(
                self.disk.reserve(launch["run_id"] + "-final", FINAL_BYTES)
            )
            directory.mkdir(mode=0o700)
            self.write(launch["run_id"], "request.json", launch)
            self.write(launch["run_id"], "state.json", state)
        except (OSError, RuntimeError):
            if not (directory / "state.json").exists():
                for path in reservations:
                    path.unlink(missing_ok=True)
                for name in (
                    "request.json",
                    "request.json.pending",
                    "state.json.pending",
                ):
                    (directory / name).unlink(missing_ok=True)
                if directory.exists():
                    directory.rmdir()
            raise
        return state, True

    def update(self, run_id: str, phase: str) -> dict:
        state = self.get(run_id)
        if state["state"] == "terminal":
            return state
        state = {**state, "state": phase, "updated_at": now()}
        validate("status", state)
        self.write(run_id, "state.json", state)
        return state

    def write(self, run_id: str, filename: str, value: object) -> None:
        self.disk.write_reserved(
            self.path / (run_id + ".reserve"), self.path / run_id / filename, value
        )

    def finish(self, result: dict) -> dict:
        validate("result", result)
        run_id = result["run_id"]
        previous = self.get(run_id)
        if previous["state"] == "terminal":
            return previous
        # Восстановление читает result.json даже при аварии до обновления state.
        destination = self.path / run_id / "result.json"
        if not destination.exists():
            self.disk.finalize(
                self.path / (run_id + "-final.reserve"), destination, result
            )
        (self.path / (run_id + ".reserve")).unlink(missing_ok=True)
        return self.get(run_id)
