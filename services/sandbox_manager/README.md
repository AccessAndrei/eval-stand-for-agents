# sandbox-manager

Реализация — этот Python-пакет; Docker target
`manager` в [общем Dockerfile](../../docker/Dockerfile.services). Это единственный основной сервис
с Docker socket. Внешний запрос не может выбрать произвольную команду Docker.
Полномочия, изоляция и сбор файлов определены в
[ADR-0002](../../docs/adr/0002-sandbox-authority.md).

[server.py](server.py) принимает защищённые `MANAGER_TOKEN`
запросы provision, lookup по run id, collect, release, cancel и shutdown на 8081.
[tunnel.py](tunnel.py) на 8082 запускает фиксированный
`codex exec-server --listen stdio` и отделяет stderr от протокольного stdout.

[manager.py](manager.py) связывает project/run/handle и
fingerprint с принадлежащими проекту контейнерами. Повтор provision не создаёт
вторую среду. [recovery.py](recovery.py) сверяет ресурсы после
аварии; чужие контейнеры не удаляются.

Рабочий сандбокс непривилегирован, без сети, с read-only root и ограниченными
CPU/RAM/PIDs/tmpfs. Для B5 область `/work/input` защищена от подмены.
Отдельный доверенный сборщик прекращает процессы агента и читает стабильные
файлы до удаления среды. Неполный сбор остаётся явным исходом; файлы и авторизация
контроллеров в рабочую среду не копируются.

Жизненный цикл и ресурсные пределы: [harness.md](../../docs/harness.md).
Обоснование полномочий: [ADR](../../docs/adr/README.md). Воспроизведение и
результаты проверок: [README](../../README.md), [отчёт](../../docs/report.md).
