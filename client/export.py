"""Локальный SFTP-экспорт выбранной папки для GUI и headless-клиента."""

import asyncio
import ctypes
import os
import stat
import sys
from collections.abc import AsyncGenerator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from unicodedata import normalize
from weakref import WeakSet

import asyncssh

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
_WRITE_FLAGS = (
    asyncssh.FXF_WRITE
    | asyncssh.FXF_APPEND
    | asyncssh.FXF_CREAT
    | asyncssh.FXF_TRUNC
    | asyncssh.FXF_EXCL
)
_MAX_READ = 256 * 1024
_MAX_OFFSET = (1 << 63) - 1
_MAX_DEPTH = 64
_MAX_RENAME_ENTRIES = 4096
_MAX_OPEN_HANDLES = 256
_POSIX = ctypes.CDLL(None, use_errno=True)

_PathParts = tuple[bytes, ...]
_FoldedParts = tuple[str, ...]


def _hidden(name: bytes) -> bool:
    return _fold(name) == ".git"


def _fold(name: bytes) -> str:
    return normalize("NFC", os.fsdecode(name)).casefold()


def _parts(path: bytes) -> _PathParts:
    if b"\0" in path:
        raise asyncssh.SFTPPermissionDenied("Недопустимый путь")
    parts = tuple(part for part in path.split(b"/") if part not in (b"", b"."))
    if len(parts) > _MAX_DEPTH or any(part == b".." or _hidden(part) for part in parts):
        raise asyncssh.SFTPPermissionDenied("Путь вне экспорта")
    return parts


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _no_replace(src: bytes, dst: bytes, src_fd: int, dst_fd: int) -> None:
    # Обычный SFTP RENAME запрещает замену даже при одновременной правке.
    symbol, flags = (
        ("renameatx_np", 0x00000004) if sys.platform == "darwin" else ("renameat2", 1)
    )
    rename = getattr(_POSIX, symbol, None)
    if rename is None:
        raise asyncssh.SFTPOpUnsupported("Атомарное переименование недоступно")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(src_fd, src, dst_fd, dst, flags):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


