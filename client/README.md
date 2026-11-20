# Десктоп-клиент

Клиент macOS использует Tkinter, общий асинхронный HTTP-клиент и AsyncSSH.
Установите зависимости из `uv.lock` в окружение Python с Tk. Команда
`python -m tkinter` открывает пробное окно и подтверждает наличие Tk.

```bash
uv sync --frozen
.venv/bin/python -m client.gui
```

Укажите адрес Gateway, токен и профиль модели. Выберите папку и режим
`ro` или `rw`, подключитесь и дождитесь готовности `/work`. Окно показывает
чат, события инструментов и итог запуска. Из окна можно отменить команду,
дочитать поток после разрыва HTTP и закрыть сессию.

## Headless / CLI-режим

Передайте токен через `CLIENT_TOKEN` и запустите одну сессию с сообщением.

```bash
.venv/bin/python -m client --gateway http://127.0.0.1:8080 \
  --directory ./workdir --mode rw --profile mock --prompt "Прочитай README.md"
```

CLI печатает события NDJSON. Для повторного подключения и отмены в тестах
используйте `MountedClient` из [session.py](session.py), общий для CLI и GUI.
