"""Tests for Claude credential lookup and the usage query."""

from datetime import UTC, datetime
import json
import subprocess
from typing import Any

import pytest
import requests
import responses

from busyboy import claude, exceptions

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
FUTURE_MS = int(datetime(2026, 11, 12, 18, 0, tzinfo=UTC).timestamp() * 1000)
PAST_MS = int(datetime(2026, 11, 12, 6, 0, tzinfo=UTC).timestamp() * 1000)
SECRET = "sk-ant-oat01-secret-value"

PAYLOAD = {
    "five_hour": {
        "utilization": 3.0,
        "resets_at": "2026-11-12T20:29:59.606009+00:00",
        "limit_dollars": None,
        "locked_reason": None,
    },
    "seven_day": {"utilization": 26.0, "resets_at": "2026-11-15T01:59:59.606027+00:00"},
    "tangelo": None,
    "limits": [{"kind": "session", "percent": 3}],
}


def credentials(token=SECRET, expires_at=FUTURE_MS):
    return json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires_at}})


@pytest.fixture
def credentials_file(tmp_path, monkeypatch):
    path = tmp_path / ".credentials.json"
    monkeypatch.setattr(claude, "CREDENTIALS_FILE", path)
    return path


@pytest.fixture
def keychain(monkeypatch):
    """Pretend to be macOS with a scriptable `security` command."""
    monkeypatch.setattr(claude.sys, "platform", "darwin")
    state: dict[str, Any] = {"stdout": "", "returncode": 0, "error": None, "calls": []}

    def fake_run(command, **kwargs):
        state["calls"].append(command)
        if state["error"] is not None:
            raise state["error"]
        return subprocess.CompletedProcess(command, state["returncode"], stdout=state["stdout"], stderr="")

    monkeypatch.setattr(claude.subprocess, "run", fake_run)
    return state


@responses.activate
def test_fetch_usage_parses_the_real_payload_shape():
    responses.get(USAGE_URL, json=PAYLOAD)

    usage = claude.fetch_usage(SECRET)

    assert usage.session.utilization == 3.0
    assert usage.weekly.utilization == 26.0
    assert usage.session.resets_at == datetime(2026, 11, 12, 20, 29, 59, 606009, tzinfo=UTC)
    assert usage.weekly.resets_at is not None
    assert usage.weekly.resets_at.tzinfo is not None


@responses.activate
def test_fetch_usage_sends_the_required_headers():
    responses.get(USAGE_URL, json=PAYLOAD)

    claude.fetch_usage(SECRET)

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["User-Agent"].startswith("claude-code/")
    assert headers["Content-Type"] == "application/json"


@responses.activate
def test_null_resets_at_is_allowed():
    body = {"five_hour": {"utilization": 0.0, "resets_at": None}, "seven_day": {"utilization": 1.0}}
    responses.get(USAGE_URL, json=body)

    usage = claude.fetch_usage(SECRET)

    assert usage.session.resets_at is None
    assert usage.weekly.resets_at is None


@pytest.mark.parametrize("status", [401, 403])
@responses.activate
def test_rejected_token_is_an_auth_error(status):
    responses.get(USAGE_URL, status=status)

    with pytest.raises(exceptions.ClaudeAuthError):
        claude.fetch_usage(SECRET)


@pytest.mark.parametrize(
    ("status", "headers", "retry_after"),
    [(429, {}, None), (429, {"Retry-After": "30"}, 30.0), (500, {}, None), (503, {}, None)],
)
@responses.activate
def test_server_trouble_and_rate_limits_are_transient(status, headers, retry_after):
    responses.get(USAGE_URL, status=status, headers=headers)

    with pytest.raises(exceptions.ClaudeTransientError) as info:
        claude.fetch_usage(SECRET)

    assert info.value.retry_after == retry_after


@responses.activate
def test_other_client_errors_are_fatal_but_neither_subclass():
    responses.get(USAGE_URL, status=404)

    with pytest.raises(exceptions.ClaudeError) as info:
        claude.fetch_usage(SECRET)

    assert not isinstance(info.value, exceptions.ClaudeAuthError | exceptions.ClaudeTransientError)


@responses.activate
def test_connection_error_is_transient():
    responses.get(USAGE_URL, body=requests.ConnectionError("down"))

    with pytest.raises(exceptions.ClaudeTransientError):
        claude.fetch_usage(SECRET)


@responses.activate
def test_non_json_body_is_transient():
    responses.get(USAGE_URL, body="<html>oops</html>")

    with pytest.raises(exceptions.ClaudeTransientError):
        claude.fetch_usage(SECRET)


