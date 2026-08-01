"""Tests for exit codes and output of the busyboy command line."""

import json
import traceback

from click.testing import CliRunner
import httpx
import pytest

from busyboy import bar, cli

ENV = {"BUSYBOY_HOST": "10.0.4.20", "BUSYBOY_TOKEN": "testtoken"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


class Recorder:
    """An in-memory stand-in for the bar, capturing what the CLI sends."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: dict[str, str] = {"result": "ok"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def recorder(monkeypatch):
    """Route the CLI's client through a mock transport."""
    recorder = Recorder()
    real_open_client = bar.open_client

    def fake_open_client(config, *, transport=None):
        return real_open_client(config, transport=httpx.MockTransport(recorder.handle))

    monkeypatch.setattr(cli.bar, "open_client", fake_open_client)
    return recorder


def test_a_successful_draw_says_nothing(recorder):
    result = CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""


def test_text_sends_the_string_to_the_bar(recorder):
    CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "POST"
    body = json.loads(recorder.requests[0].content)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_options_reach_the_payload(recorder):
    CliRunner().invoke(
        cli.main,
        ["text", "hi", "--color", "red", "--timeout", "30", "--font", "small"],
        env=ENV,
    )

    element = json.loads(recorder.requests[0].content)["elements"][0]
    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30
    assert element["font"] == "small"


def test_clear_issues_a_delete(recorder):
    result = CliRunner().invoke(cli.main, ["clear"], env=ENV)

    assert result.exit_code == 0
    assert recorder.requests[0].method == "DELETE"


def test_host_flag_overrides_the_environment(recorder):
    CliRunner().invoke(cli.main, ["text", "hi", "--host", "192.168.1.5"], env=ENV)

    assert recorder.requests[0].url.host == "192.168.1.5"


def test_missing_configuration_exits_one_and_names_the_variable():
    result = CliRunner().invoke(cli.main, ["text", "hi"], env={})

    assert result.exit_code == 1
    assert "BUSYBOY_HOST" in result.stderr
    assert "BUSYBOY_TOKEN" in result.stderr


def test_a_rejected_request_exits_one(recorder):
    recorder.status = 401
    recorder.body = {"error": "unauthorized"}

    result = CliRunner().invoke(cli.main, ["text", "hi"], env=ENV)

    assert result.exit_code == 1
    assert "401" in result.stderr


def test_a_missing_argument_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text"], env=ENV)

    assert result.exit_code == 2


def test_an_unknown_font_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text", "hi", "--font", "cursive"], env=ENV)

    assert result.exit_code == 2


def test_an_invalid_colour_exits_one(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi", "--color", "chartreuseish"], env=ENV)

    assert result.exit_code == 1
    assert "color" in result.stderr


def test_help_lists_both_subcommands():
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0
    assert "text" in result.output
    assert "clear" in result.output


def test_verbose_on_a_successful_draw_still_exits_zero(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi", "--verbose"], env=ENV)

    assert result.exit_code == 0


def test_the_token_does_not_leak_under_verbose_when_config_is_missing():
    """
    Regression test for a token leak via chained tracebacks.

    ConfigError used to chain the underlying pydantic ValidationError, whose
    `missing` errors carry the whole pre-coercion input (including the raw
    token) as `input_value`. Under --verbose the CLI re-raises instead of
    swallowing the error, and Python prints the chained cause's traceback to
    stderr, so the token ended up on the console.
    """
    result = CliRunner().invoke(
        cli.main,
        ["text", "hi", "--verbose"],
        env={"BUSYBOY_TOKEN": "SUPERSECRETTOKEN123"},
    )

    assert "SUPERSECRETTOKEN123" not in result.output
    assert "SUPERSECRETTOKEN123" not in result.stderr
    assert result.exception is not None
    assert "SUPERSECRETTOKEN123" not in "".join(
        traceback.format_exception(type(result.exception), result.exception, result.exception.__traceback__)
    )
