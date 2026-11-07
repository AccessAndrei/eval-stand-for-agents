"""Чистые настройки экземпляра, закреплённая удалённая среда."""

import json
import os
import sys
from pathlib import Path

VERSION = "0.159.3"
COMMIT = "01fc69f4026735edfdf6789820549727a4867b11"


def configure(
    home: Path,
    *,
    manager_url: str,
    manager_token: str,
    project_id: str,
    handle: str,
    run_id: str,
    proxy_url: str,
    inference_token: str,
    model: str,
) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=False, mode=0o700)
    quote = json.dumps
    relay_env = {
        "MANAGER_TUNNEL_URL": manager_url,
        "MANAGER_TOKEN": manager_token,
        "PROJECT_ID": project_id,
        "SANDBOX_HANDLE": handle,
        "RUN_ID": run_id,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    env_toml = [
        "include_local = false",
        'default = "sandbox"',
        "[[environments]]",
        'id = "sandbox"',
        f"program = {quote(sys.executable)}",
        'args = ["-m", "services.harness.relay"]',
        f"cwd = {quote(str(home))}",
        "initialize_timeout_sec = 30",
        "[environments.env]",
        *(f"{key} = {quote(value)}" for key, value in relay_env.items()),
    ]
    (home / "environments.toml").write_text("\n".join(env_toml) + "\n")
    config = [
        f"model = {quote(model)}",
        'model_provider = "stand"',
        'approval_policy = "never"',
        'web_search = "disabled"',
        "[shell_environment_policy]",
        'inherit = "none"',
        "[shell_environment_policy.set]",
        'PATH = "/usr/local/bin:/usr/bin:/bin"',
        'HOME = "/nonexistent"',
        "[model_providers.stand]",
        'name = "stand"',
        f"base_url = {quote(proxy_url)}",
        'env_key = "STAND_INFERENCE_TOKEN"',
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "supports_websockets = false",
        "request_max_retries = 0",
        "stream_max_retries = 0",
        "[features]",
        "unified_exec = true",
        "multi_agent = false",
        "hooks = false",
    ]
    if model in {"mock-codex", "mock-codex-alt", "gpt-6-luna"}:
        catalog = home / "models.json"
        catalog.write_text(json.dumps({"models": [sandbox_model(model)]}))
        config.insert(0, f"model_catalog_json = {quote(str(catalog))}")
    (home / "config.toml").write_text("\n".join(config) + "\n")
    for path in home.iterdir():
        path.chmod(0o600)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "CODEX_HOME": str(home),
        "STAND_INFERENCE_TOKEN": inference_token,
    }


def sandbox_model(model: str) -> dict:
    """Каталог инструментов харнесса: прямой exec без Node/code-mode runtime.

    Model id остаётся исходным; поля выбора инструментов — API Codex 0.159.3.
    Системная инструкция относится к стенду, а не к desktop-среде upstream.
    """
    return {
        "slug": model,
        "display_name": model,
        "description": None,
        "supported_reasoning_levels": [],
        "shell_type": "unified_exec",
        "tool_mode": "direct",
        "use_responses_lite": False,
        "node_repl_disabled": True,
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "availability_nux": None,
        "upgrade": None,
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "experimental_supported_tools": [],
        "include_apps_usage_instructions": False,
        "model_messages": {
            "instructions_template": "Выполни задание штатными инструментами выбранной среды. Не выбирай другую среду."
        },
    }


def bind_recovery_owner(home: Path, owner: dict[str, str]) -> None:
    """Явные markers relay, независимо от наследования окружения upstream."""
    if set(owner) != {"EVAL_STAND_ATTEMPT_OWNER"}:
        raise ValueError("invalid_recovery_owner")
    with (home / "environments.toml").open("a") as stream:
        stream.write("CODEX_HOME = " + json.dumps(str(home.absolute())) + "\n")
        stream.write(
            "EVAL_STAND_ATTEMPT_OWNER = "
            + json.dumps(owner["EVAL_STAND_ATTEMPT_OWNER"])
            + "\n"
        )
