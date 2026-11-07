from pathlib import Path

import pytest

from baf.runner.catalog import load_checkpoint
from baf.runner.checkers import run_checker


@pytest.mark.parametrize("content", [b"\xff\xfe", b"x" * 70000])
def test_invalid_answer_is_wrong_answer_not_checker_failure(
    tmp_path: Path, content: bytes
) -> None:
    (tmp_path / "answer.txt").write_bytes(content)
    result = run_checker(
        load_checkpoint()[0],
        tmp_path,
        run_id="invalid-answer",
        collection_status="complete",
    )
    assert result["checker_status"] == "failed"
    assert result["pass"] is False
