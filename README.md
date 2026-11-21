# eval-stand-for-agents: среда исполнения агента

## Что это

Стенд для оценки пар «харнес + LLM» по базисным агентским функциям.
Использует Codex и отдельный Docker-сандбокс на задачу.
Сейчас доступны B1.1–B5.1.

## Архитектура

![Архитектура](docs/images/runtime_architecture.svg)

[Харнес](docs/harness.md), [контракты](contracts/README.md), [ADR](docs/adr/README.md).

## Запуск сервера

Нужны uv и Docker с Compose и Buildx.
Перед первым запуском выполните [подготовку окружения](docs/harness.md#подготовка-и-запуск).

```bash
make up
make down
```

## Клиент

Десктоп-клиент и монтирование рабочей папки пока не реализованы.
[Требования](client/README.md).

## Тесты

| Команда | Что запускает |
|---|---|
| `make test-ci` | UI + инфраструктурные тесты + smoke-БАФ |
| `make test-ui` | UI-тесты клиента |
| `make test-infra` | Инфраструктурные тесты |
| `make test-checkpoint1` | Компонентные тесты |
| `make checkpoint1 PROFILE=api` | B1.1–B5.1 |
| `make baf PAIR=<harness>+<model>` | Полный БАФ-бенчмарк для пары |
| `make bench-fs` | Замеры ФС через монтирование |
| `make bench-sandbox` | Замеры сандбокса |
| `make check` | Линтер и проверка форматирования |
| `make pre-commit` | Проверки pre-commit |

UI, smoke-БАФ, полный БАФ и замеры пока не реализованы; их команды завершаются ошибкой.
Установка хуков: `.venv/bin/pre-commit install`.

Анализ оценочных данных: `.venv/bin/python -m scripts.analyze_baf`.
[Данные, графики и выводы](docs/eda.md).

## Структура

```text
services/                      gateway, harness, sandbox_manager, llm_proxy
client/                        десктоп-клиент (план)
contracts/                     схемы протоколов
baf/                           задачи БАФ и раннер
tests/ui/                      UI-тесты (план)
tests/infra/                   инфраструктурные тесты
docker/                        Dockerfile и конфигурации Compose
scripts/                       подготовка и запуск
docs/adr/                      архитектурные решения
docs/harness.md                устройство харнесса
docs/baf.md                    свои БАФ и правила оценки
docs/report.md                 замеры, матрица БАФ × пара, выводы
docs/ml_system_design_doc.md    дизайн-документ
docs/eda.md                     анализ оценочных данных
results/                       результаты прогонов
```

## Модели

Текущий профиль `api` получает модель и endpoint из `MODEL_ID` и
`MODEL_ENDPOINT`; API-ключ `MODEL_API_KEY` доступен только LLM-прокси.
Для совместимого Responses endpoint достаточно настроить профиль; другой
формат API подключается отдельным адаптером. [Настройка](docs/harness.md#запуск).

Исторический набор выполнен на Luna (`gpt-6-luna`) с прежней
ChatGPT-авторизацией. Он не подтверждает live-работу нового API-профиля.
