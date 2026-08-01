"""GitHub REST queries: workflows, their latest runs, and open pull requests."""

import os
import subprocess

from busyboy import exceptions

SUBPROCESS_TIMEOUT_SECONDS = 10

NO_TOKEN_MESSAGE = "No GitHub token available. Install the gh CLI and run `gh auth login`, or set GITHUB_TOKEN."


def _token_from_gh() -> str | None:
    """Ask the gh CLI for its token, or return None when it cannot supply one."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        # gh's stderr is deliberately discarded: it is a diagnostic about the
        # user's login state, and swallowing it removes any chance of a token
        # reaching a traceback.
        return None
    return result.stdout.strip() or None


def resolve_token() -> str:
    """Find a GitHub token: the gh CLI first, then GITHUB_TOKEN."""
    token = _token_from_gh() or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise exceptions.GitHubError(NO_TOKEN_MESSAGE)
    return token
