"""Границы файлового доступа по настоящему SFTP v3 на loopback."""

import asyncio
import os
from contextlib import aclosing
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from client import export as export_module
from client.export import WorkspaceExport


def public_key(private_key: asyncssh.SSHKey) -> str:
    return private_key.export_public_key().decode("ascii").strip()


async def connect(
    export: WorkspaceExport, key: asyncssh.SSHKey, *, host_key: str | None = None
) -> asyncssh.SSHClientConnection:
    known = asyncssh.import_known_hosts(
        f"[127.0.0.1]:{export.port} {host_key or export.host_public_key}"
    )
    return await asyncssh.connect(
        "127.0.0.1",
        port=export.port,
        username="export",
        client_keys=[key],
        known_hosts=known,
        config=None,
        agent_path=None,
        login_timeout=5,
    )


def test_rw_keeps_protected_input_hidden_entries_and_outside_files_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (root / "alias").symlink_to(outside)
    (root / "parent-alias").symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, root / "hard-alias")
    (root / "data" / "input").mkdir(parents=True)
    (root / "data" / "input" / "stats.py").write_bytes(b"protected")
    (root / "vcs" / ".git").mkdir(parents=True)
    (root / "binary").write_bytes(b"\xff\x00\xc0")

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        export = WorkspaceExport(
            root, mode="rw", write_limit=128, protected_paths=["data/input"]
        )
        async with export:
            assert await export.start(public_key(key)) == export.port
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client(sftp_version=3) as sftp,
            ):
                assert set(await sftp.listdir("/")) == {"data", "vcs", "binary"}
                assert await sftp.listdir("vcs") == []
                async with sftp.open("binary", "rb") as binary:
                    assert await binary.read() == b"\xff\x00\xc0"
                async with sftp.open("data/input/stats.py", "rb") as protected:
                    assert await protected.read() == b"protected"
                for path in (
                    "/../outside.txt",
                    "parent-alias/outside.txt",
                    "alias",
                    "hard-alias",
                    "vcs/.GIT",
                    "data/../binary",
                ):
                    with pytest.raises(asyncssh.SFTPError):
                        await sftp.stat(path)
                for path in ("alias", "hard-alias", "data/input/stats.py"):
                    with pytest.raises(asyncssh.SFTPError):
                        await sftp.open(path, "wb")
                for old, new in (("data", "renamed"), ("vcs", "renamed-vcs")):
                    with pytest.raises(asyncssh.SFTPError):
                        await sftp.posix_rename(old, new)
                async with sftp.open("draft", "wb") as draft:
                    await draft.write(b"answer")
                with pytest.raises(asyncssh.SFTPError):
                    await sftp.symlink("draft", "copy")
                with pytest.raises(asyncssh.SFTPError):
                    await sftp.link("draft", "copy")
                with pytest.raises(asyncssh.SFTPError):
                    await sftp.rename("draft", "binary")
                await sftp.rename("draft", "answer")
        assert await export.close() is True

    asyncio.run(run())
    assert outside.read_bytes() == b"outside"
    assert (root / "data" / "input" / "stats.py").read_bytes() == b"protected"
    assert (root / "binary").read_bytes() == b"\xff\x00\xc0"
    assert (root / "answer").read_bytes() == b"answer"


def test_ro_requires_pinned_host_key_and_exact_client_key(tmp_path: Path) -> None:
    (tmp_path / "original").write_bytes(b"safe")

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        wrong = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(tmp_path, mode="ro", write_limit=0) as export:
            await export.start(public_key(key))
            with pytest.raises(asyncssh.PermissionDenied):
                await connect(export, wrong)
            with pytest.raises(asyncssh.HostKeyNotVerifiable):
                await connect(export, key, host_key=public_key(wrong))
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client() as sftp,
            ):
                async with sftp.open("original", "rb") as file:
                    assert await file.read() == b"safe"
                    with pytest.raises(asyncssh.SFTPError):
                        await file.setstat(asyncssh.SFTPAttrs(size=0))
                with pytest.raises(asyncssh.SFTPError):
                    await sftp.open("original", "wb")
                with pytest.raises(asyncssh.SFTPError):
                    await sftp.remove("original")
                with pytest.raises(asyncssh.SFTPError):
                    await sftp.mkdir("created")
                assert await export.close() is True
                await asyncio.wait_for(ssh.wait_closed(), timeout=1)

    asyncio.run(run())
    assert (tmp_path / "original").read_bytes() == b"safe"
    assert not (tmp_path / "created").exists()


def test_write_limit_covers_sparse_extensions_rewrites_and_new_objects(
    tmp_path: Path,
) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"ab")

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(
            tmp_path, mode="rw", write_limit=8, entry_limit=1
        ) as export:
            await export.start(public_key(key))
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client() as sftp,
            ):
                async with sftp.open("file", "r+b") as file:
                    await file.write(b"end", offset=5)
                    await file.write(b"XY", offset=0)
                    with pytest.raises(asyncssh.SFTPError, match="лимит"):
                        await file.setstat(asyncssh.SFTPAttrs(size=9))
                    with pytest.raises(asyncssh.SFTPError, match="лимит"):
                        await file.write(b"Q", offset=0)
                    assert await file.read(offset=0) == b"XY\0\0\0end"
                await sftp.mkdir("once")
                await sftp.rmdir("once")
                with pytest.raises(asyncssh.SFTPError, match="лимит"):
                    await sftp.open("empty", "wb")
            assert await export.close() is True

    asyncio.run(run())
    assert target.read_bytes() == b"XY\0\0\0end"


