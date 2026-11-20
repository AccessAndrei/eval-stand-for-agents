import asyncio
import hashlib
import json
import shlex
import sys
import tomllib
from pathlib import Path

import pytest

from contracts import validate
from contracts.tool_evidence import B5_COMMAND, B5_COMMAND_KIND, B5_CWD
from services.harness.adapter import Adapter
from services.harness.rpc import RpcError
from services.harness.settings import bind_recovery_owner, configure


def test_settings_exclude_personal_and_local_environment(tmp_path: Path) -> None:
    home = tmp_path / "attempt"
    env = configure(
        home,
        manager_url="http://manager:8082",
        manager_token="manager-test",
        project_id="project",
        handle="handle",
        run_id="run",
        proxy_url="http://proxy/v0/runs/run",
        inference_token="inference-test",
        model="mock-model",
    )
    settings = tomllib.loads((home / "environments.toml").read_text())
    config = tomllib.loads((home / "config.toml").read_text())
    assert settings["include_local"] is False
    assert settings["default"] == "sandbox"
    assert config["shell_environment_policy"] == {
        "inherit": "none",
        "set": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"},
    }
    assert "MANAGER_TOKEN" not in env
    assert "inference-test" not in json.dumps(config)
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
    bind_recovery_owner(home, {"EVAL_STAND_ATTEMPT_OWNER": "a" * 64})
    relay = tomllib.loads((home / "environments.toml").read_text())["environments"][0][
        "env"
    ]
    assert relay["CODEX_HOME"] == str(home)
    assert relay["EVAL_STAND_ATTEMPT_OWNER"] == "a" * 64
    assert (home / "environments.toml").stat().st_mode & 0o777 == 0o600


def test_spawn_failure_releases_empty_slot(tmp_path: Path) -> None:
    adapter = Adapter(str(tmp_path / "missing-codex"))
    result = asyncio.run(adapter.run("test", "mock-model", tmp_path, {}, 1))
    assert result["released"] is True
    assert result["execution_status"] == "transport_error"
    assert result["evidence_complete"] is False


def test_cancel_during_version_check_never_starts_app_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = Adapter()

    async def version(env: dict) -> None:
        await adapter.cancel()

    async def forbidden_spawn(*args, **kwargs) -> None:
        raise AssertionError("app-server started after cancellation")

    monkeypatch.setattr(adapter, "check_version", version)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    result = asyncio.run(adapter.run("task", "mock-codex", tmp_path, {}, 10))
    assert result["execution_status"] == "cancelled"
    assert result["released"] is True


def test_cancel_after_thread_start_never_sends_turn() -> None:
    adapter = Adapter()
    methods = []

    class Rpc:
        async def call(self, method: str, params: dict) -> dict:
            methods.append(method)
            if method == "thread/start":
                adapter.cancel_requested = True
                return {"thread": {"id": "thread"}}
            return {}

        async def send(self, message: dict) -> None:
            pass

    adapter.rpc = Rpc()
    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(adapter._initialize("task", "mock-codex"))
    assert "turn/start" not in methods


def test_cancel_before_resumed_turn_preserves_committed_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = Adapter(mounted=True, resume_thread_id="thread-a")

    async def version(env: dict) -> None:
        await adapter.cancel()

    async def forbidden_spawn(*args, **kwargs) -> None:
        raise AssertionError("app-server started after cancellation")

    monkeypatch.setattr(adapter, "check_version", version)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    result = asyncio.run(adapter.run("next", "mock", tmp_path, {}, 10))
    assert result["thread_id"] == "thread-a"
    assert result["turn_id"] is None
    assert result["released"] and result["continuation_safe"]
    assert result["execution_status"] == "cancelled"


def native_message(thread: str, turn: str, message: str) -> dict:
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "item": {"type": "agentMessage", "id": message, "text": message},
        },
    }


def native_completion(thread: str, turn: str, status: str) -> dict:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread,
            "turn": {"id": turn, "status": status},
        },
    }