class _Tree:
    def __init__(
        self,
        fd: int,
        mode: str,
        write_limit: int,
        entry_limit: int,
        protected: tuple[_FoldedParts, ...],
    ) -> None:
        self.fd = fd
        self.device = os.fstat(fd).st_dev
        self.mode = mode
        self.remaining_bytes = write_limit
        self.remaining_entries = entry_limit
        self.protected = protected
        self.live = True
        self.uncertain = False

    def require_live(self) -> None:
        if not self.live:
            raise asyncssh.SFTPConnectionLost("Экспорт закрыт")

    def permit_write(self, parts: _PathParts) -> None:
        self.require_live()
        if self.mode != "rw":
            raise asyncssh.SFTPPermissionDenied("Экспорт доступен только на чтение")
        folded = tuple(_fold(part) for part in parts)
        if not folded or any(
            folded[: len(path)] == path or path[: len(folded)] == folded
            for path in self.protected
        ):
            raise asyncssh.SFTPPermissionDenied("Защищённый путь")

    def charge(self, size: int) -> None:
        if size > self.remaining_bytes:
            raise asyncssh.SFTPFailure("Исчерпан лимит записи экспорта")
        self.remaining_bytes -= size

    def create_entry(self) -> None:
        if not self.remaining_entries:
            raise asyncssh.SFTPFailure("Исчерпан лимит новых объектов экспорта")
        self.remaining_entries -= 1

    def checked(self, info: os.stat_result) -> os.stat_result:
        mode = info.st_mode
        if info.st_dev != self.device or not (
            stat.S_ISDIR(mode) or (stat.S_ISREG(mode) and info.st_nlink == 1)
        ):
            raise asyncssh.SFTPPermissionDenied("Тип объекта за пределами экспорта")
        return info

    @contextmanager
    def directory(self, parts: _PathParts) -> Iterator[int]:
        self.require_live()
        # dup разделяет позицию чтения каталога между двумя запросами READDIR.
        descriptor = os.open(b".", _DIR_FLAGS, dir_fd=self.fd)
        try:
            for part in parts:
                child = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                self.checked(os.fstat(descriptor))
            yield descriptor
        finally:
            os.close(descriptor)

    def entry(self, fd: int, name: bytes) -> os.stat_result:
        return self.checked(os.stat(name, dir_fd=fd, follow_symlinks=False))

    def info(self, parts: _PathParts) -> os.stat_result:
        if not parts:
            self.require_live()
            return self.checked(os.fstat(self.fd))
        with self.directory(parts[:-1]) as parent:
            return self.entry(parent, parts[-1])

    def require_current(self, parts: _PathParts, previous: os.stat_result) -> None:
        if _identity(previous) != _identity(self.info(parts)):
            raise asyncssh.SFTPFailure("Объект изменён локально")

    @contextmanager
    def open_existing(self, parts: _PathParts, flags: int) -> Iterator[int]:
        with self.directory(parts[:-1]) as parent:
            previous = self.entry(parent, parts[-1])
            descriptor = os.open(parts[-1], _FILE_FLAGS | flags, dir_fd=parent)
            try:
                current = self.checked(os.fstat(descriptor))
                if _identity(previous) != _identity(current):
                    raise asyncssh.SFTPFailure("Объект изменён локально")
                self.require_current(parts, current)
                yield descriptor
            finally:
                os.close(descriptor)

    def check_movable(
        self, parts: _PathParts, destination: _PathParts, info: os.stat_result
    ) -> None:
        if not stat.S_ISDIR(info.st_mode):
            return
        extra_depth = max(0, len(destination) - len(parts))
        stack = [parts]
        remaining = _MAX_RENAME_ENTRIES
        while stack:
            directory = stack.pop()
            with (
                self.directory(directory) as descriptor,
                os.scandir(descriptor) as entries,
            ):
                for entry in entries:
                    name = os.fsencode(entry.name)
                    if _hidden(name):
                        raise asyncssh.SFTPPermissionDenied("Каталог содержит .git")
                    remaining -= 1
                    if remaining < 0 or len(directory) + extra_depth >= _MAX_DEPTH:
                        raise asyncssh.SFTPFailure(
                            "Превышен предел проверки переименования"
                        )
                    status = self.entry(descriptor, name)
                    if stat.S_ISDIR(status.st_mode):
                        stack.append((*directory, name))


@dataclass(eq=False)
class _File:
    fd: int
    parts: _PathParts
    readable: bool
    writable: bool
    append: bool

    def fileno(self) -> int:
        # Расширение ranges обходит обычные методы SFTPServer.
        raise asyncssh.SFTPOpUnsupported("Разреженные диапазоны не поддерживаются")


