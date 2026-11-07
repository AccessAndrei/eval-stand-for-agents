from services.common.http import valid_bearer


def test_invalid_unicode_bearer_is_rejected_without_exception() -> None:
    token = "private-test-token-which-is-long-enough"
    assert valid_bearer("Bearer " + token, token)
    assert not valid_bearer("Bearer неверный", token)
    assert not valid_bearer("Bearer \udcff", token)
    assert not valid_bearer("", token)
    assert not valid_bearer("Bearer ", "")