def test_resume_keeps_only_current_turn_events_before_and_after_ack() -> None:
    adapter = Adapter(mounted=True, resume_thread_id="thread-a")
    requests: list[tuple[str, dict]] = []

    class Rpc:
        async def call(self, method: str, params: dict) -> dict:
            requests.append((method, params))
            if method == "thread/resume":
                await adapter.on_event(
                    native_completion("thread-a", "turn-a", "interrupted")
                )
                return {"thread": {"id": "thread-a"}}
            if method == "turn/start":
                await adapter.on_event(native_message("thread-a", "turn-a", "history"))
                await adapter.on_event(native_message("thread-a", "turn-b", "new"))
                return {"turn": {"id": "turn-b"}}
            return {}

        async def send(self, message: dict) -> None:
            pass

    async def scenario() -> None:
        adapter.rpc = Rpc()
        await adapter._initialize("resume", "mock")
        assert not adapter.finished.is_set()
        await adapter.on_event(native_completion("thread-a", "turn-a", "failed"))
        await adapter.on_event(native_message("thread-other", "turn-b", "foreign"))
        assert not adapter.finished.is_set()
        await adapter.on_event(native_completion("thread-a", "turn-b", "completed"))
        await adapter.on_event(native_message("thread-a", "turn-b", "after-terminal"))

    asyncio.run(scenario())
    assert requests[1] == (
        "thread/resume",
        {
            "threadId": "thread-a",
            "model": "mock",
            "modelProvider": "stand",
            "approvalPolicy": "never",
            "cwd": "/work",
            "environments": [{"environmentId": "sandbox", "cwd": "/work"}],
            "excludeTurns": True,
        },
    )
    assert adapter.finished.is_set() and adapter.execution_status == "success"
    assert [message["text"] for message in adapter.messages] == ["new"]


def test_resume_rejects_replaced_thread_before_dispatch() -> None:
    adapter = Adapter(mounted=True, resume_thread_id="thread-a")
    methods = []

    class Rpc:
        async def call(self, method: str, params: dict) -> dict:
            methods.append(method)
            return (
                {"thread": {"id": "wrong-thread"}} if method == "thread/resume" else {}
            )

        async def send(self, message: dict) -> None:
            pass

    adapter.rpc = Rpc()
    with pytest.raises(RpcError, match="thread не подтверждён"):
        asyncio.run(adapter._initialize("resume", "mock"))
    assert methods == ["initialize", "thread/resume"]


def mock_codex(path: Path, *, exit_code: int) -> Path:
    executable = path / "mock-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """\
import json
import subprocess
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.159.3")
    raise SystemExit

assert sys.argv[1:] == ["app-server", "--listen", "stdio://"]
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    process_group=0, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

try:
    with Path("methods.jsonl").open("w") as methods:
        for line in sys.stdin:
            request = json.loads(line)
            if "id" not in request:
                continue
            methods.write(line)
            methods.flush()
            method = request["method"]
            result = {}
            if method == "thread/start":
                result = {"thread": {"id": "thread-a"}}
            elif method == "turn/start":
                result = {"turn": {"id": "turn-a"}}
            print(json.dumps({"id": request["id"], "result": result}), flush=True)
            if method == "turn/interrupt":
                if Path("transcript-save-fails").exists():
                    print(json.dumps({
                        "method": "warning",
                        "params": {
                            "threadId": "thread-a",
                            "message": "Failed to save the conversation transcript; No space left on device",
                        },
                    }), flush=True)
                print(json.dumps({
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-a",
                        "turn": {"id": "turn-a", "status": "interrupted"}
                    },
                }), flush=True)
finally:
    child.terminate()
    child.wait()
