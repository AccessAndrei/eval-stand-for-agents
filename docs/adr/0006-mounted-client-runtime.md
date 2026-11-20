# ADR-0006: Смонтированные сессии и продолжение чата

- Статус: accepted
- Дата: 2026-11-20

## Контекст

Сессия клиента связывает владельца, выбранную папку, сандбокс и thread Codex.
Каждый turn использует собственный `run_id`, полномочие inference и бюджет.
После отмены сессия должна продолжиться в том же `/work` с прежним контекстом.

## Решение

- Gateway сохраняет хеш подтверждения владения и fingerprint запроса открытия
  до выделения ресурсов. Общие GUI и headless-клиент используют API v1.
- Обратный SSH-туннель и SSHFS VolumeDriver подключают папку в `/work`. Engine
  передаёт агенту готовый том. SSHFS, устройство FUSE и Docker socket доступны
  только доверенным компонентам с соответствующими ролями.
- Новый turn запускает отдельный Codex app-server. `thread/start` создаёт
  возобновляемый thread, `thread/resume` использует его контекст в ограниченном
  tmpfs сессии. В устойчивой истории остаются идентификаторы, исходы и метрики.
- Manager берёт подтверждение входа exec-server в сандбокс до передачи CONNECT.
  При завершении turn он закрывает старый relay, ставит контейнер на паузу
  через Engine, останавливает процессы UID агента и проверяет их отсутствие после
  снятия паузы. Прокси отзывает старое полномочие, завершает запрос к модели
  и возвращает учёт по исходной паре `session_id` и `run_id`. Сохраняется хеш
  inference-токена. Gateway допускает следующий turn после записи итога и
  подтверждений от Manager, Harness и прокси.

## Альтернативы

| Вариант | Плюсы | Минусы | Почему не выбран |
|---|---|---|---|
| Один app-server на сессию | Один процесс держит контекст чата | `turn/interrupt` оставлял exec-server и писателей активными | Завершение процесса и новый запуск дают проверяемую границу turn |
| WebDAV + rclone | Файловый доступ по HTTP | Отложенная запись и VFS-кэш усложняют подтверждение изменений | SSHFS с обратным туннелем прошёл сквозную проверку в целевой ВМ |
| NFS + WireGuard | Нативный каталог Linux | Нужны VPN, экспорт и отдельное управление правами | SSHFS использует один файловый канал, привязанный к сессии |

## Последствия

На Linux-ВМ Docker Desktop проверен сценарий A → отмена → B на штатном стеке.
Во время A одновременно работали команда записи и mock-запрос. После отмены
прекратились записи, relay и запрос к модели. B получил контекст A, выполнил
инструмент в том же контейнере и томе и получил собственный `run_id`. Старый
CONNECT и старый grant получили отказ. Итог A сохранил неизвестный расход,
итог B сохранил собственные входные и выходные токены. Проверка строгого
бюджета учла расход первого запроса и отклонила следующий до отправки в mock.

### Воспроизведение

Используйте одноядерный Docker Engine с FUSE и выбранный BuildKit из
[инструкции запуска](../../README.md#запуск-сервера). Команды рассчитаны на
изолированный mock-проект. Например, для ARM64 на Docker Desktop:

```bash
uv sync --frozen
export DOCKER_DEFAULT_PLATFORM=linux/arm64
umask 077
probe_dir=$(mktemp -d)
probe_project="eval-resume-probe-$(openssl rand -hex 4)"
probe_env="$probe_dir/compose.env"

.venv/bin/python - "$probe_env" "$probe_project" <<'PY'
import secrets
import socket
import sys
from pathlib import Path

env_path, project = sys.argv[1:]
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    ssh_port = listener.getsockname()[1]
settings = {
    "COMPOSE_PROJECT_NAME": project,
    "PROJECT_ID": project,
    "DOCKER_PLATFORM": "linux/arm64",
    "GATEWAY_PORT": "0",
    "WORKSPACE_SSH_PORT": str(ssh_port),
    "PROBE_CONTEXT": "context-" + secrets.token_hex(20),
}
for service in ("CLIENT", "MANAGER", "HARNESS", "PROXY", "BROKER"):
    settings[service + "_TOKEN"] = secrets.token_urlsafe(32)
Path(env_path).write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
PY

base=(docker compose --env-file "$probe_env" -p "$probe_project"
  -f docker-compose.yml -f docker/compose.mock.yml -f docker/compose.single-cpu.yml)
resume=("${base[@]}" -f docker/compose.resume-probe.yml)

"${resume[@]}" up --build --wait
probe_gateway="http://$("${resume[@]}" port gateway 8080)"
.venv/bin/python -m tests.infra.mounted_resume_probe \
  --project "$probe_project" --gateway "$probe_gateway" --env-file "$probe_env"
"${resume[@]}" down -v

MOCK_SCRIPT_PATH=/mock-scripts/baf-smoke/control-playlist.json \
  "${base[@]}" up --no-build --pull never --wait
probe_gateway="http://$("${base[@]}" port gateway 8080)"
CLIENT_TOKEN="$(sed -n 's/^CLIENT_TOKEN=//p' "$probe_env")" \
  GATEWAY_URL="$probe_gateway" \
  .venv/bin/python -m baf.runner --dataset baf/control.yaml --profile mock \
  --results "$probe_dir/control"
"${base[@]}" down -v

control_report=$(find "$probe_dir/control" -name report.json -print)
.venv/bin/python -m scripts.analyze_baf --dataset baf/control.yaml --report "$control_report" --output "$probe_dir/analysis"

make check
.venv/bin/python -m pytest -q
```
