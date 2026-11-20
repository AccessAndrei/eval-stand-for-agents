"""Одно окно для работы с локальной папкой через общий клиент."""

from __future__ import annotations

import asyncio
import queue
import secrets
import sys
import threading
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import tkinter as tk

    from client.session import MountedClient, MountedSession

RUN_TIMEOUT = 120
CLOSE_TIMEOUT = 90
RESULTS = {
    "success": "успех",
    "command_error": "ошибка команды",
    "timeout": "истёк таймаут",
    "sandbox_error": "ошибка сандбокса",
    "transport_error": "ошибка транспорта",
    "provider_error": "ошибка модели или провайдера",
    "auth_error": "ошибка авторизации модели",
    "cancelled": "отменён",
    "interrupted": "прерван",
    "budget_exceeded": "исчерпан бюджет токенов",
    "storage_budget_exceeded": "исчерпан лимит хранилища",
    "not_started": "не запущен",
}


def _mounted_client(address: str, token: str) -> MountedClient:
    from client.session import MountedClient

    return MountedClient(address, token)


def _error(operation: str, error: Exception) -> str:
    status = getattr(error, "http_status", None)
    if status in (401, 403):
        return "Gateway отклонил токен или доступ к сессии."
    code = getattr(error, "code", None)
    if code == "storage_budget_exceeded":
        return "Gateway исчерпал лимит хранилища."
    if code in {"ssh_host_key_mismatch", "ssh_key_rejected"}:
        return "SSH-брокер отклонил подключение или его ключ изменился."
    if operation == "connect":
        if code in {
            "export_failed",
            "mount_failed",
            "mount_timeout",
            "ssh_forward_rejected",
            "ssh_tunnel_failed",
            "tunnel_not_ready",
            "workspace_unavailable",
        }:
            return "Не удалось смонтировать выбранную папку."
        return (
            "Не удалось подключиться. Проверьте адрес, профиль и доступность Gateway."
        )
    return "Операция не подтверждена сервером."


