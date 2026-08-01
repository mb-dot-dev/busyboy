"""GitHub REST queries: workflows, their latest runs, and open pull requests."""

import os
from pathlib import PurePosixPath
import subprocess
from typing import Any

from pydantic import BaseModel
import requests

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


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

# A 5s cap on establishing the connection and a 10s cap on each socket read,
# matching bar.py's budget.
REQUEST_TIMEOUT = (5, 10)

AUTH_STATUS_CODES = frozenset({401, 403})


class Repo(BaseModel, frozen=True):
    """A GitHub repository, identified the way the REST API identifies one."""

    owner: str
    name: str

    @property
    def slug(self) -> str:
        """The owner/name form used in API paths and on the display."""
        return f"{self.owner}/{self.name}"


class Workflow(BaseModel):
    """One Actions workflow defined in the repository."""

    id: int
    name: str
    path: str


class Run(BaseModel):
    """One run of a workflow. `conclusion` is null until the run completes."""

    id: int
    status: str
    conclusion: str | None = None


def _get(token: str, path: str, params: dict[str, str] | None = None) -> Any:
    """
    GET one GitHub API path, classifying failures by whether retrying could help.

    A rejected token and a missing repository will never fix themselves, so
    they raise GitHubError (fatal). Server errors and transport failures raise
    GitHubTransientError, which the watch loop swallows and retries.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    try:
        response = requests.get(f"{API_ROOT}{path}", headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as error:
        raise exceptions.GitHubTransientError(f"GET {path} failed: {error}") from error
    with response:
        if response.status_code in AUTH_STATUS_CODES:
            raise exceptions.GitHubAuthError(f"GitHub rejected the token (HTTP {response.status_code}) on GET {path}")
        if response.status_code >= 500:
            raise exceptions.GitHubTransientError(f"GitHub returned HTTP {response.status_code} on GET {path}")
        if response.status_code >= 400:
            raise exceptions.GitHubError(f"GitHub returned HTTP {response.status_code} on GET {path}")
        try:
            return response.json()
        except ValueError as error:
            raise exceptions.GitHubTransientError(f"GitHub returned a non-JSON body on GET {path}") from error


def resolve_workflow(token: str, repo: Repo, reference: str) -> Workflow:
    """
    Find a workflow by numeric id, filename, or display name.

    GitHub's own runs endpoint accepts only an id or a filename, so all three
    forms are resolved here against the full workflow list.
    """
    payload = _get(token, f"/repos/{repo.slug}/actions/workflows", {"per_page": "100"})
    workflows = [Workflow.model_validate(item) for item in payload.get("workflows", [])]
    for workflow in workflows:
        if reference == str(workflow.id) or reference == PurePosixPath(workflow.path).name:
            return workflow
    for workflow in workflows:
        if reference == workflow.name:
            return workflow
    available = ", ".join(sorted(workflow.name for workflow in workflows)) or "none"
    raise exceptions.GitHubError(f"No workflow matches {reference!r} in {repo.slug}. Available: {available}")


def latest_run(token: str, repo: Repo, workflow_id: int, branch: str) -> Run | None:
    """Return the most recent run of the workflow on the branch, or None if there is none."""
    payload = _get(
        token,
        f"/repos/{repo.slug}/actions/workflows/{workflow_id}/runs",
        {"branch": branch, "per_page": "1"},
    )
    runs = payload.get("workflow_runs", [])
    return Run.model_validate(runs[0]) if runs else None


def pull_request_number(token: str, repo: Repo, branch: str) -> int | None:
    """
    Return the number of the open pull request for the branch, or None.

    The head filter is scoped to `repo.owner`, so a pull request opened from a
    fork is not matched. Watching your own checkout's branch is the case this
    serves.
    """
    payload = _get(
        token,
        f"/repos/{repo.slug}/pulls",
        {"head": f"{repo.owner}:{branch}", "state": "open", "per_page": "1"},
    )
    return int(payload[0]["number"]) if payload else None
