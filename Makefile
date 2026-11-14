VENV ?= .venv
PROFILE ?= api
DOCKER_PLATFORM ?= $(if $(filter arm64 aarch64,$(shell uname -m)),linux/arm64,linux/amd64)
export DOCKER_DEFAULT_PLATFORM = $(DOCKER_PLATFORM)
export COMPOSE_PARALLEL_LIMIT = 1
COMPOSE_LIVE = docker compose -f docker-compose.yml -f docker/compose.live.yml -f docker/compose.single-cpu.yml

.PHONY: lint format format-check check pre-commit
lint:
	$(VENV)/bin/ruff check .

format:
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

format-check:
	$(VENV)/bin/ruff format --check .

check: lint format-check

pre-commit:
	$(VENV)/bin/pre-commit run --all-files

.PHONY: checkpoint1 test-checkpoint1 test-offline preflight preflight-shared up down test-ci test-ui test-infra baf-smoke baf bench-fs bench-sandbox
checkpoint1: preflight-shared
	$(COMPOSE_LIVE) run --rm --no-deps --pull never runner --profile $(PROFILE) --results /results

test-checkpoint1:
	$(VENV)/bin/python -m pytest -q

# Совместимое имя; основная команда инфраструктурных проверок — из шаблона.
test-offline: test-infra

preflight:
	$(VENV)/bin/python -m scripts.preflight

preflight-shared:
	$(VENV)/bin/python -m scripts.preflight --shared-cpu

up: preflight-shared
	@running="$$( $(COMPOSE_LIVE) ps --status running --services )" || exit $$?; \
	if test -n "$$running"; then \
		echo "Перед сборкой остановите стек: тяжёлая сборка и прогоны выполняются по очереди" >&2; \
		exit 2; \
	fi
	$(COMPOSE_LIVE) --profile tools build --builder eval-stand-codex-source
	docker buildx stop eval-stand-codex-source
	$(COMPOSE_LIVE) up --no-build --pull never -d --wait --wait-timeout 360

down:
	$(VENV)/bin/python -m scripts.shutdown --down

test-ci: test-ui test-infra baf-smoke

test-infra:
	$(VENV)/bin/python -m tests.infra.offline_suite

# Интерфейс полного задания сохраняется; отложенные функции не дают ложный успех.
test-ui: UNAVAILABLE = Десктоп-клиент и UI-тесты ещё не реализованы
baf-smoke: UNAVAILABLE = Smoke по всем восьми БАФ ещё не реализован; checkpoint1 охватывает B1–B5
baf: UNAVAILABLE = Полный каталог БАФ и сравнение пар ещё не реализованы
bench-fs: UNAVAILABLE = Замеры файловой системы через клиентское монтирование ещё не реализованы
bench-sandbox: UNAVAILABLE = Серия замеров cold start, exec и памяти ещё не реализована
test-ui baf-smoke baf bench-fs bench-sandbox:
	@echo "$@: $(UNAVAILABLE)" >&2
	@exit 2
