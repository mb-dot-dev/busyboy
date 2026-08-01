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
