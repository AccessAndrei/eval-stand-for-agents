# Посредник модели

Запуск: `python -m services.llm_proxy`. Переменные: `INTERNAL_TOKEN`
(не менее 24 символов), `PROFILES_PATH` (по умолчанию `config/profiles.json`),
`PROXY_STATE_DIR` (по умолчанию `/state/proxy`). Каталог состояния приватный;
журнал HTTP-запросов отключён. Самостоятельный mock запускается командой
`python -m services.llm_proxy.mock --host 0.0.0.0 --port 8090`.

Управляющие запросы требуют `Authorization: Bearer <INTERNAL_TOKEN>`:

| Маршрут | Назначение |
|---|---|
| `GET /health` | Готовность процесса; не подтверждает live-профиль |
| `GET /v0/profiles/{name}` | Проверка профиля до provision; 422 при несовместимости |
| `POST /v0/runs/{run_id}` | Тело `{"provider_profile":"mock"}`; устойчивое открытие ledger |
| `GET /v0/runs/{run_id}` | Исход, число запросов и токены |
| `POST /v0/runs/{run_id}/close` | Отмена текущего upstream без повтора, закрытие ledger |
| `POST /v0/auth/native-refresh` | Только при `AUTH_STORE`: узкий нативный refresh активной auth-попытки |

Открытие возвращает `inference_token`. Только этот токен допускается в
`POST /v0/runs/{run_id}/responses`; управляющий токен не даёт inference-доступ.
В Codex задаются `base_url` с окончанием `/v0/runs/{run_id}`,
`supports_websockets=false`, `request_max_retries=0`, `stream_max_retries=0`.
Повтор открытия того же id возвращает тот же ledger; смена профиля — 409.
После перезапуска незавершённая попытка закрывается как `interrupted`;
она не возобновляется. История и занятые резервы автоматически не удаляются.

Учёт бюджета находится в `budget.py`, маршрутизация и передача SSE — в
`app.py`, параметры профилей — в `config/profiles.json` и `profiles.py`.

- `mock` и `mock-alt`: строгий предел 30 000, резерв входа и выхода до запроса.
  Искусственные токены — округлённая вверх четверть длины канонического UTF-8
  JSON. Это определение протокола mock, не токенизатор Luna.
- `luna`: цель 30 000 по фактическому usage; следующий запрос блокируется после
  достижения цели. Последний запрос может превысить её. Отдельный выходной
  предел — 2048 на запрос. Превышение отражается в `over_target`, расход не обрезается.

Отсутствующий usage блокирует следующий запрос, полный расход остаётся
`actual=null`, а `over_target=null`. В `observed_usage` значение `reserved`
отражает только pending output cap, без придуманной верхней границы входа.
Естественное завершение последнего запроса с `over_target=true` не меняет
исход на `budget_exceeded`: этот исход возникает при отказе следующему запросу.
Терминальное SSE-событие передаётся после сохранения usage.
Ограничения потока: 1 MiB на событие, 8 MiB на весь ответ.

Luna использует выделенную ChatGPT-авторизацию. В live override прокси читает
`/auth`, а единственный владелец в harness обновляет credentials штатным Codex
через узкий refresh-маршрут прокси. Прямого внешнего маршрута у harness нет.
Refresh сверяет управляющий токен, id активной auth-попытки и нативный grant;
единственный upstream — `https://auth.openai.com/oauth/token`. Прокси не
обновляет auth.json, не следует redirect и не повторяет refresh. Read-only
[native_auth.py](native_auth.py) возвращает статус готовности без credentials;
заголовки inference доступны только внутреннему HTTP-клиенту посредника.
При 401 до начала SSE допускается одно обновление и повтор запроса; оборванный
поток не воспроизводится. Нативный refresh и полный live-набор B1.1–B5.1
подтверждены; ссылки на результаты находятся в [отчёте](../../docs/report.md).
Незаполненный DeepSeek получает `profile_incomplete`.

Проверки: `python -m pytest tests/test_proxy.py tests/test_budget_modes.py tests/test_native_auth.py`.
Они проверяют HTTP/SSE-цикл
с фиктивным upstream, авторизацию, резерв, отмену и отказ от replay. Эти проверки
не подтверждают реальную маршрутизацию Codex/исполнителя в Docker, source-build
или live-доступ. Токенный бюджет и расход диска учитываются независимо;
история ledger не удаляется автоматически.