class _ExportSFTP(asyncssh.SFTPServer):
    def __init__(
        self, channel: asyncssh.SSHServerChannel, export: "WorkspaceExport"
    ) -> None:
        super().__init__(channel)
        self._export = export
        assert export._tree is not None
        self._tree = export._tree
        self._files: set[_File] = set()
        self._scans: WeakSet[AsyncGenerator[asyncssh.SFTPName, None]] = WeakSet()

    def map_path(self, path: bytes) -> bytes:
        # Унаследованные операции не должны получать пути хоста.
        raise asyncssh.SFTPOpUnsupported("Операция с путём не поддерживается")

    def _file(self, file_obj: object) -> _File:
        if not isinstance(file_obj, _File) or file_obj not in self._files:
            raise asyncssh.SFTPInvalidHandle("Недействительный дескриптор экспорта")
        return file_obj

    def _fresh(self, file: _File) -> os.stat_result:
        status = self._tree.checked(os.fstat(file.fd))
        self._tree.require_current(file.parts, status)
        return status

    def _require_handle(self) -> None:
        count = sum(
            len(peer._files) + len(peer._scans) for peer in self._export._sessions
        )
        if count >= _MAX_OPEN_HANDLES:
            raise asyncssh.SFTPFailure("Исчерпан лимит дескрипторов экспорта")

    def open(self, path: bytes, pflags: int, attrs: asyncssh.SFTPAttrs) -> _File:
        parts = _parts(path)
        readable = bool(pflags & asyncssh.FXF_READ)
        writable = bool(pflags & asyncssh.FXF_WRITE)
        if not parts:
            raise asyncssh.SFTPFailure("Не удалось открыть файл экспорта")
        self._require_handle()
        if (
            pflags & ~(asyncssh.FXF_READ | _WRITE_FLAGS)
            or (not readable and not writable)
            or (pflags & _WRITE_FLAGS and not writable)
        ):
            raise asyncssh.SFTPFailure("Недопустимые флаги SFTP OPEN")
        if writable:
            self._tree.permit_write(parts)
        flags = _FILE_FLAGS | (
            os.O_RDWR
            if readable and writable
            else os.O_WRONLY
            if writable
            else os.O_RDONLY
        )
        append = bool(pflags & asyncssh.FXF_APPEND)
        if append:
            flags |= os.O_APPEND
        permissions = 0o666 if attrs.permissions is None else attrs.permissions & 0o777
        with self._tree.directory(parts[:-1]) as parent:
            leaf = parts[-1]
            try:
                previous = self._tree.entry(parent, leaf)
            except FileNotFoundError:
                if not pflags & asyncssh.FXF_CREAT:
                    raise
                self._tree.create_entry()
                previous = None
                flags |= os.O_CREAT | os.O_EXCL
            else:
                if pflags & asyncssh.FXF_EXCL:
                    raise asyncssh.SFTPFileAlreadyExists("Файл уже существует")
                if not stat.S_ISREG(previous.st_mode):
                    raise asyncssh.SFTPPermissionDenied("Ожидается обычный файл")
            descriptor = os.open(leaf, flags, permissions, dir_fd=parent)
            try:
                file = _File(descriptor, parts, readable, writable, append)
                current = self._fresh(file)
                if previous is not None and _identity(previous) != _identity(current):
                    raise asyncssh.SFTPFailure("Объект изменён локально")
                if pflags & asyncssh.FXF_TRUNC:
                    try:
                        os.ftruncate(descriptor, 0)
                    except OSError:
                        self._tree.uncertain = True
                        raise
            except BaseException:
                os.close(descriptor)
                raise
        self._files.add(file)
        return file

    def read(self, file_obj: object, offset: int, size: int) -> bytes:
        file = self._file(file_obj)
        if not file.readable or offset > _MAX_OFFSET:
            raise asyncssh.SFTPPermissionDenied("Чтение файла отклонено")
        self._fresh(file)
        return os.pread(file.fd, min(size, _MAX_READ), offset)

    def write(self, file_obj: object, offset: int, data: bytes) -> int:
        file = self._file(file_obj)
        self._tree.permit_write(file.parts)
        if not file.writable or offset > _MAX_OFFSET - len(data):
            raise asyncssh.SFTPPermissionDenied("Запись файла отклонена")
        status = self._fresh(file)
        if not data:
            return 0
        self._tree.charge(
            len(data)
            if file.append
            else max(len(data), offset + len(data) - status.st_size)
        )
        try:
            count = (
                os.write(file.fd, data)
                if file.append
                else os.pwrite(file.fd, data, offset)
            )
        except OSError:
            self._tree.uncertain = True
            raise
        if count != len(data):
            self._tree.uncertain = True
            raise asyncssh.SFTPFailure("Неполная запись в файл")
        return count

    def close(self, file_obj: object) -> None:
        if not isinstance(file_obj, _File) or file_obj not in self._files:
            return
        self._files.remove(file_obj)
        try:
            os.close(file_obj.fd)
        except OSError:
            self._tree.uncertain = True
            raise

    def stat(self, path: bytes) -> os.stat_result:
        return self._tree.info(_parts(path))

    lstat = stat

    def fstat(self, file_obj: object) -> os.stat_result:
        return self._fresh(self._file(file_obj))

    def realpath(self, path: bytes) -> bytes:
        parts = _parts(path)
        self._tree.info(parts)
        return b"/" + b"/".join(parts)

    def fsync(self, file_obj: object) -> None:
        file = self._file(file_obj)
        self._fresh(file)
        try:
            os.fsync(file.fd)
        except OSError:
            if file.writable:
                self._tree.uncertain = True
            raise

    def _set_attrs(
        self, fd: int, info: os.stat_result, attrs: asyncssh.SFTPAttrs
    ) -> None:
        if attrs.uid is not None or attrs.gid is not None or attrs.extended:
            raise asyncssh.SFTPOpUnsupported(
                "Изменение владельца или расширенных атрибутов запрещено"
            )
        if attrs.size is not None:
            if not stat.S_ISREG(info.st_mode) or not 0 <= attrs.size <= _MAX_OFFSET:
                raise asyncssh.SFTPFailure("Недопустимый размер файла")
            self._tree.charge(max(0, attrs.size - info.st_size))
        try:
            if attrs.size is not None:
                os.ftruncate(fd, attrs.size)
            if attrs.permissions is not None:
                os.fchmod(fd, attrs.permissions & 0o777)
            if attrs.atime is not None and attrs.mtime is not None:
                os.utime(fd, ns=(attrs.atime * 10**9, attrs.mtime * 10**9))
        except OSError:
            self._tree.uncertain = True
            raise

    def setstat(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> None:
        parts = _parts(path)
        self._tree.permit_write(parts)
        flags = os.O_WRONLY if attrs.size is not None else os.O_RDONLY
        with self._tree.open_existing(parts, flags) as descriptor:
            self._set_attrs(descriptor, os.fstat(descriptor), attrs)

    lsetstat = setstat

    def fsetstat(self, file_obj: object, attrs: asyncssh.SFTPAttrs) -> None:
        file = self._file(file_obj)
        self._tree.permit_write(file.parts)
        if not file.writable:
            raise asyncssh.SFTPPermissionDenied("Дескриптор открыт только на чтение")
        self._set_attrs(file.fd, self._fresh(file), attrs)

    def scandir(self, path: bytes) -> AsyncGenerator[asyncssh.SFTPName, None]:
        parts = _parts(path)
        status = self._tree.info(parts)
        if not stat.S_ISDIR(status.st_mode):
            raise asyncssh.SFTPNotADirectory("Ожидается каталог")
        self._require_handle()
        scan = self._scan(parts, status)
        self._scans.add(scan)
        return scan

    async def _scan(
        self, parts: _PathParts, status: os.stat_result
    ) -> AsyncGenerator[asyncssh.SFTPName, None]:
        with (
            self._tree.directory(parts) as descriptor,
            os.scandir(descriptor) as entries,
        ):
            if _identity(status) != _identity(os.fstat(descriptor)):
                raise asyncssh.SFTPFailure("Каталог изменён локально")
            for entry in entries:
                self._tree.require_current(parts, status)
                name = os.fsencode(entry.name)
                if _hidden(name):
                    continue
                try:
                    info = self._tree.entry(descriptor, name)
                except (FileNotFoundError, asyncssh.SFTPPermissionDenied):
                    continue
                yield asyncssh.SFTPName(name, attrs=asyncssh.SFTPAttrs.from_local(info))

    def format_longname(self, name: asyncssh.SFTPName) -> None:
        name.longname = name.filename

    def statvfs(self, path: bytes) -> asyncssh.SFTPVFSAttrs:
        self._tree.info(_parts(path))
        return self._capacity()

    def fstatvfs(self, file_obj: object) -> asyncssh.SFTPVFSAttrs:
        self._fresh(self._file(file_obj))
        return self._capacity()

    def _capacity(self) -> asyncssh.SFTPVFSAttrs:
        attrs = asyncssh.SFTPVFSAttrs.from_local(os.fstatvfs(self._tree.fd))
        readonly = self._tree.mode == "ro"
        available = 0 if readonly else self._tree.remaining_bytes
        entries = 0 if readonly else self._tree.remaining_entries
        limit = available // max(1, attrs.frsize)
        attrs.bfree = min(attrs.bfree, limit)
        attrs.bavail = min(attrs.bavail, limit)
        attrs.ffree = min(attrs.ffree, entries)
        attrs.favail = min(attrs.favail, entries)
        if readonly:
            attrs.flags |= os.ST_RDONLY
        return attrs

    def mkdir(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> None:
        parts = _parts(path)
        self._tree.permit_write(parts)
        permissions = 0o777 if attrs.permissions is None else attrs.permissions & 0o777
        with self._tree.directory(parts[:-1]) as parent:
            self._tree.create_entry()
            os.mkdir(parts[-1], permissions, dir_fd=parent)

    def remove(self, path: bytes) -> None:
        parts = _parts(path)
        self._tree.permit_write(parts)
        with self._tree.directory(parts[:-1]) as parent:
            self._tree.entry(parent, parts[-1])
            os.unlink(parts[-1], dir_fd=parent)

    def rmdir(self, path: bytes) -> None:
        parts = _parts(path)
        self._tree.permit_write(parts)
        with self._tree.directory(parts[:-1]) as parent:
            self._tree.entry(parent, parts[-1])
            os.rmdir(parts[-1], dir_fd=parent)

    def rename(self, oldpath: bytes, newpath: bytes) -> None:
        self._rename(oldpath, newpath, overwrite=False)

    def posix_rename(self, oldpath: bytes, newpath: bytes) -> None:
        self._rename(oldpath, newpath, overwrite=True)

    def _rename(self, oldpath: bytes, newpath: bytes, *, overwrite: bool) -> None:
        old, new = _parts(oldpath), _parts(newpath)
        self._tree.permit_write(old)
        self._tree.permit_write(new)
        with (
            self._tree.directory(old[:-1]) as src,
            self._tree.directory(new[:-1]) as dst,
        ):
            source = self._tree.entry(src, old[-1])
            self._tree.check_movable(old, new, source)
            self._tree.require_current(old, source)
            self._tree.require_current(new[:-1], os.fstat(dst))
            if overwrite:
                try:
                    self._tree.entry(dst, new[-1])
                except FileNotFoundError:
                    pass
                os.replace(old[-1], new[-1], src_dir_fd=src, dst_dir_fd=dst)
            else:
                _no_replace(old[-1], new[-1], src, dst)

    async def exit(self) -> None:
        for scan in tuple(self._scans):
            await scan.aclose()
        self.revoke_files()
        self._export._sessions.discard(self)

    def revoke_files(self) -> None:
        for file in tuple(self._files):
            try:
                self.close(file)
            except OSError:
                continue


class _Auth(asyncssh.SSHServer):
    def __init__(self, export: "WorkspaceExport", key: asyncssh.SSHKey) -> None:
        self._export = export
        self._key = key
        self._connection: asyncssh.SSHServerConnection | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        if self._export._revoked:
            conn.abort()
            return
        self._connection = conn
        self._export._connections.add(conn)

    def connection_lost(self, exc: Exception | None) -> None:
        if self._connection is not None:
            self._export._connections.discard(self._connection)

    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return (
            not self._export._revoked
            and username == "export"
            and key.public_data == self._key.public_data
        )


class WorkspaceExport:
    """Один SFTP-экспорт с отдельным ключом и бюджетом файловых операций."""

    def __init__(
        self,
        root: Path,
        *,
        mode: str,
        write_limit: int,
        protected_paths: Iterable[str] = (),
        entry_limit: int = 256,
    ) -> None:
        if sys.platform not in ("darwin", "linux"):
            raise RuntimeError("SFTP-экспорт поддерживается на macOS и Linux")
        if mode not in ("ro", "rw") or write_limit < 0 or entry_limit <= 0:
            raise ValueError("Недопустимые параметры экспорта")
        self._root = Path(root)
        self._mode = mode
        self._write_limit = write_limit
        self._entry_limit = entry_limit
        self._protected = tuple(
            tuple(_fold(part) for part in _parts(os.fsencode(path)))
            for path in protected_paths
        )
        if () in self._protected:
            raise ValueError("Для защиты всей папки используйте режим ro")
        self._host_key = asyncssh.generate_private_key("ssh-ed25519")
        self._listener: asyncssh.SSHAcceptor | None = None
        self._startup: asyncio.Task[asyncssh.SSHAcceptor] | None = None
        self._connections: set[asyncssh.SSHServerConnection] = set()
        self._sessions: set[_ExportSFTP] = set()
        self._tree: _Tree | None = None
        self._revoked = False
        self._close_result: bool | None = None
        self._lock = asyncio.Lock()

    @property
    def host_public_key(self) -> str:
        return self._host_key.export_public_key().decode("ascii").strip()

    @property
    def port(self) -> int | None:
        return self._listener.get_port() if self._listener is not None else None

    def _sftp(self, channel: asyncssh.SSHServerChannel) -> _ExportSFTP:
        if self._revoked:
            raise asyncssh.SFTPConnectionLost("Экспорт закрыт")
        session = _ExportSFTP(channel, self)
        self._sessions.add(session)
        return session

    @staticmethod
    def _close_canceled_listener(task: asyncio.Task[asyncssh.SSHAcceptor]) -> None:
        if not task.cancelled() and task.exception() is None:
            task.result().close()

    async def start(self, export_client_key: str) -> int:
        async with self._lock:
            if self._revoked or self._listener is not None:
                raise RuntimeError("Экспорт уже запущен или закрыт")
            key = asyncssh.import_public_key(export_client_key)
            if key.get_algorithm() != "ssh-ed25519":
                raise ValueError("Ожидается ключ ssh-ed25519")
            root = self._root.resolve(strict=True)
            if any(
                _hidden(os.fsencode(part)) for part in (*self._root.parts, *root.parts)
            ):
                raise ValueError("Каталоги .git исключены из экспорта")
            descriptor = os.open(root, _DIR_FLAGS)
            try:
                self._tree = _Tree(
                    descriptor,
                    self._mode,
                    self._write_limit,
                    self._entry_limit,
                    self._protected,
                )

                async def listen() -> asyncssh.SSHAcceptor:
                    return await asyncssh.create_server(
                        lambda: _Auth(self, key),
                        "127.0.0.1",
                        0,
                        config=None,
                        server_host_keys=[self._host_key],
                        sftp_factory=self._sftp,
                        sftp_version=3,
                        login_timeout=8,
                    )

                startup = asyncio.create_task(listen())
                try:
                    self._listener = await asyncio.shield(startup)
                except asyncio.CancelledError:
                    # bind может уже состояться к моменту отмены ожидания.
                    self._revoked = True
                    self._startup = startup
                    startup.add_done_callback(self._close_canceled_listener)
                    for connection in tuple(self._connections):
                        connection.abort()
                    raise
            except BaseException:
                if self._tree is not None:
                    self._tree.live = False
                os.close(descriptor)
                self._tree = None
                raise
            return self._listener.get_port()

    async def close(self, timeout: float = 5.0) -> bool:
        """Отзывает доступ и подтверждает завершение операций экспорта."""
        async with self._lock:
            if self._close_result is not None:
                return self._close_result
            self._revoked = True
            tree = self._tree
            if tree is not None:
                tree.live = False
            listener = self._listener
            self._listener = None
            if listener is not None:
                listener.close()
            for session in tuple(self._sessions):
                session.revoke_files()
            connections = tuple(self._connections)
            for connection in connections:
                connection.close()
            drained = False
            try:
                async with asyncio.timeout(timeout):
                    if listener is not None:
                        await listener.wait_closed()
                    if self._startup is not None:
                        startup = self._startup
                        await asyncio.wait((startup,))
                        if not startup.cancelled() and startup.exception() is None:
                            acceptor = startup.result()
                            acceptor.close()
                            await acceptor.wait_closed()
                        self._startup = None
                    await asyncio.gather(*(conn.wait_closed() for conn in connections))
                    for session in tuple(self._sessions):
                        await session.exit()
                drained = not self._connections and not self._sessions
            except TimeoutError:
                for connection in connections:
                    connection.abort()
            finally:
                if tree is not None:
                    try:
                        os.close(tree.fd)
                    except OSError:
                        tree.uncertain = True
                self._close_result = drained and (tree is None or not tree.uncertain)
            return self._close_result

    async def __aenter__(self) -> "WorkspaceExport":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
