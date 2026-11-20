"""Публикация идентичности образа Codex для Gateway при запуске Compose."""

import re
from pathlib import Path

from contracts import validate
from services.common.storage import atomic_json

from .docker import Docker, DockerError

CODEX_VERSION = "0.159.3"
CODEX_COMMIT = "01fc69f4026735edfdf6789820549727a4867b11"


async def publish_provenance(docker: Docker, image: str, destination: Path) -> None:
    info = await docker.inspect("image", image)
    if info is None:
        raise DockerError("codex_source_image_missing")
    labels = (info.get("Config") or {}).get("Labels") or {}
    digest, architecture = info.get("Id"), info.get("Architecture")
    if (
        labels.get("org.opencontainers.image.revision") != CODEX_COMMIT
        or labels.get("org.eval-stand.codex-origin") != "source_build"
        or labels.get("org.eval-stand.codex-version") != CODEX_VERSION
        or info.get("Os") != "linux"
        or architecture not in ("amd64", "arm64")
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
    ):
        raise DockerError("codex_source_image_mismatch")
    provenance = {
        "codex_version": CODEX_VERSION,
        "commit": CODEX_COMMIT,
        "origin": "source_build",
        "image_digest": digest,
        "architecture": architecture,
    }
    validate("provenance", provenance)
    atomic_json(destination, provenance)
    destination.chmod(0o644)
