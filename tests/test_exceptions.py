"""Tests for the BUSY Bar delivery exception hierarchy."""

from busyboy import exceptions


def test_bar_api_error_is_a_bar_error():
    error = exceptions.BarAPIError("bad request", code=400, status_code=400, method="POST", path="/api/display/draw")

    assert isinstance(error, exceptions.BarError)


def test_bar_api_error_message_includes_status_method_and_path():
    error = exceptions.BarAPIError("bad request", code=400, status_code=400, method="POST", path="/api/display/draw")

    message = exceptions.format_delivery_error(error)

    assert "HTTP 400" in message
    assert "POST /api/display/draw" in message
    assert "bad request" in message
    assert "code=400" in message


def test_bar_api_error_without_a_code_omits_it():
    error = exceptions.BarAPIError("bad request", code=None, status_code=400, method="POST", path="/api/display/draw")

    message = exceptions.format_delivery_error(error)

    assert "code=" not in message


def test_bar_request_error_is_a_bar_error():
    error = exceptions.BarRequestError("connection refused", method="POST", path="/api/display/draw", attempts=3)

    assert isinstance(error, exceptions.BarError)


def test_bar_request_error_message_includes_method_path_and_attempts():
    error = exceptions.BarRequestError("connection refused", method="POST", path="/api/display/draw", attempts=3)

    message = exceptions.format_delivery_error(error)

    assert "POST /api/display/draw" in message
    assert "attempts=3" in message
    assert "connection refused" in message


def test_every_expected_failure_shares_one_base():
    assert issubclass(exceptions.BarError, exceptions.BusyboyError)
    assert issubclass(exceptions.GitError, exceptions.BusyboyError)
    assert issubclass(exceptions.GitHubError, exceptions.BusyboyError)


def test_github_auth_and_transient_failures_are_distinguishable():
    assert issubclass(exceptions.GitHubAuthError, exceptions.GitHubError)
    assert issubclass(exceptions.GitHubTransientError, exceptions.GitHubError)
    assert not issubclass(exceptions.GitHubAuthError, exceptions.GitHubTransientError)


def test_a_non_bar_failure_formats_as_its_message():
    assert exceptions.format_delivery_error(exceptions.GitError("no origin remote")) == "no origin remote"


def test_claude_auth_error_is_not_transient():
    error = exceptions.ClaudeAuthError("expired")

    assert isinstance(error, exceptions.ClaudeError)
    assert not isinstance(error, exceptions.ClaudeTransientError)


def test_claude_transient_error_carries_retry_after():
    error = exceptions.ClaudeTransientError("slow down", retry_after=30)

    assert isinstance(error, exceptions.ClaudeError)
    assert error.retry_after == 30


def test_claude_transient_error_retry_after_defaults_to_none():
    assert exceptions.ClaudeTransientError("boom").retry_after is None


def test_claude_errors_render_as_their_message():
    assert exceptions.format_delivery_error(exceptions.ClaudeAuthError("expired")) == "expired"


def test_claude_errors_are_not_github_errors():
    assert not issubclass(exceptions.ClaudeError, exceptions.GitHubError)
    assert issubclass(exceptions.ClaudeError, exceptions.BusyboyError)
