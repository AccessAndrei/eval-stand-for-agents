"""Проверяет решение о пересадке и время между рейсами."""

import json
import sys
from pathlib import Path


def main() -> None:
    answer = Path(sys.argv[1]) / "answer.txt"
    passed = False
    if answer.is_file() and not answer.is_symlink() and answer.stat().st_size <= 65_536:
        try:
            passed = answer.read_text(encoding="utf-8").split() == ["yes", "35"]
        except UnicodeError:
            pass
    result = {"pass": passed, "reason": "ok" if passed else "Ожидался ответ yes 35"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