def test_stale_handle_after_local_replace_cannot_write_to_old_inode(
    tmp_path: Path,
) -> None:
    (tmp_path / "file").write_bytes(b"old")

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(tmp_path, mode="rw", write_limit=32) as export:
            await export.start(public_key(key))
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client() as sftp,
            ):
                async with sftp.open("file", "r+b") as file:
                    (tmp_path / "new").write_bytes(b"local edit")
                    os.replace(tmp_path / "new", tmp_path / "file")
                    with pytest.raises(asyncssh.SFTPError):
                        await file.write(b"agent edit")

    asyncio.run(run())
    assert (tmp_path / "file").read_bytes() == b"local edit"


def test_partial_local_write_yields_unconfirmed_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = os.pwrite

    def partial(fd: int, data: bytes, offset: int) -> int:
        return write(fd, data[:1], offset)

    monkeypatch.setattr(os, "pwrite", partial)

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(tmp_path, mode="rw", write_limit=32) as export:
            await export.start(public_key(key))
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client() as sftp,
            ):
                async with sftp.open("file", "wb") as file:
                    with pytest.raises(asyncssh.SFTPError, match="Неполная запись"):
                        await file.write(b"foo")
                assert await export.close() is False
                assert await export.close() is False

    asyncio.run(run())
    assert (tmp_path / "file").read_bytes() == b"f"


def test_overlapping_root_listings_each_see_all_200_entries(tmp_path: Path) -> None:
    expected = {f"file-{number:03d}-" + "x" * 90 for number in range(200)}
    for name in expected:
        (tmp_path / name).write_bytes(b"")

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(tmp_path, mode="ro", write_limit=0) as export:
            await export.start(public_key(key))
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client() as sftp,
            ):
                listings = await asyncio.gather(sftp.listdir("/"), sftp.listdir("/"))
                for listing in listings:
                    assert len(listing) == 200
                    assert set(listing) == expected

    asyncio.run(run())


def test_git_cannot_be_selected_by_path_or_alias(tmp_path: Path) -> None:
    internal = tmp_path / ".git" / "objects"
    internal.mkdir(parents=True)
    alias = tmp_path / "ordinary-name"
    alias.symlink_to(internal, target_is_directory=True)

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        for excluded in (internal, alias):
            async with WorkspaceExport(excluded, mode="ro", write_limit=0) as export:
                with pytest.raises(ValueError, match=r"\.git"):
                    await export.start(public_key(key))

    asyncio.run(run())


def test_directory_handle_counts_against_limit_and_rejects_local_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    directory = root / "directory"
    directory.mkdir(parents=True)
    names = {f"file-{number:03d}" for number in range(200)}
    for name in names:
        (directory / name).write_bytes(b"")
    (root / "available").write_bytes(b"safe")
    monkeypatch.setattr(export_module, "_MAX_OPEN_HANDLES", 1)

    async def run() -> None:
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(root, mode="ro", write_limit=0) as export:
            await export.start(public_key(key))
            async with (
                await connect(export, key) as ssh,
                ssh.start_sftp_client() as sftp,
            ):
                async with aclosing(sftp.scandir("directory")) as entries:
                    assert (await anext(entries)).filename in names
                    with pytest.raises(asyncssh.SFTPError, match="лимит дескрипторов"):
                        await sftp.open("available", "rb")
                    directory.rename(tmp_path / "moved")
                    directory.mkdir()
                    (tmp_path / "moved" / "private").write_bytes(b"outside")
                    with pytest.raises(asyncssh.SFTPError, match="изменён локально"):
                        async for entry in entries:
                            assert entry.filename in names
                async with sftp.open("available", "rb") as available:
                    assert await available.read() == b"safe"

    asyncio.run(run())


def test_cancel_during_start_revokes_listener_and_prevents_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_create = asyncssh.create_server

    async def run() -> None:
        bound = asyncio.Event()
        resume = asyncio.Event()
        port = 0

        async def create_server(*args: Any, **kwargs: Any) -> asyncssh.SSHAcceptor:
            nonlocal port
            acceptor = await original_create(*args, **kwargs)
            port = acceptor.get_port()
            bound.set()
            await resume.wait()
            return acceptor

        monkeypatch.setattr(asyncssh, "create_server", create_server)
        key = asyncssh.generate_private_key("ssh-ed25519")
        async with WorkspaceExport(tmp_path, mode="rw", write_limit=32) as export:
            starting = asyncio.create_task(export.start(public_key(key)))
            await asyncio.wait_for(bound.wait(), timeout=1)
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting
            with pytest.raises(RuntimeError, match="закрыт"):
                await export.start(public_key(key))
            resume.set()
            assert await export.close() is True
            with pytest.raises(ConnectionRefusedError):
                await asyncio.open_connection("127.0.0.1", port)

    asyncio.run(run())
