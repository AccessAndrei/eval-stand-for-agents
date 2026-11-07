VENV ?= .venv
PROFILE ?= luna
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

test-offline:
	$(VENV)/bin/python -m tests.infra.offline_suite

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

test-ui:
	@echo "Десктоп-клиент отложен; UI-тестов пока нет" >&2
	@exit 2

test-infra:
	$(VENV)/bin/python -m pytest -q tests/test_contracts.py tests/test_manager.py tests/test_manager_tunnel.py tests/test_proxy.py tests/test_gateway_store.py tests/test_gateway_runtime.py tests/test_storage.py tests/test_harness.py

baf-smoke:
	@echo "Реальный smoke-БАФ требует подтверждённого Docker-маршрута; компонентные проверки: make test-checkpoint1" >&2
	@exit 2

baf:
	@echo "Полный БАФ-бенчмарк ещё не реализован; checkpoint1 не заменяет полный каталог" >&2
	@exit 2

bench-fs bench-sandbox:
	@echo "Реальные замеры пока не реализованы" >&2
	@exit 2
