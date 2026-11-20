"""Загрузка полного доверенного набора до первого обращения к gateway."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

import yaml

from contracts import ContractError, validate
from contracts.mounted import validate_mounted

from .mounted_files import (
    MAX_WORKTREE_FILES,
    WorkspaceTreeError,
    export_path,
    read_fixture_tree,
)

MAX_FIXTURE_BYTES = 1_000_000
MAX_FIXTURE_FILES = MAX_WORKTREE_FILES
B5_COMMAND = "/usr/local/bin/python3 -I -S /work/input/stats.py /work/input/data.csv"


@dataclass(frozen=True)
class FixtureFile:
    path: str
    content: bytes
    sha256: str
    read_only: bool = False


@dataclass(frozen=True)
class Task:
    id: str
    baf: str
    directory: Path
    messages: tuple[str, ...]
    timeout_s: int
    fixture_files: tuple[FixtureFile, ...]
    evidence_required: bool = False
    fixture_directories: tuple[str, ...] = ()
    answer_file: str = ""

    @property
    def checker_path(self) -> Path:
        return self.directory / "check.py"

    @property
    def input_manifest(self) -> dict[str, str]:
        return {file.path: file.sha256 for file in self.fixture_files if file.read_only}


class CatalogError(ValueError):
    """Неполное или недопустимое доверенное описание задачи."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise CatalogError("Ключ YAML повторён либо не является строкой")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _load_yaml(path: Path) -> dict:
    try:
        if not path.is_file():
            raise CatalogError(f"Описание {path.name} должно быть обычным файлом")
        if path.stat().st_size > 65_536:
            raise CatalogError("Описание превышает 64 КиБ")
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"Не удалось прочитать описание {path.name}") from exc
    if not isinstance(value, dict):
        raise CatalogError("Описание должно быть объектом YAML")
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise CatalogError("Недопустимый относительный путь")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise CatalogError("Недопустимый относительный путь")
    return value


def _contained_path(root: Path, value: object) -> Path:
    relative = _relative_path(value)
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            raise CatalogError("Символические ссылки в исходных файлах запрещены")
    return candidate


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogError("Ожидался список файлов")
    paths = tuple(_relative_path(item) for item in value)
    if len(paths) != len(set(paths)):
        raise CatalogError("Список файлов содержит повторения")
    return paths


def _fixture(
    directory: Path, data: dict
) -> tuple[tuple[FixtureFile, ...], tuple[str, ...]]:
    declared = _paths(data.get("fixture_files"))
    protected = _paths(data.get("read_only_files", []))
    if not set(protected).issubset(declared):
        raise CatalogError("Защищённые файлы отсутствуют в fixture_files")
    root = directory / "fixture"
    if root.is_symlink() or not root.is_dir():
        raise CatalogError("Каталог fixture отсутствует либо является ссылкой")
    try:
        tree, contents = read_fixture_tree(
            root, max_bytes=MAX_FIXTURE_BYTES, max_files=MAX_FIXTURE_FILES
        )
    except WorkspaceTreeError as error:
        raise CatalogError(f"Некорректная fixture ({error})") from error
    if not declared or set(declared) != contents.keys():
        raise CatalogError("Fixture неполна либо содержит необъявленные файлы")
    return (
        tuple(
            FixtureFile(name, content, sha256(content).hexdigest(), name in protected)
            for name, content in sorted(contents.items())
        ),
        tree.directories,
    )


def _validate_evidence(data: dict, task_id: str) -> bool:
    evidence = data.get("evidence")
    if task_id != "B5.1":
        if evidence is not None or data.get("read_only_files"):
            raise CatalogError("Неожиданный контракт доказательства")
        return False
    expected = {
        "version": "v0",
        "codex_version": "0.159.3",
        "native_item_type": "commandExecution",
        "cwd": "/work",
        "command": B5_COMMAND,
        "interpreter": "/usr/local/bin/python3",
        "native_shell_argv": ["/usr/bin/bash", "-lc"],
        "allowed_sources": ["agent", "unifiedExecStartup"],
        "required_events": ["started", "completed"],
        "required_ids": ["run_id", "thread_id", "turn_id", "item_id"],
    }
    if evidence != expected or set(data.get("read_only_files", [])) != {
        "input/stats.py",
        "input/data.csv",
    }:
        raise CatalogError("Контракт доказательства B5.1 не соответствует v0")
    return True


def _load_task(root: Path, task_id: str, relative_directory: object) -> Task:
    directory = _contained_path(root, relative_directory)
    data = _load_yaml(_contained_path(directory, "task.yaml"))
    baf = data.get("baf")
    messages = data.get("messages")
    timeout = data.get("timeout_s")
    if data.get("id") != task_id:
        raise CatalogError("Идентификатор задачи не совпадает с манифестом")
    if not isinstance(baf, str) or not baf.strip():
        raise CatalogError("Категория БАФ должна быть непустой строкой")
    if (
        not isinstance(messages, list)
        or not messages
        or any(
            not isinstance(message, str) or not message.strip() for message in messages
        )
    ):
        raise CatalogError("В задаче нет текста задания")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise CatalogError("Недопустимый timeout_s")
    if not _contained_path(directory, "check.py").is_file():
        raise CatalogError("У задачи отсутствует доверенный check.py")
    answer_file = _relative_path(data.get("answer_file"))
    fixture_files, fixture_directories = _fixture(directory, data)
    return Task(
        id=task_id,
        baf=baf,
        directory=directory,
        messages=tuple(messages),
        timeout_s=timeout,
        fixture_files=fixture_files,
        evidence_required=_validate_evidence(data, task_id),
        fixture_directories=fixture_directories,
        answer_file=answer_file,
    )


def load_dataset(path: Path, *, mounted: bool = False) -> tuple[Task, ...]:
    """Проверить весь манифест и загрузить исходные файлы до запуска."""
    path = path.resolve()
    data = _load_yaml(path)
    entries = data.get("tasks")
    if type(data.get("version")) is not int or data["version"] != 0:
        raise CatalogError("Неподдерживаемая версия манифеста")
    if not isinstance(entries, list) or not entries:
        raise CatalogError("Датасет должен содержать непустой список задач")
    directories: dict[str, object] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "directory"}:
            raise CatalogError("Запись датасета должна содержать id и directory")
        task_id = entry["id"]
        try:
            validate("task_id", task_id)
        except ContractError as error:
            raise CatalogError("Недопустимый идентификатор задачи") from error
        if task_id in directories:
            raise CatalogError("Идентификаторы задач в датасете повторяются")
        directories[task_id] = entry["directory"]
    tasks = tuple(
        _load_task(path.parent, task_id, directory)
        for task_id, directory in directories.items()
    )
    if mounted:
        for task in tasks:
            try:
                export_path(task.answer_file)
            except WorkspaceTreeError as error:
                raise CatalogError(
                    f"Задача {task.id} задаёт недоступный путь ответа"
                ) from error
            try:
                validate_mounted(
                    "run_start",
                    {
                        "run_id": "0" * 32,
                        "task_id": task.id,
                        "prompt": "\n".join(task.messages),
                        "timeout_s": task.timeout_s,
                    },
                )
            except ContractError as error:
                raise CatalogError(
                    f"Задача {task.id} не соответствует схеме запуска v1"
                ) from error
    return tasks
