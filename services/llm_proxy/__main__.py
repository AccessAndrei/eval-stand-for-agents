"""Запуск посредника с выделенным управляющим токеном."""

import os
from pathlib import Path

from aiohttp import web

from services.llm_proxy.app import AUTH_CALLBACK_URL, create_app


def main() -> None:
    auth_root = Path(os.environ["AUTH_STORE"]) if os.environ.get("AUTH_STORE") else None
    app = create_app(
        Path(os.environ.get("PROFILES_PATH", "config/profiles.json")),
        Path(os.environ.get("PROXY_STATE_DIR", "/state/proxy")),
        os.environ["INTERNAL_TOKEN"],
        auth_root=auth_root,
        auth_callback_url=AUTH_CALLBACK_URL if auth_root else None,
    )
    web.run_app(app, host="0.0.0.0", port=8000, access_log=None)


if __name__ == "__main__":
    main()