@pytest.mark.parametrize(
    "body",
    [
        {"seven_day": {"utilization": 1.0}},
        {"five_hour": {"utilization": "lots"}, "seven_day": {"utilization": 1.0}},
        {"five_hour": None, "seven_day": {"utilization": 1.0}},
        [],
    ],
)
@responses.activate
def test_malformed_shape_is_transient_not_a_key_error(body):
    responses.get(USAGE_URL, json=body)

    with pytest.raises(exceptions.ClaudeTransientError):
        claude.fetch_usage(SECRET)


@pytest.mark.parametrize("status", [401, 429, 500, 404])
@responses.activate
def test_errors_never_contain_the_token(status):
    responses.get(USAGE_URL, status=status, body=f"echo {SECRET}")

    with pytest.raises(exceptions.ClaudeError) as info:
        claude.fetch_usage(SECRET)

    assert SECRET not in str(info.value)


def test_keychain_token_is_used_on_macos(keychain, credentials_file):
    keychain["stdout"] = credentials(token="from-keychain")

    assert claude.resolve_token(now=NOW) == "from-keychain"
    assert keychain["calls"] == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]


def test_missing_keychain_entry_falls_back_to_the_credentials_file(keychain, credentials_file):
    keychain["returncode"] = 44
    credentials_file.write_text(credentials(token="from-file"))

    assert claude.resolve_token(now=NOW) == "from-file"


def test_a_failing_security_command_falls_back_to_the_credentials_file(keychain, credentials_file):
    keychain["error"] = FileNotFoundError("security")
    credentials_file.write_text(credentials(token="from-file"))

    assert claude.resolve_token(now=NOW) == "from-file"


def test_expired_keychain_token_does_not_fall_through(keychain, credentials_file):
    keychain["stdout"] = credentials(expires_at=PAST_MS)
    credentials_file.write_text(credentials(token="valid-but-ignored"))

    with pytest.raises(exceptions.ClaudeAuthError):
        claude.resolve_token(now=NOW)


def test_expired_file_token_raises(monkeypatch, credentials_file):
    monkeypatch.setattr(claude.sys, "platform", "linux")
    credentials_file.write_text(credentials(expires_at=PAST_MS))

    with pytest.raises(exceptions.ClaudeAuthError):
        claude.resolve_token(now=NOW)


def test_no_credentials_anywhere_raises_an_auth_error(keychain, credentials_file):
    keychain["returncode"] = 44

    with pytest.raises(exceptions.ClaudeAuthError, match="credentials"):
        claude.resolve_token(now=NOW)


def test_non_macos_skips_the_keychain(monkeypatch, credentials_file):
    monkeypatch.setattr(claude.sys, "platform", "linux")

    def fail(*_args, **_kwargs):
        raise AssertionError("security must not run off macOS")

    monkeypatch.setattr(claude.subprocess, "run", fail)
    credentials_file.write_text(credentials(token="from-file"))

    assert claude.resolve_token(now=NOW) == "from-file"


@pytest.mark.parametrize("raw", ["not json", "{}", json.dumps({"claudeAiOauth": {}}), json.dumps([1])])
def test_malformed_credentials_json_raises_an_auth_error(monkeypatch, credentials_file, raw):
    monkeypatch.setattr(claude.sys, "platform", "linux")
    credentials_file.write_text(raw)

    with pytest.raises(exceptions.ClaudeAuthError) as info:
        claude.resolve_token(now=NOW)

    assert SECRET not in str(info.value)


@responses.activate
def test_a_resets_at_without_an_offset_is_a_transient_error_not_a_type_error():
    body = {"five_hour": {"utilization": 1.0, "resets_at": "2026-11-12T20:29:59"}, "seven_day": {"utilization": 1.0}}
    responses.get(USAGE_URL, json=body)

    with pytest.raises(exceptions.ClaudeTransientError):
        claude.fetch_usage(SECRET)


def test_a_keychain_timeout_with_no_file_is_transient_not_a_login_prompt(keychain, credentials_file):
    keychain["error"] = subprocess.TimeoutExpired("security", 10)

    with pytest.raises(exceptions.ClaudeTransientError):
        claude.resolve_token(now=NOW)


def test_a_keychain_failure_other_than_not_found_with_no_file_is_transient(keychain, credentials_file):
    keychain["returncode"] = 1

    with pytest.raises(exceptions.ClaudeTransientError):
        claude.resolve_token(now=NOW)


def test_a_keychain_timeout_still_falls_back_to_a_credentials_file(keychain, credentials_file):
    keychain["error"] = subprocess.TimeoutExpired("security", 10)
    credentials_file.write_text(credentials(token="from-file"))

    assert claude.resolve_token(now=NOW) == "from-file"
