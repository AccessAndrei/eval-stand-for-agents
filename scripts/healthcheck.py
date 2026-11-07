"""Проверка локального HTTP endpoint без вывода управляющего токена."""

import argparse
import json
import os
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def healthy_payload(payload: object, budget_capabilities: bool = False) -> bool:
    if not isinstance(payload, dict):
        return False
    if budget_capabilities:
        return (
            payload.get("mechanism") == "mock-utf8-quarter-v1"
            and payload.get("enforces_max_output_tokens") is True
            and isinstance(payload.get("models"), list)
            and bool(payload["models"])
            and all(isinstance(model, str) and model for model in payload["models"])
        )
    return payload.get("ready") is True or payload.get("status") == "ready"


def check(
    url: str,
    token_env: str | None,
    codex: bool,
    budget_capabilities: bool = False,
) -> bool:
    headers = {}
    if token_env:
        token = os.environ.get(token_env, "")
        if len(token) < 24:
            return False
        headers["Authorization"] = "Bearer " + token
    if codex:
        reply = subprocess.run(
            ["/usr/local/bin/codex", "--version"],
            capture_output=True,
            timeout=3,
            check=False,
        )
        if reply.returncode != 0 or reply.stdout.strip() != b"codex-cli 0.159.3":
            return False
    try:
        with urlopen(Request(url, headers=headers), timeout=3) as response:
            payload = json.loads(response.read(16_384))
            return response.status == 200 and healthy_payload(
                payload, budget_capabilities
            )
    except (HTTPError, URLError, OSError, ValueError, subprocess.SubprocessError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--token-env")
    parser.add_argument("--codex", action="store_true")
    parser.add_argument("--budget-capabilities", action="store_true")
    args = parser.parse_args()
    try:
        return (
            0
            if check(args.url, args.token_env, args.codex, args.budget_capabilities)
            else 1
        )
    except (OSError, subprocess.SubprocessError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
