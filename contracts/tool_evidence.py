"""Разрешённый вызов инструмента для доверенного доказательства Б5.1."""

import shlex

B5_COMMAND = "/usr/local/bin/python3 -I -S /work/input/stats.py /work/input/data.csv"
B5_COMMAND_KIND = "b5_median"
B5_CWD = "/work"
B5_SOURCES = ("agent", "unifiedExecStartup")


def is_b5_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    if command == B5_COMMAND:
        return True
    try:
        # Codex кодирует argv через shlex_join. Тело оболочки проверяется точно.
        return shlex.split(command, posix=True) == ["/usr/bin/bash", "-lc", B5_COMMAND]
    except ValueError:
        return False
