# harness

Python-адаптер штатного Codex 0.159.3. Запуск: `python -m services.harness.server`;
Docker target `harness` — в [общем Dockerfile](../../docker/Dockerfile.services).

[adapter.py](adapter.py) ведёт один ephemeral thread/turn через [rpc.py](rpc.py).
[settings.py](settings.py) создаёт чистые настройки выбранной удалённой среды;
[relay.py](relay.py) соединяет stdio с CONNECT менеджера. Docker socket у сервиса нет.

[recovery.py](recovery.py) регистрирует попытку до spawn, сохраняет нативный
итог перед удалением home и подтверждает отсутствие собственных процессов.
Сверка выполняется до приёма новых задач. Неизвестная принадлежность процесса
или неуспешная очистка сохраняет блокировку; команды не воспроизводятся.

HTTP на 8083: `/health`, `POST /v0/runs`, `GET /v0/runs/{run_id}`,
`POST /v0/runs/{run_id}/cancel` требуют `HARNESS_TOKEN`.
`/v0/auth/status` и `/v0/auth/prepare` защищены отдельно `PROXY_TOKEN`.

При заданном `AUTH_STORE` [native_auth.py](native_auth.py) удерживает владельца
выделенных credentials и поднимает loopback-мост для нативного refresh.
Auth-only Codex не создаёт thread/turn. Агентный app-server получает только
токен доступа к посреднику; реальные ChatGPT-заголовки добавляет llm-proxy.
Освобождение auth-worker подтверждается `wait_idle` до финализации прогона.

Полная схема и указатели upstream-кода: [harness.md](../../docs/harness.md).
Первичный вход и Compose: [README](../../README.md). Измеренная готовность и
результаты итоговых проверок: [отчёт](../../docs/report.md).
