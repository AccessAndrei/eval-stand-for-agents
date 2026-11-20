"""Проверяет итоговую сумму продаж за первый квартал 2026 года."""

import json
import sys
from pathlib import Path


def main() -> None:
    answer = Path(sys.argv[1]) / "answer.txt"
    passed = False
    if answer.is_file() and not answer.is_symlink() and answer.stat().st_size <= 65_536:
        try:
            passed = answer.read_text(encoding="utf-8").strip() == "1025"
        except UnicodeError:
            pass
    result = {"pass": passed, "reason": "ok" if passed else "Ожидалась сумма 1025"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
