"""Claude Code credentials and the usage-limits query."""

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field, ValidationError
import requests

from busyboy import exceptions

SUBPROCESS_TIMEOUT_SECONDS = 10
KEYCHAIN_SERVICE = "Claude Code-credentials"
# `security` exits 44 when the item does not exist; any other failure (timeout, locked keychain) says nothing
# about whether the user is logged in.
KEYCHAIN_NOT_FOUND_EXIT_CODE = 44
KEYCHAIN_FAILED_MESSAGE = "Could not read the Claude Code credentials from the keychain."
CREDENTIALS_FILE = Path("~/.claude/.credentials.json")

NO_CREDENTIALS_MESSAGE = "No Claude Code credentials found. Run `claude` to log in."
UNREADABLE_CREDENTIALS_MESSAGE = "Claude Code credentials are unreadable. Run `claude` to log in."
EXPIRED_MESSAGE = "The Claude Code token has expired. Run `claude` to refresh it."
REJECTED_MESSAGE = "Anthropic rejected the Claude Code token. Run `claude` to log in again."

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The usage endpoint is undocumented: it is what Claude Code itself calls, so mirror its identity.
USER_AGENT = "claude-code/2.1.220"
BETA_HEADER = "oauth-2025-04-20"
REQUEST_TIMEOUT_SECONDS = 15
# A server-sent Retry-After is untrusted: cap it so one absurd value cannot park the watch for days.
MAX_RETRY_AFTER_SECONDS = 3600.0


class Window(BaseModel, frozen=True):
    """One usage limit: a 0-100 utilization and when the window resets (None when the API gives no reset)."""

    utilization: float = Field(allow_inf_nan=False)
    resets_at: AwareDatetime | None = None


class Usage(BaseModel, frozen=True):
    """The two limits busyboy displays. Every other field the endpoint returns is ignored."""

    session: Window
    weekly: Window


def _token_from_keychain() -> tuple[str | None, bool]:
    """
    Read Claude Code's credentials blob from the macOS keychain.

    Returns the blob (or None) and whether the lookup *failed* rather than simply finding nothing: a
    timeout, a missing `security` binary, or any exit other than "not found" is a failure.
    """
    if sys.platform != "darwin":
        return None, False
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None, True
    if result.returncode != 0:
        # stderr is captured and dropped: it is a diagnostic about the keychain, and swallowing it
        # removes any chance of a credential reaching busyboy's output.
        return None, result.returncode != KEYCHAIN_NOT_FOUND_EXIT_CODE
    return result.stdout.strip() or None, False


def _token_from_file() -> str | None:
    """Read Claude Code's credentials blob from its credentials file, or None when absent."""
    try:
        return CREDENTIALS_FILE.expanduser().read_text()
    except OSError:
        return None
    except UnicodeDecodeError:
        raise exceptions.ClaudeAuthError(UNREADABLE_CREDENTIALS_MESSAGE) from None


def _access_token(blob: str, now: datetime) -> str:
    """Extract an unexpired access token from a credentials blob, never echoing the blob."""
    try:
        data = json.loads(blob)
    except ValueError:
        raise exceptions.ClaudeAuthError(UNREADABLE_CREDENTIALS_MESSAGE) from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        raise exceptions.ClaudeAuthError(UNREADABLE_CREDENTIALS_MESSAGE)
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise exceptions.ClaudeAuthError(UNREADABLE_CREDENTIALS_MESSAGE)
    expires_at = oauth.get("expiresAt")
    if expires_at is not None:
        # A present-but-wrong-typed expiry says nothing about validity, so it is unreadable, not "unexpired".
        # bool is an int subclass, so it is excluded explicitly.
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
            raise exceptions.ClaudeAuthError(UNREADABLE_CREDENTIALS_MESSAGE)
        if expires_at / 1000 <= now.timestamp():
            raise exceptions.ClaudeAuthError(EXPIRED_MESSAGE)
    return token


def resolve_token(*, now: datetime | None = None) -> str:
    """
    Find Claude Code's OAuth access token: the macOS keychain first, then ~/.claude/.credentials.json.

    Read-only: the token is never refreshed or written back. The file is consulted
    only when the keychain entry is missing or its command fails (a failure with no file is transient) — an entry that
    exists but is expired or malformed is fatal and does not fall through.
    """
    blob, keychain_failed = _token_from_keychain()
    blob = blob or _token_from_file()
    if blob is None:
        if keychain_failed:
            # Mid-watch, a keychain that is briefly unreadable (right after wake, locked) must not
            # be reported as "log in": it is retryable.
            raise exceptions.ClaudeTransientError(KEYCHAIN_FAILED_MESSAGE)
        raise exceptions.ClaudeAuthError(NO_CREDENTIALS_MESSAGE)
    return _access_token(blob, now or datetime.now(UTC))


def _parse_retry_after(value: str | None) -> float | None:
    """Parse the delta-seconds form of Retry-After; anything else is None."""
    if value is None:
        return None
    try:
        seconds = float(int(value))
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def fetch_usage(token: str) -> Usage:
    """
    GET the usage limits, classifying failures by whether retrying could help.

    A rejected token never self-heals (ClaudeAuthError). Rate limiting, server errors,
    transport failures, and bodies that are not the expected shape are transient.
    No message ever includes the token or the response body.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    try:
        response = requests.get(USAGE_URL, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        raise exceptions.ClaudeTransientError(f"Claude usage request failed: {type(error).__name__}") from None

    status = response.status_code
    if status in (401, 403):
        raise exceptions.ClaudeAuthError(REJECTED_MESSAGE)
    if status == 429:
        raise exceptions.ClaudeTransientError(
            "Claude usage API rate limited the request",
            retry_after=_parse_retry_after(response.headers.get("Retry-After")),
        )
    if status >= 500:
        raise exceptions.ClaudeTransientError(f"Claude usage API returned HTTP {status}")
    if status >= 400:
        raise exceptions.ClaudeError(f"Claude usage API returned HTTP {status}")

    return _parse_usage(response)


def _parse_usage(response: requests.Response) -> Usage:
    """Turn a successful response into a Usage, treating any surprise in its shape as transient."""
    try:
        body: Any = response.json()
        return Usage(
            session=Window.model_validate(body["five_hour"]),
            weekly=Window.model_validate(body["seven_day"]),
        )
    except ValueError, TypeError, KeyError, ValidationError:
        # ValidationError subclasses ValueError; it is listed for clarity. Its message is deliberately
        # not forwarded: it quotes the offending input.
        raise exceptions.ClaudeTransientError("Claude usage API returned an unexpected response") from None
