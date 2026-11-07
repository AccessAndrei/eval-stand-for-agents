"""Загрузка полного доверенного набора до первого обращения к gateway."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

import yaml

TASK_IDS = ("B1.1", "B2.1", "B3.1", "B4.1", "B5.1")
DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "checkpoint1.yaml"
MAX_FIXTURE_BYTES = 1_000_000
MAX_FIXTURE_FILES = 64
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
    directory: Path
    messages: tuple[str, ...]
    answer_file: str
    timeout_s: int
    fixture_files: tuple[FixtureFile, ...]
    evidence_required: bool = False

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


def _fixture(directory: Path, data: dict) -> tuple[FixtureFile, ...]:
    declared = _paths(data.get("fixture_files"))
    protected = _paths(data.get("read_only_files", []))
    if not set(protected).issubset(declared):
        raise CatalogError("Защищённые файлы отсутствуют в fixture_files")
    root = directory / "fixture"
    if root.is_symlink() or not root.is_dir():
        raise CatalogError("Каталог fixture отсутствует либо является ссылкой")
    files = []
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise CatalogError("Fixture содержит ссылку либо специальный файл")
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        total += path.stat().st_size
        if total > MAX_FIXTURE_BYTES or len(files) >= MAX_FIXTURE_FILES:
            raise CatalogError("Fixture превышает ограничение объёма")
        content = path.read_bytes()
        files.append(
            FixtureFile(name, content, sha256(content).hexdigest(), name in protected)
        )
    if not declared or set(declared) != {file.path for file in files}:
        raise CatalogError("Fixture неполна либо содержит необъявленные файлы")
    return tuple(sorted(files, key=lambda file: file.path))


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


def _load_task(root: Path, entry: dict) -> Task:
    if not isinstance(entry, dict) or set(entry) != {"id", "directory"}:
        raise CatalogError("Неверная запись задачи в checkpoint1.yaml")
    directory = _contained_path(root, entry["directory"])
    data = _load_yaml(_contained_path(directory, "task.yaml"))
    messages = data.get("messages")
    timeout = data.get("timeout_s")
    if data.get("id") != entry["id"] or data.get("baf") != entry["id"][:2]:
        raise CatalogError("Идентификатор задачи не совпадает с каталогом")
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
    return Task(
        entry["id"],
        directory,
        tuple(messages),
        _relative_path(data.get("answer_file")),
        timeout,
        _fixture(directory, data),
        _validate_evidence(data, entry["id"]),
    )


def load_checkpoint(path: Path = DEFAULT_CATALOG) -> tuple[Task, ...]:
    """Проверить все пять описаний и скопировать в память только fixture."""
    path = path.resolve()
    data = _load_yaml(path)
    entries = data.get("tasks")
    if type(data.get("version")) is not int or data["version"] != 0:
        raise CatalogError("Неподдерживаемая версия каталога")
    if not isinstance(entries, list) or any(
        not isinstance(item, dict) for item in entries
    ):
        raise CatalogError("Нет списка задач")
    if tuple(item.get("id") for item in entries) != TASK_IDS:
        raise CatalogError("Нужны ровно B1.1–B5.1 в установленном порядке")
    return tuple(_load_task(path.parent, entry) for entry in entries)
