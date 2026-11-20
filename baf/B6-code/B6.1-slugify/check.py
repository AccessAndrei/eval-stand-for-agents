"""Отмечает отсутствие файла или ожидаемую проверку в изолированной среде."""

import json
import sys
from pathlib import Path


def main() -> None:
    answer = Path(sys.argv[1]) / "slug.py"
    if not answer.is_file() or answer.is_symlink():
        result = {"pass": False, "reason": "slug.py отсутствует либо недопустим"}
    else:
        result = {
            "pass": None,
            "checker_status": "not_run_evidence_incomplete",
            "reason": "Для оценки slug.py требуется изолированный запуск скрытых тестов",
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
