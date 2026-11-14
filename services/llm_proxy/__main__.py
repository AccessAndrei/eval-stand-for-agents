"""Запуск посредника с выделенным управляющим токеном."""

import os
from pathlib import Path

from aiohttp import web

from services.llm_proxy.app import create_app


def main() -> None:
    app = create_app(
        Path(os.environ.get("PROFILES_PATH", "config/profiles.json")),
        Path(os.environ.get("PROXY_STATE_DIR", "/state/proxy")),
        os.environ["INTERNAL_TOKEN"],
    )
    web.run_app(app, host="0.0.0.0", port=8000, access_log=None)


if __name__ == "__main__":
    main()
