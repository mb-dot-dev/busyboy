"""Tests for exit codes and output of the busyboy command line."""

from importlib import metadata
import json

import pytest
import responses
from typer.testing import CliRunner

from busyboy import cli, exceptions, github

ENV = {"BUSYBOY_HOST": "10.0.4.20", "BUSYBOY_TOKEN": "testtoken"}

DRAW_PATH = "/api/display/draw"
UPLOAD_PATH = "/api/assets/upload"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """
    Keep the developer's own environment out of these tests.

    GITHUB_TOKEN matters as much as the BUSYBOY_* pair: with it set, a test
    that reaches the real `github.resolve_token` passes on a developer's
    machine and fails on a CI runner that has neither it nor a gh login.
    """
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_a_successful_draw_says_nothing(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    result = CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""


def test_text_sends_the_string_to_the_bar(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert len(bar_transport.calls) == 1
    assert bar_transport.calls[0].method == "POST"
    body = json.loads(bar_transport.calls[0].content)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_options_reach_the_payload(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    CliRunner().invoke(
        cli.main,
        ["text", "hi", "--color", "red", "--timeout", "30", "--font", "small", "--scroll-rate", "600"],
        env=ENV,
    )

    element = json.loads(bar_transport.calls[0].content)["elements"][0]
    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30
    assert element["font"] == "small"
    assert element["scroll_rate"] == 600


def test_token_flag_overrides_the_environment(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    CliRunner().invoke(cli.main, ["text", "hi", "--token", "flagtoken"], env=ENV)

    assert bar_transport.calls[0].headers["X-API-Token"] == "flagtoken"


def test_clear_issues_a_delete(bar_transport):
    bar_transport.add("DELETE", DRAW_PATH)

    result = CliRunner().invoke(cli.main, ["clear"], env=ENV)

    assert result.exit_code == 0
    assert bar_transport.calls[0].method == "DELETE"


def test_host_flag_overrides_the_environment(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    CliRunner().invoke(cli.main, ["text", "hi", "--host", "192.168.1.5"], env=ENV)

    assert str(bar_transport.calls[0].url).startswith("http://192.168.1.5")


def test_no_configuration_uses_the_usb_default_and_sends_no_token(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    result = CliRunner().invoke(cli.main, ["text", "hi"], env={})

    assert result.exit_code == 0
    assert str(bar_transport.calls[0].url).startswith("http://10.0.4.20")
    assert "X-API-Token" not in bar_transport.calls[0].headers


def test_a_rejected_request_exits_one(bar_transport):
    bar_transport.add("POST", DRAW_PATH, status=401, json={"error": "unauthorized"})

    result = CliRunner().invoke(cli.main, ["text", "hi"], env=ENV)

    assert result.exit_code == 1
    assert "401" in result.stderr


def test_a_missing_argument_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text"], env=ENV)

    assert result.exit_code == 2


def test_an_unknown_font_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text", "hi", "--font", "cursive"], env=ENV)

    assert result.exit_code == 2


def test_an_invalid_colour_exits_one():
    result = CliRunner().invoke(cli.main, ["text", "hi", "--color", "chartreuseish"], env=ENV)

    assert result.exit_code == 1
    assert "color" in result.stderr


def test_help_lists_all_subcommands():
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0
    assert "text" in result.output
    assert "clear" in result.output
    assert "gh" in result.output


def test_version_prints_the_installed_version_and_exits_zero():
    """
    Typer has no version_option, so --version is busyboy's own eager callback.

    It has to work without a subcommand, which is what is_eager guards: without
    it the group would demand a COMMAND before the callback ever ran.
    """
    result = CliRunner().invoke(cli.main, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"busyboy, version {metadata.version('busyboy')}"


def test_verbose_on_a_successful_draw_still_exits_zero(bar_transport):
    bar_transport.add("POST", DRAW_PATH)

    result = CliRunner().invoke(cli.main, ["text", "hi", "--verbose"], env=ENV)

    assert result.exit_code == 0


GITHUB_RUNS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows/42/runs"
GITHUB_PULLS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/pulls"
GITHUB_WORKFLOWS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows"


@pytest.fixture
def github_bar(bar_transport, monkeypatch):
    """Register a whole happy-path watch: token, git, GitHub, and the bar."""
    monkeypatch.setattr(cli.github, "resolve_token", lambda: "gho_test")
    monkeypatch.setattr(cli.git, "current_branch", lambda: "feature/x")
    monkeypatch.setattr(cli.git, "origin_repo", lambda: ("mb-dot-dev", "busyboy"))
    responses.add(
        responses.GET,
        GITHUB_WORKFLOWS_URL,
        json={"workflows": [{"id": 42, "name": "CI", "path": ".github/workflows/main.yaml"}]},
    )
    responses.add(
        responses.GET,
        GITHUB_RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, GITHUB_PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", UPLOAD_PATH)
    bar_transport.add("POST", DRAW_PATH)
    bar_transport.add("DELETE", DRAW_PATH)
    return bar_transport


def stop_after_one_tick(monkeypatch):
    """Make the watch loop's first sleep behave like Ctrl+C."""

    def sleep(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.watch.time, "sleep", sleep)


@responses.activate
def test_watching_a_workflow_draws_and_exits_cleanly(github_bar, monkeypatch):
    stop_after_one_tick(monkeypatch)

    result = CliRunner().invoke(cli.main, ["gh", "workflow", "CI"], env=ENV)

    assert result.exit_code == 0
    assert result.output == ""
    draws = [call for call in github_bar.calls if call.method == "POST" and call.url.path == DRAW_PATH]
    assert len(draws) == 1
    body = json.loads(draws[0].content)
    elements = {element["id"]: element for element in body["elements"]}
    assert elements["repo"]["text"] == "mb-dot-dev/busyboy"
    assert elements["ref"]["text"] == "#12 CI"
    assert elements["icon"]["path"] == "success.png"


@responses.activate
def test_an_explicit_repo_and_branch_override_detection(github_bar, monkeypatch):
    stop_after_one_tick(monkeypatch)

    def fail():
        raise AssertionError("git must not be consulted when both are given")

    monkeypatch.setattr(cli.git, "current_branch", fail)
    monkeypatch.setattr(cli.git, "origin_repo", fail)

    result = CliRunner().invoke(
        cli.main,
        ["gh", "workflow", "CI", "--repo", "mb-dot-dev/busyboy", "--branch", "feature/x"],
        env=ENV,
    )

    assert result.exit_code == 0


def test_a_malformed_repo_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(cli.github, "resolve_token", lambda: "gho_test")

    result = CliRunner().invoke(cli.main, ["gh", "workflow", "CI", "--repo", "busyboy"], env=ENV)

    assert result.exit_code == 2
    assert "owner/name" in result.stderr


def test_a_malformed_repo_is_a_usage_error_even_without_a_github_token(monkeypatch):
    """
    A bad option is a usage error regardless of the environment.

    --repo is validated while the command line is parsed, before the command
    body resolves a token, so a developer with no gh login still gets exit 2
    and a message about the option they got wrong -- not exit 1 about a missing
    token. Without that ordering this test fails in CI and passes locally,
    purely on whether `gh auth token` happens to work.
    """

    def no_token():
        raise exceptions.GitHubError(github.NO_TOKEN_MESSAGE)

    monkeypatch.setattr(cli.github, "resolve_token", no_token)

    result = CliRunner().invoke(cli.main, ["gh", "workflow", "CI", "--repo", "busyboy"], env=ENV)

    assert result.exit_code == 2
    assert "owner/name" in result.stderr


def test_a_missing_token_exits_one_with_one_line(monkeypatch):
    def no_token():
        raise exceptions.GitHubError(github.NO_TOKEN_MESSAGE)

    monkeypatch.setattr(cli.github, "resolve_token", no_token)

    result = CliRunner().invoke(cli.main, ["gh", "workflow", "CI"], env=ENV)

    assert result.exit_code == 1
    assert "GITHUB_TOKEN" in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


@responses.activate
def test_an_unknown_workflow_exits_one(monkeypatch):
    monkeypatch.setattr(cli.github, "resolve_token", lambda: "gho_test")
    monkeypatch.setattr(cli.git, "current_branch", lambda: "feature/x")
    monkeypatch.setattr(cli.git, "origin_repo", lambda: ("mb-dot-dev", "busyboy"))
    responses.add(
        responses.GET,
        GITHUB_WORKFLOWS_URL,
        json={"workflows": [{"id": 42, "name": "CI", "path": ".github/workflows/main.yaml"}]},
    )

    result = CliRunner().invoke(cli.main, ["gh", "workflow", "Nope"], env=ENV)

    assert result.exit_code == 1
    assert "CI" in result.stderr


def test_a_git_failure_exits_one(monkeypatch):
    monkeypatch.setattr(cli.github, "resolve_token", lambda: "gho_test")

    def not_a_repo():
        raise exceptions.GitError("fatal: not a git repository")

    monkeypatch.setattr(cli.git, "origin_repo", not_a_repo)

    result = CliRunner().invoke(cli.main, ["gh", "workflow", "CI"], env=ENV)

    assert result.exit_code == 1
    assert "not a git repository" in result.stderr
