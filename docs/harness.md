# Харнес

Выбран upstream Codex 0.159.3, commit `01fc69f4026735edfdf6789820549727a4867b11`, Rust toolchain 1.95.0. Собственный Python-код адаптирует штатный `app-server`; агентный цикл не переписан. Исторически подтверждены ARM64 source-build, нативные mock-сценарии и набор Luna 5/5 с прежней ChatGPT-авторизацией. С 2026-11-14 live-подключение использует общий API-профиль; его проверка с конкретным внешним endpoint требует новой конфигурации и отдельного прогона. Даты и границы доказательств перечислены в [отчёте](report.md).

| Что | Где | Как работает |
|---|---|---|
| Агентный цикл | [upstream session/turn.rs](https://github.com/openai/codex/blob/01fc69f4026735edfdf6789820549727a4867b11/codex-rs/core/src/session/turn.rs) | Штатное чередование Responses и инструментов до завершения или прерывания turn |
| Условие остановки цикла | [adapter.py](../services/harness/adapter.py), [recovery.py](../services/harness/recovery.py) | Завершение turn, отмена, таймаут или ошибка; исчерпание бюджета прекращает новые запросы |
| Диспетчеризация тулов | [upstream tools/router.rs](https://github.com/openai/codex/blob/01fc69f4026735edfdf6789820549727a4867b11/codex-rs/core/src/tools/router.rs) | Выбор штатного обработчика инструмента |
| Сборка контекста | [upstream client_common.rs](https://github.com/openai/codex/blob/01fc69f4026735edfdf6789820549727a4867b11/codex-rs/core/src/client_common.rs), [client.rs](https://github.com/openai/codex/blob/01fc69f4026735edfdf6789820549727a4867b11/codex-rs/core/src/client.rs) | Форматирование истории, инструкций и инструментов для Responses |
| Обработка ошибок тулов | [upstream tools/router.rs](https://github.com/openai/codex/blob/01fc69f4026735edfdf6789820549727a4867b11/codex-rs/core/src/tools/router.rs), [adapter.py](../services/harness/adapter.py) | Результат инструмента и ошибка транспорта различаются; после обрыва команды не воспроизводятся |
| События и остановка | [adapter.py](../services/harness/adapter.py), [recovery.py](../services/harness/recovery.py) | initialize → ephemeral thread → turn; timeout, очистка сессии и отдельной группы relay, устойчивый receipt |
| JSON-RPC | [rpc.py](../services/harness/rpc.py) | Один stdio-канал; запросы не воспроизводятся при обрыве |
| Выбор среды | [settings.py](../services/harness/settings.py) | Чистый CODEX_HOME, include_local=false, явная default-среда, cwd=/work |
| Выбор инструментов | `sandbox_model` в [settings.py](../services/harness/settings.py) | Каталог каждой попытки: исходный model id, прямые unified_exec и freeform apply_patch |
| Адаптер к протоколу сандбокса | [relay.py](../services/harness/relay.py), [tunnel.py](../services/sandbox_manager/tunnel.py) | Аутентифицированный CONNECT к нативному exec-server; в stdout только байты протокола |
| Конфигурация пары «харнес + LLM» | [profiles.json](../config/profiles.json), [settings.py](../services/harness/settings.py) | Профиль выбирает модель/провайдера; реализован адаптер Codex, смена харнесса пока не реализована |
| Владение и сбор | [manager.py](../services/sandbox_manager/manager.py) | Устойчивые run/handle/fingerprint, отдельный сандбокс и доверенный сборщик |
| Жизненный цикл прогона | [runtime.py](../services/gateway/runtime.py), [store.py](../services/gateway/store.py) | Приём до побочных эффектов; восстановление без replay; независимые исходы стадий |
| Модель и токены | [llm_proxy](../services/llm_proxy/README.md) | Строгий резерв mock; остановка API-профиля между запросами по фактическому usage |
| Доступ к провайдеру | [profiles.py](../services/llm_proxy/profiles.py), [providers/](../services/llm_proxy/providers/) | Именованный профиль и адаптер протокола; API-ключ только у proxy |

Настройки не наследуют личный каталог Codex. В окружение агентной команды разрешены только заданные PATH/HOME; inference- и управляющие токены остаются в доверенной части. `externalSandbox` полагается на Docker-ограничения; выбор среды задаётся отдельно через `environments.toml` и `include_local=false`. Нативные проверки сравнивают маркеры двух сред, независимо читают правку и отключают exec-server, проверяя отсутствие локального исполнения. Состояние этих проверок указано в отчёте.

Для любой выбранной модели общий Python-хелпер `sandbox_model` создаёт `model_catalog_json` в каталоге попытки. Он сохраняет исходный `slug`, задаёт `tool_mode=direct`, `shell_type=unified_exec`, `apply_patch_tool_type=freeform`, `use_responses_lite=false`, `node_repl_disabled=true` и короткую инструкцию стенда. Это штатные поля конфигурации Codex 0.159.3: [выбор ToolMode](https://github.com/openai/codex/blob/01fc69f4026735edfdf6789820549727a4867b11/codex-rs/core/src/tools/mod.rs) учитывает метаданные модели, а клиент формирует обычный Responses-запрос с инструкцией и инструментами. Отдельный Node runtime не используется; состав четырёх основных сервисов сохраняется.

В историческом прогоне диалог Luna с исходным каталогом завершился без инструментов и файлов и не прошёл чекер; отрицательный исход сохранён в отчёте. После настройки прямого режима прошли [короткая проверка](../results/live-check-20261106T211931Z-0a10fa/summary.json) и [полный набор B1.1–B5.1](../results/d72a5329d7aa4117bb41aa7995be74c9/report.json): реальные инструменты, полные копии, пройденные чекеры и успешная очистка.

Gateway устойчиво принимает fingerprint запроса до provision. Менеджер восстанавливает handle по run id, не повторяя команду. После исполнения доверенный сборщик прекращает запись процессов агента и читает ограниченную стабильную копию до удаления сандбокса. Полнота файлов и нативных доказательств учитывается отдельно; доверенный чекер работает уже на стороне раннера.

Перед запуском app-server harness записывает приватный nonce попытки и передаёт его только своим процессам. Очистка проверяет сессию, UID, исполняемый файл и маркеры; relay может иметь другой PGID. Нативный итог записывается до удаления CODEX_HOME, а подтверждение освобождения — после очистки. После аварии сохранённый итог возвращается без replay; незавершённая попытка получает `interrupted`. Неизвестная принадлежность процесса или неподтверждённая очистка блокирует новый запуск.

Учёт токенов сосредоточен в [budget.py](../services/llm_proxy/budget.py). Mock резервирует точную верхнюю границу искусственных токенов до запроса. API-профиль использует явный `observed_usage`: цель 30 000 входных и выходных токенов, остановка между запросами, допустимое превышение последним запросом. Прокси добавляет `max_output_tokens=2048`; фактический расход сохраняется без обрезки с признаком `over_target`. При неизвестном usage следующие запросы блокируются. Проверки прежнего Luna endpoint относятся только к историческому маршруту; совместимость нового внешнего endpoint проверяется отдельно. Рабочий профиль не зависит от экспериментальных скриптов или имён их отчётов.

В `observed_usage` поле `reserved` обозначает только выходной cap незавершённого запроса; доказанной границы входа у него нет. Естественно завершившаяся задача может иметь `over_target=true`. `budget_exceeded` возникает, когда требуется следующий запрос после достижения цели. Для строгого mock проверяется сумма подтверждённого расхода и полного резерва входа/выхода.

Профиль `api` разрешает `MODEL_ID` и `MODEL_ENDPOINT` из окружения посредника; имя переменной ключа задаётся `api_key_env`, по умолчанию `MODEL_API_KEY`. Значение ключа не входит в профиль или его снимок. Live Compose передаёт эти переменные только `llm-proxy`; harness получает токен конкретной попытки для внутреннего endpoint. Сандбокс и менеджер API-ключ не получают.

Модули [providers/](../services/llm_proxy/providers/) отделяют upstream-протокол от выполнения задач. Responses поддерживается текущим адаптером. Для другого API нужен отдельный адаптер и явная регистрация; одной смены URL недостаточно. Имена профилей не фиксируются в раннере, параметры разрешённого профиля неизменны внутри попытки.

Device-login, OAuth refresh, auth-only Codex и callback авторизации удалены из текущего пути. Не заданный или отклонённый ключ даёт явную ошибку; автоматического обновления, redirect или повторного inference нет. Прежний `.auth/` не читается runtime и не удаляется миграцией. Старые результаты авторизации остаются историей, а не доказательством нового API-маршрута. Решение: [ADR-0005](adr/0005-api-provider.md).

Ресурсные потолки: одна vCPU, один CPU worker, до 8 000 000 000 байт RAM. Builder ограничен 7 ГБ и выполняется отдельно от стека. `docker/compose.single-cpu.yml` допускается после `preflight --shared-cpu`, который проверяет одну vCPU всего Docker engine; лимит каждого сервиса в этом режиме не является дополнительным CPU.

Для текущего Docker Desktop выделены 32 ГБ диска. Учёт включает toolchain, зависимости, кэш, образы, исходники, Python-окружение, временные копии и историю. Это настройка ёмкости Docker, не обещание свободного места. Материализованные резервы `Storage` защищают место для итогов в прикладной области; отдельная жёсткая квота проекта не задаётся. История и чужие ресурсы не удаляются автоматически ради освобождения места.

Удаление сандбокса не удаляет историю run id. Явное удаление серверной истории оператором завершает гарантию идемпотентности для удалённых id. После аварии незавершённый прогон не переисполняется. Ошибка очистки блокирует следующий запуск. Обычные `down` и `down -v` проверены на реальных контейнерах: собственные среды удаляются, чужие ресурсы и выгруженные результаты сохраняются. API-ключ задаётся вне серверной истории; очистка прогонов не меняет приватную конфигурацию оператора.

## Подготовка и запуск

Нужны Git, uv 0.12.1 и Docker с Compose и Buildx. Docker engine должен иметь
одну vCPU; ограничения памяти и диска описаны выше. Сборку и прогоны
выполняйте по очереди при остановленном стеке.

### Сборка

```bash
uv sync --frozen
.venv/bin/python -m scripts.build_codex
```

Укажите путь `provenance.runtime.json` из завершившейся сборки:

```bash
PROVENANCE='results/source-build-<timestamp>/provenance.runtime.json'
.venv/bin/python -m scripts.configure_local --provenance "$PROVENANCE"
```

### Запуск

После `scripts.configure_local` задайте в приватном `.env` параметры выданного провайдера. `MODEL_ENDPOINT` — base URL без завершающего `/responses`; адаптер добавляет путь запроса. Для удалённого API нужен HTTPS; HTTP разрешён только для localhost или числового loopback в локальной проверке. Например, для Responses-совместимого сервера:

```dotenv
MODEL_ID=<выданный-model-id>
MODEL_ENDPOINT=https://<адрес-провайдера>/v1
MODEL_API_KEY=<секретный-api-ключ>
```

Не коммитьте `.env` и не помещайте ключ в `config/profiles.json`. Для другого имени переменной ключа задайте `api_key_env` в профиле и передайте её только proxy. Поддержка конкретных model id, полей запроса и usage проверяется запросом к этому endpoint; настроенный профиль сам по себе её не доказывает.

Запустите сервер и задачи:

```bash
make up
make checkpoint1 PROFILE=api
make down
```

`make down` удаляет серверную историю. Для остановки с сохранением истории:

```bash
docker compose -f docker-compose.yml -f docker/compose.live.yml -f docker/compose.single-cpu.yml down
```

### Инфраструктурные проверки

Перед запуском набора остановите стек и соберите образы:

```bash
.venv/bin/python -m scripts.preflight --shared-cpu
DOCKER_DEFAULT_PLATFORM=linux/arm64 COMPOSE_PARALLEL_LIMIT=1 docker compose -f docker-compose.yml -f docker/compose.mock.yml -f docker/compose.single-cpu.yml --profile tools build --builder eval-stand-codex-source
docker buildx stop eval-stand-codex-source
make test-infra
```

На x86_64 замените `linux/arm64` на `linux/amd64` и соберите Codex под ту же архитектуру.
