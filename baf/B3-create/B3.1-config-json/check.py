"""Доверенный чекер JSON по закреплённой схеме."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from baf.runner.task_checks import check_json  # noqa: E402

if __name__ == "__main__":
    result = check_json(Path(sys.argv[1]), Path(__file__).resolve().parent)
    print(json.dumps(result, ensure_ascii=False))
