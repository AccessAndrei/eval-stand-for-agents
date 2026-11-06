VENV ?= .venv

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

# Заготовки команд среды исполнения.
.PHONY: up down test-ci test-ui test-infra baf-smoke baf bench-fs bench-sandbox
up:            ; docker compose up --build -d
down:          ; docker compose down -v
test-ci:       test-ui test-infra baf-smoke
test-ui:       ; @echo "TODO: UI-микросценарии"
test-infra:    ; @echo "TODO: инфраструктурные тесты (mock LLM)"
baf-smoke:     ; @echo "TODO: по одной простой задаче на БАФ, 1 повтор"
baf:           ; @echo "TODO: полный БАФ для PAIR=$(PAIR), 3 повтора"
bench-fs:      ; @echo "TODO: замеры ФС через монтирование"
bench-sandbox: ; @echo "TODO: замеры сандбокса"
