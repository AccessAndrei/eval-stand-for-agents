"""Проверка медианы; доверенное доказательство передаётся через stdin."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from baf.runner.task_checks import check_tool  # noqa: E402

if __name__ == "__main__":
    evidence = json.loads(sys.stdin.read(1_000_001))
    result = check_tool(
        Path(sys.argv[1]), Path(__file__).resolve().parent, evidence, sys.argv[2]
    )
    print(json.dumps(result, ensure_ascii=False))
