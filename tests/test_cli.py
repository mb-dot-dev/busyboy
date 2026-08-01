"""Tests for exit codes and output of the busyboy command line."""

import json
import re

from click.testing import CliRunner
import pytest
import responses

from busyboy import cli

ENV = {"BUSYBOY_HOST": "10.0.4.20", "BUSYBOY_TOKEN": "testtoken"}

DRAW_URL_PATTERN = re.compile(r"^http://[^/]+/api/display/draw")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


class Recorder:
    """An in-memory stand-in for the bar, capturing what the CLI sends."""

    def __init__(self) -> None:
        self.requests: list = []
        self.status = 200
        self.body: dict[str, str] = {"result": "ok"}

    def callback(self, request):
        self.requests.append(request)
        return (self.status, {}, json.dumps(self.body))


@pytest.fixture
def recorder():
    """Route the CLI's requests through a responses-registered callback."""
    recorder = Recorder()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add_callback(responses.POST, DRAW_URL_PATTERN, callback=recorder.callback, content_type="application/json")
        mock.add_callback(
            responses.DELETE, DRAW_URL_PATTERN, callback=recorder.callback, content_type="application/json"
        )
        yield recorder


def test_a_successful_draw_says_nothing(recorder):
    result = CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""


def test_text_sends_the_string_to_the_bar(recorder):
    CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "POST"
    body = json.loads(recorder.requests[0].body)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_options_reach_the_payload(recorder):
    CliRunner().invoke(
        cli.main,
        ["text", "hi", "--color", "red", "--timeout", "30", "--font", "small", "--scroll-rate", "600"],
        env=ENV,
    )

    element = json.loads(recorder.requests[0].body)["elements"][0]
    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30
    assert element["font"] == "small"
    assert element["scroll_rate"] == 600


def test_token_flag_overrides_the_environment(recorder):
    CliRunner().invoke(cli.main, ["text", "hi", "--token", "flagtoken"], env=ENV)

    assert recorder.requests[0].headers["X-API-Token"] == "flagtoken"


def test_clear_issues_a_delete(recorder):
    result = CliRunner().invoke(cli.main, ["clear"], env=ENV)

    assert result.exit_code == 0
    assert recorder.requests[0].method == "DELETE"


def test_host_flag_overrides_the_environment(recorder):
    CliRunner().invoke(cli.main, ["text", "hi", "--host", "192.168.1.5"], env=ENV)

    assert recorder.requests[0].url.startswith("http://192.168.1.5")


def test_no_configuration_uses_the_usb_default_and_sends_no_token(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi"], env={})

    assert result.exit_code == 0
    assert recorder.requests[0].url.startswith("http://10.0.4.20")
    assert "X-API-Token" not in recorder.requests[0].headers


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