class _Chat:
    def __init__(self, notify: Callable[[str, str], None]) -> None:
        self.notify = notify
        self._client = AsyncExitStack()
        self._session: MountedSession | None = None
        self._task: asyncio.Task[Any] | None = None
        self._closing_task: asyncio.Task[bool] | None = None
        self._run_id: str | None = None
        self._cursor = 0

    async def connect(
        self, address: str, token: str, folder: Path, profile: str, mode: str
    ) -> None:
        self._task = asyncio.current_task()
        self._closing_task = None
        try:
            client = await self._client.enter_async_context(
                _mounted_client(address, token)
            )
            self._session = client.session(folder, profile=profile, mode=mode)
            await self._session.mount()
            self.notify("ready", f"Папка подключена в /work ({mode}).")
        except Exception as error:
            message = _error("connect", error)
            if not await self._release():
                message += " Освобождение сессии не подтверждено."
            self.notify("disconnected", message)
        finally:
            self._task = None

    async def send(self, prompt: str) -> None:
        session = self._session
        if session is None:
            return
        self._task = asyncio.current_task()
        self._run_id = secrets.token_hex(16)
        self._cursor = 0
        try:
            await session.start_run(prompt, timeout_s=RUN_TIMEOUT, run_id=self._run_id)
        except Exception as error:
            if getattr(error, "http_status", None) in (400, 401, 403, 404, 409, 507):
                self._run_id = None
                self.notify("ready", _error("send", error))
            else:
                self.notify(
                    "paused", "Приём сообщения не подтверждён. Проверьте поток."
                )
            self._task = None
            return
        await self.watch()

    async def watch(self) -> None:
        session, run_id = self._session, self._run_id
        if session is None or run_id is None:
            return
        self._task = asyncio.current_task()
        try:
            async for event in session.events(
                run_id, after=self._cursor, timeout=RUN_TIMEOUT + CLOSE_TIMEOUT
            ):
                self._cursor = event["seq"]
                event_type = event["type"]
                if event_type == "message":
                    self.notify("chat", f"\nАгент: {event['text']}\n")
                elif event_type == "result":
                    self._finish(event["result"])
                    return
                elif event_type == "tool":
                    tool = event["tool"]
                    subject = (
                        "Команда"
                        if tool["type"] == "commandExecution"
                        else "Правка файла"
                    )
                    action = "началась" if tool["phase"] == "started" else "завершилась"
                    code = tool.get("exit_code")
                    details = f" (код {code})" if type(code) is int else ""
                    self.notify("log", f"{subject} {action}{details}.")
            self.notify("paused", "Поток прервался до получения итога.")
        except Exception as error:
            if getattr(error, "code", None) == "history_unavailable":
                oldest = getattr(error, "earliest_seq", None)
                message = "Часть событий недоступна."
                if type(oldest) is int and oldest > self._cursor:
                    self._cursor = oldest - 1
                    message += f" Первый доступный номер - {oldest}."
                self.notify("log", message)
                try:
                    status = await session.run_status(run_id)
                except Exception as status_error:
                    self.notify("paused", _error("stream", status_error))
                else:
                    result = status.get("result")
                    if status.get("state") == "terminal" and isinstance(result, dict):
                        self._finish(result)
                    else:
                        self.notify(
                            "paused",
                            "Запуск продолжается. Доступную часть потока можно возобновить.",
                        )
            else:
                self.notify("paused", _error("stream", error))
        finally:
            self._task = None

    def _finish(self, result: dict[str, Any]) -> None:
        self._run_id = None
        code = result.get("execution_status")
        message = (
            RESULTS.get(code, "неизвестный исход")
            if isinstance(code, str)
            else "неизвестный исход"
        )
        self.notify("log", f"Исход запуска - {message}.")
        self.notify("ready", f"Запуск завершён ({message}).")

    async def cancel(self) -> None:
        if self._session is None or self._run_id is None:
            return
        try:
            await self._session.cancel_run(self._run_id)
            self.notify("log", "Запрос на отмену принят.")
        except Exception as error:
            self.notify("log", _error("cancel", error))

    async def disconnect(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if await self._release():
            self.notify("disconnected", "Сессия закрыта.")
        else:
            self.notify("cleanup_failed", "Освобождение сессии не подтверждено.")

    async def _release(self) -> bool:
        if self._closing_task is None:
            self._closing_task = asyncio.create_task(self._close_session())
        return await asyncio.shield(self._closing_task)

    async def _close_session(self) -> bool:
        session, self._session = self._session, None
        self._run_id = None
        ok = True
        try:
            if session is not None:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    ok = (await session.close()).ok
        except Exception:
            ok = False
        try:
            async with asyncio.timeout(5):
                await self._client.aclose()
        except Exception:
            ok = False
        return ok


class _Window:
    def __init__(self, root: tk.Tk) -> None:
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.root.title("eval-stand-for-agents")
        self.root.minsize(680, 520)
        self._messages: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._chat = _Chat(lambda kind, text: self._messages.put((kind, text)))
        self._state = "disconnected"
        self._exit = False

        controls = ttk.Frame(root, padding=12)
        controls.pack(fill="both", expand=True)
        controls.columnconfigure(1, weight=1)
        controls.rowconfigure(5, weight=3)
        controls.rowconfigure(6, weight=1)

        self._address = tk.StringVar(value="http://127.0.0.1:8080")
        self._token = tk.StringVar()
        self._profile = tk.StringVar(value="api")
        self._folder = tk.StringVar()
        self._mode = tk.StringVar(value="rw")
        self._status = tk.StringVar(value="Выберите папку и подключитесь к Gateway.")
        self._fields = []
        for row, (label, value) in enumerate(
            (
                ("Адрес Gateway", self._address),
                ("Токен", self._token),
                ("Профиль", self._profile),
            )
        ):
            ttk.Label(controls, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=2
            )
            entry = ttk.Entry(
                controls, textvariable=value, show="•" if value is self._token else ""
            )
            entry.grid(row=row, column=1, sticky="ew", columnspan=3, pady=2)
            self._fields.append(entry)

        folder_button = ttk.Button(
            controls, text="Выбрать папку…", command=self._choose_folder
        )
        folder_button.grid(row=3, column=0, sticky="w", pady=4)
        self._fields.append(folder_button)
        ttk.Entry(controls, textvariable=self._folder, state="readonly").grid(
            row=3, column=1, columnspan=3, sticky="ew"
        )
        ttk.Label(controls, text="Режим доступа").grid(row=4, column=0, sticky="w")
        for column, (mode, caption) in enumerate(
            (("rw", "Чтение и запись"), ("ro", "Только чтение")), 1
        ):
            button = ttk.Radiobutton(
                controls, text=caption, value=mode, variable=self._mode
            )
            button.grid(row=4, column=column, sticky="w", padx=(0, 8))
            self._fields.append(button)
        self._connect_button = ttk.Button(
            controls, text="Подключить", command=self._connect
        )
        self._connect_button.grid(row=4, column=3, padx=(8, 0), sticky="e")

        self._transcript = ScrolledText(
            controls, height=14, wrap="word", state="disabled"
        )
        self._transcript.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(8, 4))
        self._history = ScrolledText(controls, height=4, wrap="word", state="disabled")
        self._history.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=(0, 4))
        self._prompt = tk.Text(controls, height=3, wrap="word")
        self._prompt.grid(row=7, column=0, columnspan=3, sticky="ew", pady=4)
        self._send_button = ttk.Button(controls, text="Отправить", command=self._send)
        self._send_button.grid(row=7, column=3, padx=(8, 0), sticky="e")
        ttk.Label(controls, textvariable=self._status, wraplength=445).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=4
        )
        self._reconnect_button = ttk.Button(
            controls, text="Возобновить поток", command=self._reconnect
        )
        self._reconnect_button.grid(row=8, column=2, sticky="e", padx=(8, 0))
        self._cancel_button = ttk.Button(
            controls, text="Отменить запуск", command=self._cancel
        )
        self._cancel_button.grid(row=8, column=3, sticky="e", padx=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self._render()
        self.root.after(40, self._receive)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*tasks, return_exceptions=True)
            )
            self._loop.close()

    def _submit(self, work: Coroutine[Any, Any, None]) -> None:
        asyncio.run_coroutine_threadsafe(work, self._loop)

    def _choose_folder(self) -> None:
        from tkinter import filedialog

        folder = filedialog.askdirectory(
            parent=self.root, mustexist=True, title="Папка для /work"
        )
        if folder:
            self._folder.set(folder)

    def _connect(self) -> None:
        if self._state != "disconnected":
            self._disconnect()
            return
        address, token, profile = (
            self._address.get().strip(),
            self._token.get().strip(),
            self._profile.get().strip(),
        )
        folder = self._folder.get()
        if not all((address, token, profile, folder)):
            self._status.set("Укажите адрес, токен, профиль и папку.")
            return
        self._set_state("connecting", "Подключение и монтирование папки…")
        self._submit(
            self._chat.connect(address, token, Path(folder), profile, self._mode.get())
        )

    def _send(self) -> None:
        prompt = self._prompt.get("1.0", "end-1c")
        if self._state != "ready" or not prompt.strip():
            return
        self._prompt.delete("1.0", "end")
        self._append(self._transcript, f"\nВы: {prompt}\n")
        self._set_state("running", "Агент выполняет запрос…")
        self._submit(self._chat.send(prompt))

    def _reconnect(self) -> None:
        self._set_state("running", "Возобновление потока…")
        self._submit(self._chat.watch())

    def _cancel(self) -> None:
        self._submit(self._chat.cancel())

    def _disconnect(self) -> None:
        self._set_state("disconnecting", "Закрытие сессии и освобождение ресурсов…")
        self._submit(self._chat.disconnect())

    def _quit(self) -> None:
        self._exit = True
        if self._state == "disconnected":
            self._loop.call_soon_threadsafe(self._loop.stop)
            self.root.destroy()
        elif self._state != "disconnecting":
            self._disconnect()

    def _receive(self) -> None:
        for _ in range(100):
            try:
                kind, text = self._messages.get_nowait()
            except queue.Empty:
                break
            if kind == "chat":
                self._append(self._transcript, text)
            elif kind == "log":
                self._append(self._history, text + "\n")
            elif self._state != "disconnecting" or kind in {
                "disconnected",
                "cleanup_failed",
            }:
                self._set_state(
                    "disconnected" if kind == "cleanup_failed" else kind, text
                )
                if self._exit and self._state == "disconnected":
                    if kind == "cleanup_failed":
                        from tkinter import messagebox

                        messagebox.showwarning(
                            "Закрытие сессии", text, parent=self.root
                        )
                    self._quit()
                    return
        self.root.after(40, self._receive)

    @staticmethod
    def _append(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", content)
        widget.see("end")
        widget.configure(state="disabled")

    def _set_state(self, state: str, text: str) -> None:
        self._state = state
        self._status.set(text)
        self._render()

    def _render(self) -> None:
        disconnected = self._state == "disconnected"
        for item in self._fields:
            item.configure(state="normal" if disconnected else "disabled")
        self._connect_button.configure(
            text="Подключить" if disconnected else "Закрыть сессию",
            state="disabled" if self._state == "disconnecting" else "normal",
        )
        self._send_button.configure(
            state="normal" if self._state == "ready" else "disabled"
        )
        self._reconnect_button.configure(
            state="normal" if self._state == "paused" else "disabled"
        )
        self._cancel_button.configure(
            state="normal" if self._state in {"running", "paused"} else "disabled"
        )


def main() -> int:
    try:
        import tkinter as tk
    except ImportError:
        print(
            "Для графического клиента нужен Tk. Для Homebrew Python 3.13 есть python-tk@3.13.",
            file=sys.stderr,
        )
        return 2
    try:
        root = tk.Tk()
    except tk.TclError:
        print("Не удалось открыть окно Tk.", file=sys.stderr)
        return 2
    _Window(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
