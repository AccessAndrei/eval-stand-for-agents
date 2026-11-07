import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.configure_local import configure


def test_local_setup_preserves_tokens_and_records_native_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    metadata = {
        "codex_version": "0.159.3",
        "commit": "01fc69f4026735edfdf6789820549727a4867b11",
        "origin": "source_build",
        "architecture": "arm64",
        "image_digest": "sha256:" + "a" * 64,
    }
    source = tmp_path / "provenance.json"
    source.write_text(json.dumps(metadata))
    monkeypatch.setattr(
        "scripts.configure_local.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            stdout=json.dumps(
                [{"Id": metadata["image_digest"], "Architecture": "arm64"}]
            )
        ),
    )
    env = tmp_path / ".env"
    env.write_text("CLIENT_TOKEN=existing-private-test-token\nUNRELATED=preserve\n")
    configure(source, env)
    text = env.read_text()
    assert "CLIENT_TOKEN=existing-private-test-token" in text
    assert "UNRELATED=preserve" in text
    assert "DOCKER_PLATFORM=linux/arm64" in text
    assert env.stat().st_mode & 0o777 == 0o600
    first = text
    configure(source, env)
    assert env.read_text() == first


def test_rejects_diagnostic_provenance_before_env_change(tmp_path: Path) -> None:
    source = tmp_path / "provenance.json"
    source.write_text(json.dumps({"origin": "upstream_diagnostic"}))
    with pytest.raises(ValueError):
        configure(source, tmp_path / ".env")
    assert not (tmp_path / ".env").exists()


def test_dotenv_assignment_syntax_is_recognized() -> None:
    from scripts.configure_local import assignments

    assert assignments(
        [
            '  CLIENT_TOKEN = "preserve-me" # comment',
            "MANAGER_TOKEN=''",
            "export HARNESS_TOKEN=abc",
        ]
    ) == {"CLIENT_TOKEN": "preserve-me", "MANAGER_TOKEN": "", "HARNESS_TOKEN": "abc"}
    with pytest.raises(ValueError):
        assignments(["CLIENT_TOKEN=one", " CLIENT_TOKEN = two"])
