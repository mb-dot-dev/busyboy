"""Inspection of the local git checkout: which branch, and which remote."""

import re
import subprocess

from busyboy import exceptions

# Matches the trailing owner/name of any remote URL form git accepts:
# git@host:owner/name.git, https://host/owner/name, ssh://git@host/owner/name.git
REMOTE_PATTERN = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/:]+?)(?:\.git)?/?$")

# Matches the userinfo component of a URL: everything between `//` and `@`, e.g. the
# `user:token` in `https://user:token@host/...`. Used only to redact before logging/raising.
USERINFO_PATTERN = re.compile(r"//[^/@]*@")

REDACTED_USERINFO = "//<redacted>@"

SUBPROCESS_TIMEOUT_SECONDS = 10


def _redact_userinfo(url: str) -> str:
    """Replace any credentials embedded between `//` and `@` with a fixed placeholder."""
    return USERINFO_PATTERN.sub(REDACTED_USERINFO, url)


def _run(*args: str) -> str:
    """Run a git command, returning its stripped stdout."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise exceptions.GitError("git is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise exceptions.GitError(f"git {' '.join(args)} timed out") from error
    if result.returncode != 0:
        raise exceptions.GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def parse_remote_url(url: str) -> tuple[str, str]:
    """Pull the owner and repository name out of any git remote URL form."""
    match = REMOTE_PATTERN.search(url)
    if match is None:
        raise exceptions.GitError(f"Cannot read an owner/name out of the remote URL: {_redact_userinfo(url)}")
    return match["owner"], match["name"]


def current_branch() -> str:
    """Return the branch the checkout is currently on."""
    branch = _run("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise exceptions.GitError("HEAD is detached; pass --branch explicitly")
    return branch


def origin_repo() -> tuple[str, str]:
    """Return origin's (owner, name)."""
    return parse_remote_url(_run("remote", "get-url", "origin"))
