"""Доверенный чекер поиска определения функции."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from baf.runner.task_checks import check_search  # noqa: E402

if __name__ == "__main__":
    print(json.dumps(check_search(Path(sys.argv[1])), ensure_ascii=False))
