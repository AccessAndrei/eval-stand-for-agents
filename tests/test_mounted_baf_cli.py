"""Обычный запуск БАФ использует общий клиент смонтированной папки."""

from pathlib import Path
from typing import Any

import pytest

from baf.runner import __main__ as cli
from baf.runner import mounted_batch
from client import session

DATASET = Path("baf/control.yaml").resolve()
FILE_TOKEN = "token-from-test-file-keep-private"
ENV_TOKEN = "token-from-test-process-keep-private"


@pytest.mark.parametrize(
    "overrides, file_port", [(False, "18080"), (True, "18080"), (False, "")]
)
def test_default_cli_runs_mounted_batch_with_safe_host_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: bool,
    file_port: str,
) -> None:
    configuration = tmp_path / ".env"
    from_file = tmp_path / "reports with spaces"
    file_results = str(from_file) if file_port else ""
    file_storage_limit = "123456789" if file_port else ""
    configuration.write_text(
        f"CLIENT_TOKEN='{FILE_TOKEN}'\n"
        f"GATEWAY_PORT={file_port}\n"
        f'RESULTS_PATH="{file_results}"\n'
        f"RUNNER_STORAGE_BYTES={file_storage_limit}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "LOCAL_ENV", configuration)
    for key in cli.HOST_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    arguments = ["baf.runner", "--dataset", str(DATASET), "--profile", "mock"]
    if overrides:
        monkeypatch.setenv("CLIENT_TOKEN", ENV_TOKEN)
        monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:28080")
        arguments += ["--results", str(tmp_path / "explicit")]
    monkeypatch.setattr("sys.argv", arguments)
    client = object()

    def connect(url: str, token: str) -> object:
        port = "28080" if overrides else (file_port or "8080")
        assert url == f"http://127.0.0.1:{port}"
        assert token == (ENV_TOKEN if overrides else FILE_TOKEN)
        return client

    async def run(supplied: object, **options: Any) -> tuple[Path, dict]:
        assert supplied is client
        assert "smoke" not in options
        assert options["profile"] == "mock"
        assert options["storage_limit"] == int(file_storage_limit or "1000000000")
        results = (
            tmp_path / "explicit" if overrides else Path(file_results or "results")
        )
        assert options["results"] == results
        assert [item.id for item in options["tasks"]] == [
            "B1.1",
            "B2.1",
            "B3.1",
            "B4.1",
            "B5.1",
        ]
        return tmp_path / "report.json", {
            "success": not overrides,
            "tasks": options["tasks"],
            "evaluated": 5,
            "wrong_answers": int(overrides),
            "technical_skips": 0,
        }

    monkeypatch.setattr(session, "MountedClient", connect)
    monkeypatch.setattr(mounted_batch, "run_mounted_batch", run)
    assert cli.main() == int(overrides)
    output = capsys.readouterr()
    assert str(tmp_path / "report.json") in output.out
    assert "Оценено: 5/5" in output.out
    assert FILE_TOKEN not in output.out + output.err
    assert ENV_TOKEN not in output.out + output.err


def test_mounted_dataset_is_validated_before_reading_config_or_building_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "manifest.yaml"
    dataset.write_text("version: 0\ntasks: []\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["baf.runner", "--dataset", str(dataset), "--profile", "mock"]
    )

    def unexpected(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("Настройки и клиент должны загружаться после проверки датасета")

    monkeypatch.setattr(cli, "_host_settings", unexpected)
    monkeypatch.setattr(session, "MountedClient", unexpected)
    assert cli.main() == 2
    assert "Ошибка датасета" in capsys.readouterr().err


def test_file_interpolation_requests_explicit_environment_without_exposing_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configuration = tmp_path / ".env"
    configuration.write_text("CLIENT_TOKEN=${PRIVATE_ENV_VALUE}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "LOCAL_ENV", configuration)
    monkeypatch.delenv("CLIENT_TOKEN", raising=False)
    monkeypatch.setenv("PRIVATE_ENV_VALUE", FILE_TOKEN)
    monkeypatch.setattr(
        "sys.argv", ["baf.runner", "--dataset", str(DATASET), "--profile", "mock"]
    )
    assert cli.main() == 2
    output = capsys.readouterr()
    assert "Задайте CLIENT_TOKEN в окружении" in output.err
    assert FILE_TOKEN not in output.out + output.err


def test_smoke_uses_named_flag_and_reports_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "LOCAL_ENV", tmp_path / ".env")
    for key in cli.HOST_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLIENT_TOKEN", ENV_TOKEN)
    monkeypatch.setattr(
        "sys.argv",
        ["baf.runner", "--smoke", "--dataset", str(DATASET), "--profile", "mock"],
    )

    async def run(_client: session.MountedClient, **options: Any) -> tuple[Path, dict]:
        assert options["smoke"] is True and options["profile"] == "mock"
        return tmp_path / "report.json", {
            "success": False,
            "tasks": [{"smoke_status": "passed"}, {"smoke_status": "failed"}],
        }

    monkeypatch.setattr(mounted_batch, "run_mounted_batch", run)
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert "Проверку файлов smoke прошли 1/2 задач" in output
    assert "Оценено" not in output


def test_smoke_rejects_legacy_protocol_before_dataset_loading(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "baf.runner",
            "--protocol",
            "v0",
            "--smoke",
            "--dataset",
            "missing.yaml",
            "--profile",
            "mock",
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "--smoke требует протокол v1" in capsys.readouterr().err
