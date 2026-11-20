VENV ?= .venv
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

.PHONY: test-offline preflight preflight-shared up down test-ci test-ui test-infra baf-smoke baf bench-fs bench-sandbox
baf: export BAF_DATASET := $(value DATASET)
baf: export BAF_PROFILE := $(value PROFILE)
baf:
	@if [ -z "$$BAF_DATASET" ] || [ -z "$$BAF_PROFILE" ]; then \
		echo "Укажите DATASET и PROFILE" >&2; exit 2; \
	fi
	$(VENV)/bin/python -m baf.runner --dataset "$$BAF_DATASET" --profile "$$BAF_PROFILE"

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

test-ui:
	$(VENV)/bin/python -m pytest --tb=short -q tests/test_mounted_gui.py tests/test_mounted_client.py

test-infra:
	$(VENV)/bin/python -m tests.infra.offline_suite

baf-smoke:
	$(VENV)/bin/python -m baf.runner --smoke --dataset baf/smoke.yaml --profile mock

# Интерфейс полного задания сохраняется; отложенные функции не дают ложный успех.
bench-fs: UNAVAILABLE = Замеры файловой системы через клиентское монтирование ещё не реализованы
bench-sandbox: UNAVAILABLE = Серия замеров cold start, exec и памяти ещё не реализована
bench-fs bench-sandbox:
	@echo "$@: $(UNAVAILABLE)" >&2
	@exit 2
