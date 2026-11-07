"""Подготовить локальную .env без печати секретов и без изменения Docker."""

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from scripts.build_codex import COMMIT, VERSION

ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def assignments(lines: list[str]) -> dict[str, str]:
    values = {}
    for line in lines:
        match = ASSIGNMENT.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key in values:
            raise ValueError("Повторяющийся ключ .env требует явного исправления")
        value = value.strip()
        if value[:1] in {'"', "'"}:
            quoted = re.fullmatch(r"""(["'])(.*?)\1(?:\s+#.*)?""", value)
            if quoted is None:
                raise ValueError("Неоднозначная строка .env")
            value = quoted[2]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def configure(provenance_path: Path, env_path: Path = Path(".env")) -> None:
    provenance_path = provenance_path.resolve(strict=True)
    data = json.loads(provenance_path.read_text())
    if (
        data.get("origin") != "source_build"
        or data.get("commit") != COMMIT
        or data.get("codex_version") != VERSION
        or not str(data.get("image_digest", "")).startswith("sha256:")
    ):
        raise ValueError("Нужны проверенные метаданные source-build")
    architecture = data.get("architecture")
    platform = {
        "arm64": "linux/arm64",
        "aarch64": "linux/arm64",
        "amd64": "linux/amd64",
        "x86_64": "linux/amd64",
    }.get(architecture)
    if platform is None:
        raise ValueError("Неизвестная архитектура source-build")
    image = subprocess.run(
        ["docker", "image", "inspect", data["image_digest"]],
        capture_output=True,
        timeout=15,
        check=True,
    )
    actual = json.loads(image.stdout)[0]
    if (
        actual["Id"] != data["image_digest"]
        or actual["Architecture"] != platform.split("/")[1]
    ):
        raise ValueError("Локальный образ не соответствует метаданным")
    if env_path.is_symlink():
        raise ValueError("Символическая ссылка вместо .env запрещена")
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    existing = assignments(lines)
    changes = {}
    for name in ("CLIENT_TOKEN", "MANAGER_TOKEN", "HARNESS_TOKEN", "PROXY_TOKEN"):
        if not existing.get(name):
            changes[name] = secrets.token_urlsafe(32)
        elif "$" not in existing[name] and (
            len(existing[name]) < 24
            or any(character.isspace() for character in existing[name])
        ):
            raise ValueError(
                "Существующий токен .env недопустим; автоматическая замена запрещена"
            )
    changes.update(
        PROVENANCE_PATH=str(provenance_path),
        DOCKER_PLATFORM=platform,
        LOCAL_UID=str(os.getuid()),
        LOCAL_GID=str(os.getgid()),
        RESULTS_PATH=str(Path("results").resolve()),
    )
    for name, value in changes.items():
        lines = [
            line
            for line in lines
            if not (match := ASSIGNMENT.match(line)) or match[1] != name
        ]
        lines.append(name + "=" + value)
    Path("results").mkdir(exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=env_path.parent, prefix=".env-write-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, env_path)
        env_path.chmod(0o600)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    try:
        configure(args.provenance)
    except (OSError, ValueError, subprocess.SubprocessError, KeyError):
        print("Не удалось подготовить .env; проверьте метаданные и Docker")
        return 2
    print(
        "Локальная .env подготовлена; существующие токены сохранены, секреты не выводятся"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