"""
        + f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize(
    ("exit_code", "transcript_failure", "continuation_safe"),
    [(0, False, True), (3, False, False), (0, True, False)],
)
def test_mounted_cancel_interrupts_once_then_drains_app_and_relay(
    tmp_path: Path, exit_code: int, transcript_failure: bool, continuation_safe: bool
) -> None:
    from services.common.processes import session_members

    adapter = Adapter(str(mock_codex(tmp_path, exit_code=exit_code)), mounted=True)
    if transcript_failure:
        (tmp_path / "transcript-save-fails").touch()

    async def scenario() -> dict:
        run = asyncio.create_task(adapter.run("first", "mock", tmp_path, {}, 12))
        try:
            async with asyncio.timeout(4):
                while adapter.turn_id is None:
                    await asyncio.sleep(0.01)
            assert adapter.process is not None
            session_id = adapter.process.pid
            assert len(await session_members(session_id)) == 2
            await asyncio.gather(adapter.cancel(), adapter.cancel())
            result = await run
            assert await session_members(session_id) == []
            return result
        finally:
            if not run.done():
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    result = asyncio.run(scenario())
    requests = [
        json.loads(line)
        for line in (tmp_path / "methods.jsonl").read_text().splitlines()
    ]
    assert [request["method"] for request in requests] == [
        "initialize",
        "thread/start",
        "turn/start",
        "turn/interrupt",
    ]
    assert requests[1]["params"]["ephemeral"] is False
    assert requests[-1]["params"] == {"threadId": "thread-a", "turnId": "turn-a"}
    assert result["execution_status"] == "cancelled"
    assert result["released"]
    assert result["continuation_safe"] is continuation_safe
    assert "No space left on device" not in json.dumps(result)


def test_mounted_completion_after_timeout_does_not_authorize_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = Adapter(str(mock_codex(tmp_path, exit_code=0)), mounted=True)
    close = adapter._close_app_server

    async def watchdog() -> None:
        raise TimeoutError

    async def close_with_delayed_completion(
        process: asyncio.subprocess.Process,
    ) -> bool:
        await adapter.on_event(native_completion("thread-a", "turn-a", "completed"))
        return await close(process)

    monkeypatch.setattr(adapter, "_watch_tools", watchdog)
    monkeypatch.setattr(adapter, "_close_app_server", close_with_delayed_completion)
    result = asyncio.run(adapter.run("test", "mock", tmp_path, {}, 12))
    assert result["turn_id"] == "turn-a"
    assert result["execution_status"] == "timeout"
    assert result["released"] and not result["continuation_safe"]


def test_relay_separate_group_is_stopped_after_parent_exit(tmp_path: Path) -> None:
    import os
    import sys

    from services.common.processes import session_members, stop_session

    async def scenario() -> None:
        child_file = tmp_path / "child.pid"
        child = (
            "import os,signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"open({str(child_file)!r},'w').write(str(os.getpid()));"
            "time.sleep(30)"
        )
        parent = (
            "import subprocess,sys;"
            f"subprocess.Popen([sys.executable,'-c',{child!r}],process_group=0)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent,
            start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await process.wait()
            async with asyncio.timeout(3):
                while not child_file.exists() or not child_file.read_text():
                    await asyncio.sleep(0.01)
            child_pid = int(child_file.read_text())
            assert os.getpgid(child_pid) != process.pid
            assert child_pid in await session_members(process.pid)
            assert await stop_session(process, 3)
            assert await session_members(process.pid) == []
        finally:
            await stop_session(process, 2)
            if child_file.exists():
                try:
                    pid = int(child_file.read_text())
                    if os.getsid(pid) == process.pid:
                        os.kill(pid, 9)
                except ProcessLookupError:
                    pass

    asyncio.run(scenario())


def test_native_output_redaction_and_evidence_independent_of_stdout() -> None:
    adapter = Adapter(secrets=("private-test-token",))

    async def scenario() -> None:
        for phase in ("started", "completed"):
            await adapter.on_event(
                {
                    "method": "item/" + phase,
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "item": {
                            "id": "item",
                            "type": "commandExecution",
                            "command": "echo ok",
                            "cwd": "/work",
                            "source": "agent",
                            "exitCode": 0 if phase == "completed" else None,
                            "aggregatedOutput": "private-test-token" + "x" * 20000,
                        },
                    },
                }
            )

    asyncio.run(scenario())
    assert adapter.evidence_complete
    assert len(adapter.events) == 2
    assert adapter.traces[-1]["output_truncated"]
    assert "private-test-token" not in json.dumps(adapter.snapshot())
    assert adapter.traces[-1]["output"].startswith("[REDACTED]")
    validate(
        "evidence",
        {
            "version": "v0",
            "codex_version": "0.159.3",
            "run_id": "run",
            "thread_id": "thread",
            "turn_id": "turn",
            "complete": True,
            "protected_inputs": True,
            "input_manifest": {"input/stats.py": "a" * 64, "input/data.csv": "b" * 64},
            "events": [{**event, "run_id": "run"} for event in adapter.events],
        },
    )


def test_mounted_adapter_preserves_structural_evidence_and_separate_message() -> None:
    adapter = Adapter(secrets=("private-token",), mounted=True)
    adapter.thread_id, adapter.turn_id = "thread", "turn"

    async def scenario() -> None:
        for phase in ("started", "completed"):
            await adapter.on_event(
                {
                    "method": f"item/{phase}",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "item": {
                            "id": "provider-call-worktree-canary",
                            "type": "commandExecution",
                            "command": "printf worktree-data",
                            "cwd": "/work/private-path",
                            "aggregatedOutput": "worktree-data",
                            "exitCode": 0,
                        },
                    },
                }
            )
        message = {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "item": {
                    "id": "msg",
                    "type": "agentMessage",
                    "text": "ответ private-token",
                },
            },
        }
        await adapter.on_event(message)
        await adapter.on_event(message)
        adapter.record_diagnostic({"error": "worktree-data"})

    asyncio.run(scenario())
    snapshot = adapter.snapshot()
    assert snapshot.pop("messages") == [
        {"item_id": hashlib.sha256(b"msg").hexdigest(), "text": "ответ [REDACTED]"}
    ]
    assert snapshot["events"][-1] == {
        "type": "commandExecution",
        "phase": "completed",
        "thread_id": "thread",
        "turn_id": "turn",
        "item_id": hashlib.sha256(b"provider-call-worktree-canary").hexdigest(),
        "exit_code": 0,
    }
    assert snapshot["evidence_complete"]
    assert "worktree-data" not in json.dumps(
        snapshot
    ) and "private-path" not in json.dumps(snapshot)
    assert "provider-call-worktree-canary" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    ("command", "cwd", "source", "matches"),
    [
        (B5_COMMAND, B5_CWD, "agent", True),
        (
            shlex.join(["/usr/bin/bash", "-lc", B5_COMMAND]),
            B5_CWD,
            "unifiedExecStartup",
            True,
        ),
        (
            shlex.join(["/usr/bin/bash", "-lc", B5_COMMAND + "; echo 11"]),
            B5_CWD,
            "agent",
            False,
        ),
        (shlex.join(["/bin/bash", "-lc", B5_COMMAND]), B5_CWD, "agent", False),
        (B5_COMMAND.replace(" -I", "\n-I"), B5_CWD, "agent", False),
        (B5_COMMAND + "'", B5_CWD, "agent", False),
        (B5_COMMAND, "/work/subdir", "agent", False),
        (B5_COMMAND, B5_CWD, "userShell", False),
        (B5_COMMAND, B5_CWD, "private-source-text", False),
    ],
)
def test_mounted_b5_marker_follows_exact_native_invocation(
    command: str, cwd: str, source: str, matches: bool
) -> None:
    adapter = Adapter(mounted=True)
    adapter.thread_id, adapter.turn_id = "thread-1", "turn-1"

    async def scenario() -> None:
        for phase in ("started", "completed"):
            await adapter.on_event(
                {
                    "method": f"item/{phase}",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": "native-id",
                            "type": "commandExecution",
                            "command": command,
                            "cwd": cwd,
                            "source": source,
                            "exitCode": 7,
                            "aggregatedOutput": "private-output-text",
                        },
                    },
                }
            )

    asyncio.run(scenario())
    first, second = adapter.snapshot()["events"]
    for phase, event in (("started", first), ("completed", second)):
        assert event["phase"] == phase
        assert "command" not in event and "cwd" not in event
        if matches:
            assert (
                event["command_kind"] == B5_COMMAND_KIND and event["source"] == source
            )
        else:
            assert "command_kind" not in event and "source" not in event
    assert "exit_code" not in first and second["exit_code"] == 7
    snapshot = json.dumps(adapter.snapshot())
    assert "stats.py" not in snapshot
    assert (
        "private-source-text" not in snapshot and "private-output-text" not in snapshot
    )


@pytest.mark.parametrize(
    "model", ["mock-codex", "mock-codex-alt", "arbitrary-provider-model"]
)
def test_catalog_keeps_requested_slug_and_direct_native_tools(
    tmp_path: Path,
    model: str,
) -> None:
    home = tmp_path / "mock"
    configure(
        home,
        manager_url="http://manager:8082",
        manager_token="manager-test",
        project_id="project",
        handle="handle",
        run_id="run",
        proxy_url="http://proxy",
        inference_token="inference-test",
        model=model,
    )
    catalog = json.loads((home / "models.json").read_text())["models"]
    assert [item["slug"] for item in catalog] == [model]
    assert catalog[0]["tool_mode"] == "direct"
    assert catalog[0]["use_responses_lite"] is False
    assert catalog[0]["node_repl_disabled"] is True
    assert catalog[0]["apply_patch_tool_type"] == "freeform"
    assert catalog[0]["shell_type"] == "unified_exec"
