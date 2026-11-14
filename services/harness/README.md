# harness

Python-адаптер штатного Codex 0.159.3. Запуск: `python -m services.harness.server`;
Docker target `harness` — в [общем Dockerfile](../../docker/Dockerfile.services).
Выбор харнесса: [ADR-0001](../../docs/adr/0001-codex-python.md);
транспорт исполнителя: [ADR-0003](../../docs/adr/0003-stdio-transport.md).

[adapter.py](adapter.py) ведёт один ephemeral thread/turn через [rpc.py](rpc.py).
[settings.py](settings.py) создаёт чистые настройки выбранной удалённой среды;
[relay.py](relay.py) соединяет stdio с CONNECT менеджера. Docker socket у сервиса нет.

[recovery.py](recovery.py) регистрирует попытку до spawn, сохраняет нативный
итог перед удалением home и подтверждает отсутствие собственных процессов.
Сверка выполняется до приёма новых задач. Неизвестная принадлежность процесса
или неуспешная очистка сохраняет блокировку; команды не воспроизводятся.

HTTP на 8083: `/health`, `POST /v0/runs`, `GET /v0/runs/{run_id}`,
`POST /v0/runs/{run_id}/cancel` требуют `HARNESS_TOKEN`.
Модель и её endpoint выбирает профиль `llm-proxy`. Harness получает только
внутренний токен конкретной попытки; upstream API-ключ ему не передаётся.
Device-login, `/v0/auth/*`, refresh-worker и монтирование `.auth/` удалены.
Настройки Codex создаются отдельно для каждой выбранной модели; личный
каталог пользователя не наследуется.

Полная схема и указатели upstream-кода: [harness.md](../../docs/harness.md).
Первичный вход и Compose: [README](../../README.md). Измеренная готовность и
результаты итоговых проверок: [отчёт](../../docs/report.md).
