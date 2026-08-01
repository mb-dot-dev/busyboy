# `busyboy gh workflow` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `busyboy gh workflow <workflow>`, which polls a GitHub Actions workflow's latest run every 10 seconds and keeps the repository, pull request (or branch), and a GitHub-style status icon on the BUSY Bar's front display until Ctrl+C.

**Architecture:** Three new modules with hard boundaries — `git.py` (local repository inspection via subprocess), `github.py` (GitHub REST queries and token resolution), and `watch.py` (the poll loop, and the only module that knows about both GitHub and the bar). `bar.py` gains image elements, a two-row payload builder, and asset upload; `cli.py` gains a `gh` group. Status icons ship as generated 12x12 PNGs inside the package.

**Tech Stack:** Python 3.14, Click, pydantic + pydantic-settings, `requests`, `pytest` + `responses`, `ruff`, `ty`, `uv`.

**Spec:** `docs/superpowers/specs/2026-08-01-gh-workflow-watch-design.md`

## Global Constraints

- Python `>=3.14`. All commands run through `make`, which wraps `uv run --frozen`.
- **No new runtime dependencies.** Everything here uses `requests`, pydantic, Click, and the standard library. Do not add Pillow — the icon PNGs are generated with `zlib` + `struct`.
- Module boundaries are enforced by review: `config.py` imports neither Click nor `bar.py`; `bar.py` knows nothing about argv, the environment, or GitHub; `cli.py` holds no BUSY Bar payload knowledge; `git.py` and `github.py` do not import each other.
- Ruff: 120-char lines, double quotes, PEP 257 docstrings, `force-sort-within-sections = true` in isort. That last one sorts `import x` and `from x import y` **together by module name** — write imports that way or `make format` will rewrite them.
- `ty` type checking must pass. Unpacking into a `BaseSettings` subclass needs `dict[str, Any]`, never `dict[str, str]`.
- Coverage floor is 75% (`fail_under` in `pyproject.toml`); the project currently sits near 92%. Do not let it drop below the floor.
- Never log, print, or include an API token in an exception message or traceback. This applies to the new `GITHUB_TOKEN` / `gh auth token` value exactly as it already applies to `BUSYBOY_TOKEN`.
- Run `make test` (lint + unit) before every commit.

## Deviation from the spec

The spec writes `git.origin_repo() -> Repo`. This plan returns `tuple[str, str]` instead, and defines `Repo` in `github.py`. Reason: `owner/name` is a GitHub concept, and having `git.py` return a GitHub type would put an import edge between two modules the spec requires to be independent. `watch.py` composes the two, which is its job.

## File Structure

| File | Responsibility |
|---|---|
| `src/busyboy/exceptions.py` *(modify)* | Gains `BusyboyError` base above `BarError`; adds `GitError`, `GitHubError`, `GitHubAuthError`, `GitHubTransientError` |
| `src/busyboy/bar.py` *(modify)* | `_request` takes a `path`; adds `ImageElement`, `IconName`, `build_workflow_payload`, `upload_icons` |
| `src/busyboy/git.py` *(new)* | `current_branch()`, `origin_repo()`, `parse_remote_url()` |
| `src/busyboy/github.py` *(new)* | `resolve_token()`, `Repo`, `Workflow`, `Run`, `resolve_workflow()`, `latest_run()`, `pull_request_number()` |
| `src/busyboy/watch.py` *(new)* | `Target`, `Screen`, `icon_for()`, `render()`, `tick()`, `watch()` |
| `src/busyboy/cli.py` *(modify)* | The `gh` group and its `workflow` subcommand |
| `src/busyboy/assets/*.png` *(new)* | Six generated 12x12 RGBA status icons |
| `tools/generate_icons.py` *(new)* | Regenerates the icon PNGs from ASCII art, stdlib only |
| `tests/test_git.py`, `tests/test_github.py`, `tests/test_watch.py` *(new)* | One test module per source module, as the project already does |

---

### Task 1: Widen the exception hierarchy and make `bar._request` path-agnostic

`BarError` is currently the root, but git and GitHub failures are not bar failures. And `_request` hardcodes `DISPLAY_DRAW_PATH` in both the URL it builds and the errors it raises, so it cannot reach the asset-upload endpoint. Both are prerequisites for everything that follows.

**Files:**
- Modify: `src/busyboy/exceptions.py`
- Modify: `src/busyboy/bar.py:139-198` (`_request`, `draw_text`, `clear`)
- Modify: `src/busyboy/cli.py:53-57` (`_handle_errors`)
- Test: `tests/test_exceptions.py`, `tests/test_bar.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `exceptions.BusyboyError(Exception)` — base for every expected busyboy failure
  - `exceptions.BarError(BusyboyError)`, `exceptions.GitError(BusyboyError)`
  - `exceptions.GitHubError(BusyboyError)`, `exceptions.GitHubAuthError(GitHubError)`, `exceptions.GitHubTransientError(GitHubError)`
  - `exceptions.format_delivery_error(error: BusyboyError) -> str`
  - `bar._request(config, method, path, *, params=None, json_body=None, data=None, content_type=None) -> None`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_exceptions.py`:

```python
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
```

Add to `tests/test_bar.py`:

```python
@responses.activate
def test_a_request_reports_the_path_it_actually_used(config):
    responses.add(responses.POST, "http://10.0.4.20/api/assets/upload", json={"error": "nope"}, status=401)

    with pytest.raises(exceptions.BarAPIError) as caught:
        bar._request(config, "POST", "/api/assets/upload", data=b"x", content_type="application/octet-stream")

    assert caught.value.path == "/api/assets/upload"


@responses.activate
def test_a_request_sends_a_raw_body_with_its_content_type(config):
    responses.add(responses.POST, "http://10.0.4.20/api/assets/upload", json={"result": "ok"}, status=200)

    bar._request(config, "POST", "/api/assets/upload", data=b"\x89PNG", content_type="application/octet-stream")

    request = responses.calls[0].request
    assert request.body == b"\x89PNG"
    assert request.headers["Content-Type"] == "application/octet-stream"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_exceptions.py tests/test_bar.py -v`
Expected: FAIL — `AttributeError: module 'busyboy.exceptions' has no attribute 'BusyboyError'`, and `TypeError` on `_request` for the positional `path`.

- [ ] **Step 3: Rewrite the exception hierarchy**

Replace the top of `src/busyboy/exceptions.py`:

```python
"""Exception hierarchy for expected busyboy failures."""


class BusyboyError(Exception):
    """Base class for every failure busyboy expects and reports as one line."""


class BarError(BusyboyError):
    """Base class for all busyboy BUSY Bar delivery exceptions."""


class GitError(BusyboyError):
    """Raised when inspecting the local git repository fails."""


class GitHubError(BusyboyError):
    """Raised when a GitHub API request fails in a way that will not self-heal."""


class GitHubAuthError(GitHubError):
    """Raised when GitHub rejects the token (401 or 403)."""


class GitHubTransientError(GitHubError):
    """Raised for failures worth retrying: 5xx responses, timeouts, dropped connections."""
```

`BarAPIError`, `BarRequestError`, and `format_delivery_error` stay exactly as they are, except widen the signature:

```python
def format_delivery_error(error: BusyboyError) -> str:
    """Render an expected failure as a compact one-line diagnostic for stderr."""
```

Its existing `return str(error)` fallback already handles the non-bar cases correctly.

- [ ] **Step 4: Make `_request` take a path**

In `src/busyboy/bar.py`, add the new endpoint constant next to `DISPLAY_DRAW_PATH`:

```python
DISPLAY_DRAW_PATH = "/api/display/draw"
ASSET_UPLOAD_PATH = "/api/assets/upload"
```

Change `_raise_for_error_response` to take the path, and `_request` to take it positionally:

```python
def _raise_for_error_response(response: requests.Response, *, method: str, path: str) -> None:
    """Convert an HTTP error response into a BarAPIError."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    raise exceptions.BarAPIError(
        error or response.text or f"HTTP {response.status_code}",
        code=code if isinstance(code, int) else None,
        status_code=response.status_code,
        method=method,
        path=path,
    )


def _request(
    config: BusyboyConfig,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> None:
    """
    Send one request to the bar, retrying transport-level failures.

    Only connection errors and timeouts are retried, up to MAX_RETRIES extra
    attempts with growing backoff. An HTTP error response raises immediately,
    and so does any other request failure (e.g. a malformed URL) — those are
    deterministic, so retrying them would just add pointless backoff delay.
    """
    url = f"{_base_url(config.host)}{path}"
    headers = _auth_headers(config)
    if content_type is not None:
        headers["Content-Type"] = content_type
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if attempt >= MAX_RETRIES:
                raise exceptions.BarRequestError(
                    str(error),
                    method=method,
                    path=path,
                    attempts=attempt + 1,
                ) from error
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        except requests.exceptions.RequestException as error:
            raise exceptions.BarRequestError(
                str(error),
                method=method,
                path=path,
                attempts=attempt + 1,
            ) from error
        try:
            if response.status_code >= 400:
                _raise_for_error_response(response, method=method, path=path)
        finally:
            response.close()
        return
```

Update the two existing callers:

```python
def draw_text(config: BusyboyConfig, payload: DisplayElements) -> None:
    """Send a draw payload to the bar."""
    _request(config, "POST", DISPLAY_DRAW_PATH, json_body=payload.model_dump(exclude_none=True))


def clear(config: BusyboyConfig) -> None:
    """Remove what busyboy drew, without touching other applications' elements."""
    _request(config, "DELETE", DISPLAY_DRAW_PATH, params={"application_name": APPLICATION_NAME})
```

- [ ] **Step 5: Widen what the CLI catches**

In `src/busyboy/cli.py`, change the last `except` clause of `_handle_errors`:

```python
        except exceptions.BusyboyError as error:
            if verbose:
                raise
            message = exceptions.format_delivery_error(error)
            raise click.ClickException(message) from error
```

- [ ] **Step 6: Run the full suite**

Run: `make test`
Expected: PASS — all pre-existing tests still pass (they exercise `draw_text` and `clear`, whose behavior is unchanged), plus the five new ones.

- [ ] **Step 7: Commit**

```bash
git add src/busyboy/exceptions.py src/busyboy/bar.py src/busyboy/cli.py tests/test_exceptions.py tests/test_bar.py
git commit -m "refactor: add a BusyboyError base and make bar._request path-agnostic"
```

---

### Task 2: `git.py` — local repository inspection

**Files:**
- Create: `src/busyboy/git.py`
- Test: `tests/test_git.py`

**Interfaces:**
- Consumes: `exceptions.GitError` (Task 1).
- Produces:
  - `git.current_branch() -> str`
  - `git.origin_repo() -> tuple[str, str]` — `(owner, name)`
  - `git.parse_remote_url(url: str) -> tuple[str, str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_git.py`:

```python
"""Tests for local git repository inspection."""

import subprocess

import pytest

from busyboy import exceptions, git


class FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_git(monkeypatch):
    """Replace the git subprocess with a scripted result, recording the argv."""
    calls: list[list[str]] = []
    result = FakeCompleted()

    def run(argv, **kwargs):
        calls.append(argv)
        return result

    monkeypatch.setattr(git.subprocess, "run", run)
    return calls, result


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:mb-dot-dev/busyboy.git", ("mb-dot-dev", "busyboy")),
        ("https://github.com/mb-dot-dev/busyboy.git", ("mb-dot-dev", "busyboy")),
        ("https://github.com/mb-dot-dev/busyboy", ("mb-dot-dev", "busyboy")),
        ("ssh://git@github.com/mb-dot-dev/busyboy.git", ("mb-dot-dev", "busyboy")),
        ("https://github.com/mb-dot-dev/busyboy/", ("mb-dot-dev", "busyboy")),
    ],
)
def test_remote_urls_parse_into_owner_and_name(url, expected):
    assert git.parse_remote_url(url) == expected


def test_an_unparseable_remote_url_is_rejected():
    with pytest.raises(exceptions.GitError):
        git.parse_remote_url("not-a-remote")


def test_the_current_branch_comes_from_rev_parse(fake_git):
    calls, result = fake_git
    result.stdout = "feature/gh-workflow-watch\n"

    assert git.current_branch() == "feature/gh-workflow-watch"
    assert calls[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]


def test_a_detached_head_is_rejected(fake_git):
    _, result = fake_git
    result.stdout = "HEAD\n"

    with pytest.raises(exceptions.GitError, match="detached"):
        git.current_branch()


def test_a_failing_git_command_raises(fake_git):
    _, result = fake_git
    result.returncode = 128
    result.stderr = "fatal: not a git repository"

    with pytest.raises(exceptions.GitError, match="not a git repository"):
        git.current_branch()


def test_a_missing_git_binary_raises(monkeypatch):
    def run(argv, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git.subprocess, "run", run)

    with pytest.raises(exceptions.GitError, match="not installed"):
        git.current_branch()


def test_the_origin_remote_becomes_owner_and_name(fake_git):
    calls, result = fake_git
    result.stdout = "git@github.com:mb-dot-dev/busyboy.git\n"

    assert git.origin_repo() == ("mb-dot-dev", "busyboy")
    assert calls[0] == ["git", "remote", "get-url", "origin"]


def test_git_is_never_run_through_a_shell(fake_git):
    calls, result = fake_git
    result.stdout = "main\n"

    git.current_branch()

    assert isinstance(calls[0], list)
    assert calls[0][0] == "git"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_git.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'busyboy.git'`.

- [ ] **Step 3: Write `git.py`**

Create `src/busyboy/git.py`:

```python
"""Inspection of the local git checkout: which branch, and which remote."""

import re
import subprocess

from busyboy import exceptions

# Matches the trailing owner/name of any remote URL form git accepts:
# git@host:owner/name.git, https://host/owner/name, ssh://git@host/owner/name.git
REMOTE_PATTERN = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/:]+?)(?:\.git)?/?$")

SUBPROCESS_TIMEOUT_SECONDS = 10


def _run(*args: str) -> str:
    """Run a git command, returning its stripped stdout."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        raise exceptions.GitError(f"Cannot read an owner/name out of the remote URL: {url}")
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen pytest tests/test_git.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Lint and commit**

```bash
make test
git add src/busyboy/git.py tests/test_git.py
git commit -m "feat: read the current branch and origin repo from git"
```

---

### Task 3: `github.py` — token resolution

Split from the rest of `github.py` because it is the one piece with a security requirement attached, and because a reviewer could reasonably accept the query layer while rejecting the token fallback order.

**Files:**
- Create: `src/busyboy/github.py`
- Test: `tests/test_github.py`

**Interfaces:**
- Consumes: `exceptions.GitHubError` (Task 1).
- Produces: `github.resolve_token() -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_github.py`:

```python
"""Tests for GitHub token resolution and REST queries."""

import subprocess

import pytest

from busyboy import exceptions, github


class FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own GITHUB_TOKEN out of these tests."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def set_gh(monkeypatch, result):
    """Point the gh subprocess at a scripted result, or an exception to raise."""

    def run(argv, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(github.subprocess, "run", run)


def test_the_gh_cli_token_wins(monkeypatch):
    set_gh(monkeypatch, FakeCompleted(stdout="gho_fromcli\n"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromenv")

    assert github.resolve_token() == "gho_fromcli"


def test_the_environment_is_the_fallback_when_gh_is_absent(monkeypatch):
    set_gh(monkeypatch, FileNotFoundError("gh"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromenv")

    assert github.resolve_token() == "ghp_fromenv"


def test_the_environment_is_the_fallback_when_gh_is_logged_out(monkeypatch):
    set_gh(monkeypatch, FakeCompleted(stderr="not logged in", returncode=1))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromenv")

    assert github.resolve_token() == "ghp_fromenv"


def test_no_token_anywhere_raises_naming_both_options(monkeypatch):
    set_gh(monkeypatch, FileNotFoundError("gh"))

    with pytest.raises(exceptions.GitHubError) as caught:
        github.resolve_token()

    assert "gh auth login" in str(caught.value)
    assert "GITHUB_TOKEN" in str(caught.value)


def test_the_failure_message_never_contains_a_token(monkeypatch):
    set_gh(monkeypatch, FakeCompleted(stdout="", stderr="gho_leaked_secret", returncode=1))

    with pytest.raises(exceptions.GitHubError) as caught:
        github.resolve_token()

    assert "gho_leaked_secret" not in str(caught.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_github.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'busyboy.github'`.

- [ ] **Step 3: Write the token half of `github.py`**

Create `src/busyboy/github.py`:

```python
"""GitHub REST queries: workflows, their latest runs, and open pull requests."""

import os
import subprocess

from busyboy import exceptions

SUBPROCESS_TIMEOUT_SECONDS = 10

NO_TOKEN_MESSAGE = (
    "No GitHub token available. Install the gh CLI and run `gh auth login`, or set GITHUB_TOKEN."
)


def _token_from_gh() -> str | None:
    """Ask the gh CLI for its token, or return None when it cannot supply one."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen pytest tests/test_github.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Lint and commit**

```bash
make test
git add src/busyboy/github.py tests/test_github.py
git commit -m "feat: resolve a GitHub token from gh or GITHUB_TOKEN"
```

---

### Task 4: `github.py` — workflow, run, and pull request queries

**Files:**
- Modify: `src/busyboy/github.py`
- Test: `tests/test_github.py`

**Interfaces:**
- Consumes: `exceptions.GitHubError`, `GitHubAuthError`, `GitHubTransientError` (Task 1); `github.resolve_token` (Task 3).
- Produces:
  - `github.Repo` — frozen pydantic model with `owner: str`, `name: str`, and a `slug` property returning `"owner/name"`
  - `github.Workflow` — `id: int`, `name: str`, `path: str`
  - `github.Run` — `id: int`, `status: str`, `conclusion: str | None`
  - `github.resolve_workflow(token: str, repo: Repo, reference: str) -> Workflow`
  - `github.latest_run(token: str, repo: Repo, workflow_id: int, branch: str) -> Run | None`
  - `github.pull_request_number(token: str, repo: Repo, branch: str) -> int | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_github.py` (and add `import responses` to its imports, sorted so `import responses` follows `import pytest`):

```python
REPO = github.Repo(owner="mb-dot-dev", name="busyboy")
TOKEN = "gho_test"

WORKFLOWS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows"
RUNS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows/42/runs"
PULLS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/pulls"

WORKFLOW_LIST = {
    "workflows": [
        {"id": 42, "name": "CI", "path": ".github/workflows/main.yaml"},
        {"id": 43, "name": "Release", "path": ".github/workflows/release.yaml"},
    ]
}


def test_the_repo_slug_is_owner_slash_name():
    assert REPO.slug == "mb-dot-dev/busyboy"


@responses.activate
@pytest.mark.parametrize("reference", ["42", "main.yaml", "CI"])
def test_a_workflow_resolves_by_id_filename_or_display_name(reference):
    responses.add(responses.GET, WORKFLOWS_URL, json=WORKFLOW_LIST, status=200)

    assert github.resolve_workflow(TOKEN, REPO, reference).id == 42


@responses.activate
def test_an_unknown_workflow_raises_listing_what_exists():
    responses.add(responses.GET, WORKFLOWS_URL, json=WORKFLOW_LIST, status=200)

    with pytest.raises(exceptions.GitHubError) as caught:
        github.resolve_workflow(TOKEN, REPO, "nope")

    assert "CI" in str(caught.value)
    assert "Release" in str(caught.value)


@responses.activate
def test_requests_carry_the_bearer_token_and_api_version():
    responses.add(responses.GET, WORKFLOWS_URL, json=WORKFLOW_LIST, status=200)

    github.resolve_workflow(TOKEN, REPO, "CI")

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


@responses.activate
def test_the_latest_run_is_the_first_for_the_branch():
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
        status=200,
    )

    run = github.latest_run(TOKEN, REPO, 42, "main")

    assert run is not None
    assert (run.id, run.status, run.conclusion) == (7, "completed", "success")
    assert "branch=main" in responses.calls[0].request.url


@responses.activate
def test_no_runs_yet_is_not_an_error():
    responses.add(responses.GET, RUNS_URL, json={"workflow_runs": []}, status=200)

    assert github.latest_run(TOKEN, REPO, 42, "main") is None


@responses.activate
def test_an_in_progress_run_has_no_conclusion():
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 8, "status": "in_progress", "conclusion": None}]},
        status=200,
    )

    run = github.latest_run(TOKEN, REPO, 42, "main")

    assert run is not None
    assert run.conclusion is None


@responses.activate
def test_an_open_pull_request_is_found_for_the_branch():
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}], status=200)

    assert github.pull_request_number(TOKEN, REPO, "feature/x") == 12
    assert "head=mb-dot-dev%3Afeature%2Fx" in responses.calls[0].request.url


@responses.activate
def test_no_open_pull_request_returns_none():
    responses.add(responses.GET, PULLS_URL, json=[], status=200)

    assert github.pull_request_number(TOKEN, REPO, "feature/x") is None


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_raises_a_fatal_auth_error(status):
    responses.add(responses.GET, WORKFLOWS_URL, json={"message": "Bad credentials"}, status=status)

    with pytest.raises(exceptions.GitHubAuthError):
        github.resolve_workflow(TOKEN, REPO, "CI")


@responses.activate
def test_a_server_error_is_transient():
    responses.add(responses.GET, RUNS_URL, json={"message": "oops"}, status=502)

    with pytest.raises(exceptions.GitHubTransientError):
        github.latest_run(TOKEN, REPO, 42, "main")


@responses.activate
def test_a_dropped_connection_is_transient():
    responses.add(responses.GET, RUNS_URL, body=requests.exceptions.ConnectionError("boom"))

    with pytest.raises(exceptions.GitHubTransientError):
        github.latest_run(TOKEN, REPO, 42, "main")


@responses.activate
def test_a_missing_repository_is_fatal_but_not_transient():
    responses.add(responses.GET, WORKFLOWS_URL, json={"message": "Not Found"}, status=404)

    with pytest.raises(exceptions.GitHubError) as caught:
        github.resolve_workflow(TOKEN, REPO, "CI")

    assert not isinstance(caught.value, exceptions.GitHubTransientError)
```

Add `import requests` to the test module's imports (sorted after `import pytest`, before `import responses`).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_github.py -v`
Expected: FAIL — `AttributeError: module 'busyboy.github' has no attribute 'Repo'`.

- [ ] **Step 3: Add the models and query layer**

Append to `src/busyboy/github.py`. The imports at the top become:

```python
import os
from pathlib import PurePosixPath
import subprocess
from typing import Any

from pydantic import BaseModel
import requests

from busyboy import exceptions
```

Then add below `resolve_token`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen pytest tests/test_github.py -v`
Expected: PASS, 21 tests.

- [ ] **Step 5: Lint and commit**

```bash
make test
git add src/busyboy/github.py tests/test_github.py
git commit -m "feat: query workflows, runs, and pull requests from GitHub"
```

---

### Task 5: Generate the status icons and ship them in the package

The generator is committed alongside its output so the icons can be re-tuned after hardware calibration (Task 10) without hand-editing binary files. Verified during planning: `uv_build` includes non-Python files under the package directory in the wheel with no `pyproject.toml` change.

**Files:**
- Create: `tools/generate_icons.py`
- Create: `src/busyboy/assets/{success,failure,pending,in_progress,cancelled,skipped}.png`
- Test: `tests/test_bar.py`

**Interfaces:**
- Consumes: nothing.
- Produces: six 12x12 RGBA PNGs at `src/busyboy/assets/<icon>.png`, one per `bar.IconName` value defined in Task 6.

- [ ] **Step 1: Write the generator**

Create `tools/generate_icons.py`. This is a development tool, not part of the shipped package — it is excluded from coverage because it never runs at import time.

```python
"""
Regenerate the 12x12 status icon PNGs in src/busyboy/assets.

Run with `uv run --frozen python tools/generate_icons.py`. Standard library
only: no image dependency is added to the project for six tiny files.

Each icon is a filled disc in a GitHub status colour with a glyph knocked out
to transparency, which reads as black on the bar's LED matrix.
"""

import math
import pathlib
import struct
import zlib

SIZE = 12
OUTPUT_DIRECTORY = pathlib.Path(__file__).resolve().parent.parent / "src" / "busyboy" / "assets"

CHECK = """
............
............
............
.........##.
........##..
..#....##...
..##..##....
...##.##....
....####....
.....##.....
............
............
"""

CROSS = """
............
............
............
...##..##...
....####....
.....##.....
.....##.....
....####....
...##..##...
............
............
............
"""

SLASH = """
............
............
............
.......##...
......##....
.....##.....
....##......
...##.......
..##........
............
............
............
"""

BAR = """
............
............
............
............
............
...######...
...######...
............
............
............
............
............
"""

Mask = list[list[bool]]


def parse(art: str) -> Mask:
    """Turn a 12-line block of '#'/'.' art into a boolean mask."""
    rows = art.strip().splitlines()
    if len(rows) != SIZE or any(len(row) != SIZE for row in rows):
        raise ValueError(f"art must be {SIZE}x{SIZE}")
    return [[char == "#" for char in row] for row in rows]


def disc(radius: float) -> Mask:
    """A filled circle centred on the grid."""
    centre = (SIZE - 1) / 2
    return [[math.hypot(x - centre, y - centre) <= radius for x in range(SIZE)] for y in range(SIZE)]


def ring(outer: float, inner: float) -> Mask:
    """An annulus centred on the grid."""
    centre = (SIZE - 1) / 2
    return [
        [inner <= math.hypot(x - centre, y - centre) <= outer for x in range(SIZE)]
        for y in range(SIZE)
    ]


def knock_out(base: Mask, glyph: Mask) -> Mask:
    """Remove the glyph from the base shape, leaving it transparent."""
    return [[lit and not cut for lit, cut in zip(base_row, glyph_row)] for base_row, glyph_row in zip(base, glyph)]


def render(mask: Mask) -> str:
    """Draw a mask as ASCII, for eyeballing a change before committing it."""
    return "\n".join("".join("#" if lit else "." for lit in row) for row in mask)


def png_bytes(mask: Mask, colour: str) -> bytes:
    """Encode a mask as an 8-bit RGBA PNG in the given #RRGGBB colour."""
    red, green, blue = (int(colour[index : index + 2], 16) for index in (1, 3, 5))
    raw = bytearray()
    for row in mask:
        raw.append(0)  # filter type 0 (None) for every scanline
        for lit in row:
            raw.extend((red, green, blue, 255) if lit else (0, 0, 0, 0))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# Colours are GitHub's dark-mode status palette, which is brighter than the
# light-mode one and reads better on an LED matrix.
ICONS: dict[str, tuple[Mask, str]] = {
    "success": (knock_out(disc(5.6), parse(CHECK)), "#3FB950"),
    "failure": (knock_out(disc(5.6), parse(CROSS)), "#F85149"),
    "pending": (disc(3.6), "#D29922"),
    "in_progress": (ring(5.6, 3.2), "#D29922"),
    "cancelled": (knock_out(disc(5.6), parse(SLASH)), "#8B949E"),
    "skipped": (knock_out(disc(5.6), parse(BAR)), "#8B949E"),
}


def main() -> None:
    """Write every icon, printing its ASCII rendering for review."""
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for name, (mask, colour) in ICONS.items():
        (OUTPUT_DIRECTORY / f"{name}.png").write_bytes(png_bytes(mask, colour))
        print(f"\n=== {name} ({colour}) ===")
        print(render(mask))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it and eyeball the output**

Run: `uv run --frozen python tools/generate_icons.py`

Expected: six blocks of ASCII art. Confirm each reads correctly at a glance — `success` is a disc with a check cut out, `failure` a disc with an X, `pending` a small solid dot, `in_progress` a ring, `cancelled` a disc with a diagonal slash, `skipped` a disc with a horizontal bar. In every knocked-out icon the disc's outer edge must remain unbroken; if a glyph severs it, the glyph is too large.

- [ ] **Step 3: Keep the generator out of coverage**

Coverage measures `src` only (`source = ["src"]` in `pyproject.toml`), and `tools/` is outside it. No change needed — confirm with the coverage run in Step 6.

- [ ] **Step 4: Write a test that the assets are real, loadable PNGs**

Add to `tests/test_bar.py`:

```python
def test_every_icon_ships_as_a_12x12_rgba_png():
    for icon in bar.ICON_NAMES:
        data = bar.icon_bytes(icon)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height, depth, colour_type = struct.unpack(">IIBB", data[16:26])
        assert (width, height, depth, colour_type) == (12, 12, 8, 6)
```

Add `import struct` to the test module's imports (sorted: `import json`, `import struct`, then `from urllib.parse import urlparse`).

This test depends on `bar.ICON_NAMES` and `bar.icon_bytes`, which Task 6 adds. Write the test now and expect it to fail until Task 6 lands; it is committed there.

- [ ] **Step 5: Commit the generator and its output**

```bash
git add tools/generate_icons.py src/busyboy/assets
git commit -m "feat: generate the six GitHub-style status icons"
```

- [ ] **Step 6: Confirm the wheel carries the assets**

```bash
uv build --wheel
python -c "import zipfile,glob; print([n for n in zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]).namelist() if n.endswith('.png')])"
rm -rf dist
```

Expected: all six `busyboy/assets/*.png` paths listed. If any are missing, add an explicit include under `[tool.uv.build-backend]` in `pyproject.toml` and re-run.

---

### Task 6: `bar.py` — image elements, the two-row payload, and asset upload

**Files:**
- Modify: `src/busyboy/bar.py`
- Test: `tests/test_bar.py`

**Interfaces:**
- Consumes: `bar._request` with a path (Task 1); the PNG assets (Task 5).
- Produces:
  - `bar.IconName` — `Literal["success", "failure", "pending", "in_progress", "cancelled", "skipped"]`
  - `bar.ICON_NAMES: tuple[str, ...]`
  - `bar.icon_bytes(icon: IconName) -> bytes`
  - `bar.upload_icons(config: BusyboyConfig) -> None`
  - `bar.build_workflow_payload(*, repo_label: str, ref_label: str, icon: IconName) -> DisplayElements`
  - Element ids `"repo"`, `"ref"`, `"icon"`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_bar.py`:

```python
def elements_by_id(payload):
    """Index a payload's elements by their stable element id."""
    return {element["id"]: element for element in payload.model_dump(exclude_none=True)["elements"]}


def test_the_workflow_payload_carries_two_rows_and_an_icon():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
    )

    assert set(elements) == {"repo", "ref", "icon"}
    assert elements["repo"]["text"] == "mb-dot-dev/busyboy"
    assert elements["ref"]["text"] == "#12"
    assert elements["icon"]["type"] == "image"
    assert elements["icon"]["path"] == "success.png"


def test_the_two_rows_share_a_text_column_beside_the_icon():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="main", icon="pending")
    )

    for row in (elements["repo"], elements["ref"]):
        assert row["x"] == bar.TEXT_X
        assert row["width"] == bar.TEXT_WIDTH
        assert row["display"] == "front"
        assert row["font"] == bar.ROW_FONT
    assert elements["repo"]["y"] == bar.ROW_ONE_Y
    assert elements["ref"]["y"] == bar.ROW_TWO_Y
    assert elements["icon"]["x"] == bar.ICON_X
    assert elements["icon"]["y"] == bar.ICON_Y


def test_the_icon_and_text_column_fit_the_front_display():
    assert bar.ICON_X + bar.ICON_SIZE <= bar.TEXT_X
    assert bar.TEXT_X + bar.TEXT_WIDTH == bar.FRONT_DISPLAY_WIDTH


def test_workflow_element_ids_are_stable_so_a_redraw_replaces():
    first = elements_by_id(bar.build_workflow_payload(repo_label="a/b", ref_label="#1", icon="success"))
    second = elements_by_id(bar.build_workflow_payload(repo_label="c/d", ref_label="#2", icon="failure"))

    assert set(first) == set(second)


def test_non_ascii_row_text_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_workflow_payload(repo_label="mb-dot-dev/büsyboy", ref_label="#12", icon="success")


@responses.activate
def test_uploading_icons_posts_every_asset_scoped_to_the_application(config):
    responses.add(responses.POST, "http://10.0.4.20/api/assets/upload", json={"result": "ok"}, status=200)

    bar.upload_icons(config)

    assert len(responses.calls) == len(bar.ICON_NAMES)
    uploaded = set()
    for call in responses.calls:
        query = parse_qs(urlparse(call.request.url).query)
        assert query["application_name"] == ["busyboy"]
        assert call.request.headers["Content-Type"] == "application/octet-stream"
        assert call.request.body[:8] == b"\x89PNG\r\n\x1a\n"
        uploaded.add(query["file"][0])
    assert uploaded == {f"{icon}.png" for icon in bar.ICON_NAMES}


@responses.activate
def test_a_failed_icon_upload_raises(config):
    responses.add(responses.POST, "http://10.0.4.20/api/assets/upload", json={"error": "nope"}, status=401)

    with pytest.raises(exceptions.BarError):
        bar.upload_icons(config)
```

Extend the `urllib.parse` import in the test module to `from urllib.parse import parse_qs, urlparse`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_bar.py -v`
Expected: FAIL — `AttributeError: module 'busyboy.bar' has no attribute 'build_workflow_payload'`.

- [ ] **Step 3: Add the constants and the image element**

In `src/busyboy/bar.py`, extend the imports (isort sorts `import x` and `from x import y` together by module name):

```python
from importlib import resources
import time
from typing import Literal, get_args
```

Add below `DEFAULT_TEXT_Y`:

```python
IconName = Literal["success", "failure", "pending", "in_progress", "cancelled", "skipped"]
ICON_NAMES: tuple[str, ...] = get_args(IconName)
ASSETS_PACKAGE = "busyboy.assets"

# Two-row workflow layout on the 72x16 front display: a 12x12 icon on the
# left, and a text column beside it carrying both rows. See the hardware-facts
# section of CLAUDE.md for how the row offsets were measured.
ICON_SIZE = 12
ICON_X = 2
ICON_Y = 2
TEXT_X = 18
TEXT_WIDTH = FRONT_DISPLAY_WIDTH - TEXT_X
ROW_ONE_Y = 1
ROW_TWO_Y = 9
ROW_FONT: DisplayFontName = "tiny"

REPO_ELEMENT_ID = "repo"
REF_ELEMENT_ID = "ref"
ICON_ELEMENT_ID = "icon"
```

Give `TextElement` an `x` (it currently has only `y`), inserting the field after `display`:

```python
    display: DisplayName = "front"
    x: int = 0
    y: int = 0
```

Add the image element after `TextElement`:

```python
class ImageElement(BaseModel):
    """An image drawn from a file previously uploaded to the app's assets."""

    id: str
    type: Literal["image"] = "image"
    path: str = Field(pattern=r"^[a-zA-Z0-9._/-]+$")
    display: DisplayName = "front"
    x: int = 0
    y: int = 0
```

Widen the container:

```python
class DisplayElements(BaseModel):
    """A draw request: one application's elements for the bar to render."""

    application_name: str
    elements: list[TextElement | ImageElement] = Field(min_length=1)
```

- [ ] **Step 4: Add the payload builder and the uploader**

Append to `src/busyboy/bar.py`:

```python
def _row(element_id: str, text: str, y: int) -> TextElement:
    """Build one row of the workflow layout, scrolling when it overflows the column."""
    return TextElement(
        id=element_id,
        text=text,
        font=ROW_FONT,
        display="front",
        x=TEXT_X,
        y=y,
        width=TEXT_WIDTH,
        scroll_rate=DEFAULT_SCROLL_RATE,
    )


def build_workflow_payload(*, repo_label: str, ref_label: str, icon: IconName) -> DisplayElements:
    """
    Build the two-row workflow layout: repository, pull request or branch, and a status icon.

    Element ids are stable, so redrawing replaces the previous elements rather
    than stacking new ones on top of them.
    """
    return DisplayElements(
        application_name=APPLICATION_NAME,
        elements=[
            ImageElement(id=ICON_ELEMENT_ID, path=f"{icon}.png", display="front", x=ICON_X, y=ICON_Y),
            _row(REPO_ELEMENT_ID, repo_label, ROW_ONE_Y),
            _row(REF_ELEMENT_ID, ref_label, ROW_TWO_Y),
        ],
    )


def icon_bytes(icon: IconName) -> bytes:
    """Read one packaged icon PNG."""
    return (resources.files(ASSETS_PACKAGE) / f"{icon}.png").read_bytes()


def upload_icons(config: BusyboyConfig) -> None:
    """
    Upload every status icon to the bar's asset store for this application.

    The upload is unconditional: the bar's API has no endpoint that lists an
    app's existing assets, and six ~200-byte requests cost less than the
    machinery to avoid them.
    """
    for icon in ICON_NAMES:
        _request(
            config,
            "POST",
            ASSET_UPLOAD_PATH,
            params={"application_name": APPLICATION_NAME, "file": f"{icon}.png"},
            data=icon_bytes(icon),
            content_type="application/octet-stream",
        )
```

Placement: `_row` and `build_workflow_payload` do no I/O, so they belong beside `build_text_payload`. `icon_bytes` and `upload_icons` go at the end of the module beside `clear`, since `upload_icons` calls `_request`. Leave `build_text_payload` exactly as it is — the `text` subcommand still uses it unchanged.

- [ ] **Step 5: Make the assets an importable package**

`resources.files("busyboy.assets")` needs `src/busyboy/assets/__init__.py`:

```python
"""Packaged status icon PNGs, generated by tools/generate_icons.py."""
```

- [ ] **Step 6: Run the tests**

Run: `make test`
Expected: PASS. The Task 5 asset test now passes too.

- [ ] **Step 7: Commit**

```bash
git add src/busyboy/bar.py src/busyboy/assets/__init__.py tests/test_bar.py
git commit -m "feat: draw a two-row workflow layout with an uploaded status icon"
```

---

### Task 7: `watch.py` — status mapping and one poll tick

**Files:**
- Create: `src/busyboy/watch.py`
- Test: `tests/test_watch.py`

**Interfaces:**
- Consumes: `github.Repo`, `github.Run`, `github.latest_run`, `github.pull_request_number` (Task 4); `bar.build_workflow_payload`, `bar.draw_text`, `bar.IconName` (Task 6); `exceptions.GitHubTransientError`, `exceptions.BarError` (Task 1).
- Produces:
  - `watch.Target` — frozen dataclass `repo: github.Repo`, `branch: str`, `workflow_id: int`
  - `watch.Screen` — frozen dataclass `repo_label: str`, `ref_label: str`, `icon: bar.IconName`
  - `watch.icon_for(run: github.Run | None) -> bar.IconName`
  - `watch.render(target: Target, run: github.Run | None, pull_request: int | None) -> Screen`
  - `watch.tick(config, token, target, previous: Screen | None) -> Screen | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watch.py`:

```python
"""Tests for the workflow poll loop."""

import re

import pytest
import requests
import responses

from busyboy import bar, exceptions, github, watch
from busyboy.config import load_config

REPO = github.Repo(owner="mb-dot-dev", name="busyboy")
TARGET = watch.Target(repo=REPO, branch="feature/x", workflow_id=42)
TOKEN = "gho_test"

RUNS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows/42/runs"
PULLS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/pulls"
DRAW_URL = re.compile(r"^http://[^/]+/api/display/draw")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


@pytest.fixture
def config():
    return load_config(host="10.0.4.20", token="testtoken")


def run(status, conclusion=None):
    return github.Run(id=1, status=status, conclusion=conclusion)


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("queued", None, "pending"),
        ("waiting", None, "pending"),
        ("pending", None, "pending"),
        ("requested", None, "pending"),
        ("in_progress", None, "in_progress"),
        ("completed", "success", "success"),
        ("completed", "failure", "failure"),
        ("completed", "timed_out", "failure"),
        ("completed", "startup_failure", "failure"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "skipped", "skipped"),
        ("completed", "neutral", "skipped"),
        ("completed", "action_required", "pending"),
    ],
)
def test_run_state_maps_to_a_github_style_icon(status, conclusion, expected):
    assert watch.icon_for(run(status, conclusion)) == expected


def test_an_unrecognised_status_falls_back_to_pending():
    assert watch.icon_for(run("teleported")) == "pending"


def test_an_unrecognised_conclusion_falls_back_to_pending():
    assert watch.icon_for(run("completed", "vaporised")) == "pending"


def test_no_run_at_all_shows_pending():
    assert watch.icon_for(None) == "pending"


def test_an_open_pull_request_is_shown_as_its_number():
    screen = watch.render(TARGET, run("completed", "success"), 12)

    assert screen == watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")


def test_without_a_pull_request_the_branch_name_is_shown():
    screen = watch.render(TARGET, run("in_progress"), None)

    assert screen.ref_label == "feature/x"


@responses.activate
def test_a_tick_draws_the_current_state(config):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    screen = watch.tick(config, TOKEN, TARGET, None)

    assert screen == watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
    assert len(responses.calls) == 3


@responses.activate
def test_an_unchanged_state_is_not_redrawn(config):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
    screen = watch.tick(config, TOKEN, TARGET, previous)

    assert screen == previous
    assert not [call for call in responses.calls if call.request.method == "POST"]


@responses.activate
def test_a_changed_state_is_redrawn(config):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "failure"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
    screen = watch.tick(config, TOKEN, TARGET, previous)

    assert screen is not None
    assert screen.icon == "failure"
    assert [call for call in responses.calls if call.request.method == "POST"]


@responses.activate
def test_a_transient_github_failure_keeps_the_previous_state(config):
    responses.add(responses.GET, RUNS_URL, json={"message": "oops"}, status=502)

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous) == previous


@responses.activate
def test_a_dropped_github_connection_keeps_the_previous_state(config):
    responses.add(responses.GET, RUNS_URL, body=requests.exceptions.ConnectionError("boom"))

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous) == previous


@responses.activate
def test_a_rejected_github_token_is_not_swallowed(config):
    responses.add(responses.GET, RUNS_URL, json={"message": "Bad credentials"}, status=401)

    with pytest.raises(exceptions.GitHubAuthError):
        watch.tick(config, TOKEN, TARGET, None)


@responses.activate
def test_an_unreachable_bar_keeps_the_previous_state(config, monkeypatch):
    monkeypatch.setattr(bar.time, "sleep", lambda seconds: None)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "failure"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, body=requests.exceptions.ConnectionError("boom"))

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous) == previous
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_watch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'busyboy.watch'`.

- [ ] **Step 3: Write the mapping and the tick**

Create `src/busyboy/watch.py`:

```python
"""The workflow poll loop: fetch, translate to a display state, draw when it changes."""

import dataclasses
import logging
import time

from busyboy import bar, exceptions, github
from busyboy.config import BusyboyConfig

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 10

# GitHub's own status vocabulary. A run reports `status` until it completes,
# at which point `conclusion` carries the outcome.
STATUS_ICONS: dict[str, bar.IconName] = {
    "queued": "pending",
    "waiting": "pending",
    "pending": "pending",
    "requested": "pending",
    "in_progress": "in_progress",
}

CONCLUSION_ICONS: dict[str, bar.IconName] = {
    "success": "success",
    "failure": "failure",
    "timed_out": "failure",
    "startup_failure": "failure",
    "cancelled": "cancelled",
    "skipped": "skipped",
    "neutral": "skipped",
    "action_required": "pending",
}

FALLBACK_ICON: bar.IconName = "pending"


@dataclasses.dataclass(frozen=True)
class Target:
    """What is being watched. Resolved once at startup; none of it changes mid-watch."""

    repo: github.Repo
    branch: str
    workflow_id: int


@dataclasses.dataclass(frozen=True)
class Screen:
    """Exactly what is on the bar. Comparing two of these decides whether to redraw."""

    repo_label: str
    ref_label: str
    icon: bar.IconName


def icon_for(run: github.Run | None) -> bar.IconName:
    """
    Map a run's state to a status icon.

    Unrecognised values fall back rather than raising: GitHub adds statuses and
    conclusions over time, and a watch loop should not die because of one.
    """
    if run is None:
        return FALLBACK_ICON
    if run.status == "completed":
        return CONCLUSION_ICONS.get(run.conclusion or "", FALLBACK_ICON)
    return STATUS_ICONS.get(run.status, FALLBACK_ICON)


def render(target: Target, run: github.Run | None, pull_request: int | None) -> Screen:
    """Turn a fetched run into the three things the display shows."""
    return Screen(
        repo_label=target.repo.slug,
        ref_label=f"#{pull_request}" if pull_request is not None else target.branch,
        icon=icon_for(run),
    )


def tick(
    config: BusyboyConfig,
    token: str,
    target: Target,
    previous: Screen | None,
) -> Screen | None:
    """
    Run one poll cycle, returning the state now on the bar.

    Transient GitHub failures and bar delivery failures leave the display
    untouched and return `previous`: a watch process is expected to outlive a
    laptop sleeping or a wifi hiccup. Auth failures are not caught here — they
    never self-heal, so they propagate and end the watch.
    """
    try:
        run = github.latest_run(token, target.repo, target.workflow_id, target.branch)
        pull_request = github.pull_request_number(token, target.repo, target.branch)
    except exceptions.GitHubTransientError as error:
        LOGGER.debug("GitHub request failed, keeping the display as it is: %s", error)
        return previous

    screen = render(target, run, pull_request)
    if screen == previous:
        return previous

    try:
        bar.draw_text(config, bar.build_workflow_payload(
            repo_label=screen.repo_label,
            ref_label=screen.ref_label,
            icon=screen.icon,
        ))
    except exceptions.BarError as error:
        LOGGER.debug("Draw failed, keeping the display as it is: %s", error)
        return previous
    return screen
```

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen pytest tests/test_watch.py -v`
Expected: PASS, 24 tests.

- [ ] **Step 5: Lint and commit**

```bash
make test
git add src/busyboy/watch.py tests/test_watch.py
git commit -m "feat: map workflow runs to a display state and draw on change"
```

---

### Task 8: `watch.py` — the driver loop and Ctrl+C

**Files:**
- Modify: `src/busyboy/watch.py`
- Test: `tests/test_watch.py`

**Interfaces:**
- Consumes: `watch.tick` (Task 7); `bar.upload_icons`, `bar.clear` (Task 6).
- Produces: `watch.watch(config, token, target, *, interval=DEFAULT_INTERVAL_SECONDS, sleep=time.sleep) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_watch.py`:

```python
@responses.activate
def test_the_loop_uploads_icons_then_polls_until_interrupted(config):
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})

    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    assert slept == [10, 10]
    uploads = [call for call in responses.calls if "assets/upload" in call.request.url]
    assert len(uploads) == len(bar.ICON_NAMES)
    assert [call for call in responses.calls if call.request.method == "DELETE"]


@responses.activate
def test_an_interrupt_clears_the_display(config):
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(responses.GET, RUNS_URL, json={"workflow_runs": []})
    responses.add(responses.GET, PULLS_URL, json=[])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    deletes = [call for call in responses.calls if call.request.method == "DELETE"]
    assert len(deletes) == 1


@responses.activate
def test_a_fatal_error_still_clears_the_display(config):
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(responses.GET, RUNS_URL, json={"message": "Bad credentials"}, status=401)
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})

    with pytest.raises(exceptions.GitHubAuthError):
        watch.watch(config, TOKEN, TARGET, interval=10, sleep=lambda seconds: None)

    assert [call for call in responses.calls if call.request.method == "DELETE"]


@responses.activate
def test_a_bar_that_dies_during_cleanup_does_not_mask_the_interrupt(config, monkeypatch):
    monkeypatch.setattr(bar.time, "sleep", lambda seconds: None)
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(responses.GET, RUNS_URL, json={"workflow_runs": []})
    responses.add(responses.GET, PULLS_URL, json=[])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})
    responses.add(responses.DELETE, DRAW_URL, body=requests.exceptions.ConnectionError("boom"))

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)


@responses.activate
def test_a_failed_icon_upload_stops_before_the_loop(config):
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"error": "no"}, status=401)

    with pytest.raises(exceptions.BarError):
        watch.watch(config, TOKEN, TARGET, interval=10, sleep=lambda seconds: None)

    assert not [call for call in responses.calls if "actions/workflows" in call.request.url]
```

Add `from collections.abc import Callable` to `watch.py`'s imports when writing Step 2 — isort sorts it before `import dataclasses`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_watch.py -v`
Expected: FAIL — `AttributeError: module 'busyboy.watch' has no attribute 'watch'`.

- [ ] **Step 3: Write the driver**

Append to `src/busyboy/watch.py`:

```python
def watch(
    config: BusyboyConfig,
    token: str,
    target: Target,
    *,
    interval: int = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """
    Poll until interrupted, then clear the display.

    `sleep` is injected so tests can drive the loop without waiting, and can
    end it by raising KeyboardInterrupt the way Ctrl+C does.

    The display is cleared on the way out however the loop ends, including on a
    fatal error: leaving a stale workflow status on the bar after the process
    is gone would be worse than showing nothing.
    """
    bar.upload_icons(config)
    screen: Screen | None = None
    try:
        while True:
            screen = tick(config, token, target, screen)
            sleep(interval)
    except KeyboardInterrupt:
        LOGGER.debug("Interrupted, clearing the display")
    finally:
        try:
            bar.clear(config)
        except exceptions.BarError as error:
            # Never let cleanup mask why the loop actually ended.
            LOGGER.debug("Could not clear the display on exit: %s", error)
```

Change the imports at the top of `watch.py` to:

```python
from collections.abc import Callable
import dataclasses
import logging
import time
```

- [ ] **Step 4: Run the tests**

Run: `uv run --frozen pytest tests/test_watch.py -v`
Expected: PASS, 29 tests.

- [ ] **Step 5: Lint and commit**

```bash
make test
git add src/busyboy/watch.py tests/test_watch.py
git commit -m "feat: poll until interrupted, clearing the display on exit"
```

---

### Task 9: `cli.py` — the `gh workflow` command

**Files:**
- Modify: `src/busyboy/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: `busyboy gh workflow <workflow>` with `--branch`, `--repo`, `--interval`, plus the shared `--host`, `--token`, `--verbose`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
GITHUB_RUNS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows/42/runs"
GITHUB_PULLS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/pulls"
GITHUB_WORKFLOWS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows"
UPLOAD_URL_PATTERN = re.compile(r"^http://[^/]+/api/assets/upload")


@pytest.fixture
def github_bar(monkeypatch):
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
    responses.add(responses.POST, UPLOAD_URL_PATTERN, json={"result": "ok"})
    responses.add(responses.POST, DRAW_URL_PATTERN, json={"result": "ok"})
    responses.add(responses.DELETE, DRAW_URL_PATTERN, json={"result": "ok"})


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
    draws = [call for call in responses.calls if call.request.method == "POST" and "display/draw" in call.request.url]
    assert len(draws) == 1
    body = json.loads(draws[0].request.body)
    elements = {element["id"]: element for element in body["elements"]}
    assert elements["repo"]["text"] == "mb-dot-dev/busyboy"
    assert elements["ref"]["text"] == "#12"
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


def test_a_malformed_repo_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["gh", "workflow", "CI", "--repo", "busyboy"], env=ENV)

    assert result.exit_code == 2
    assert "owner/name" in result.stderr


@responses.activate
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
```

Extend the test module's imports with `from busyboy import cli, exceptions, github`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --frozen pytest tests/test_cli.py -v`
Expected: FAIL — `AttributeError: module 'busyboy.cli' has no attribute 'github'`, and `No such command 'gh'`.

- [ ] **Step 3: Add the command**

In `src/busyboy/cli.py`, extend the imports:

```python
from busyboy import bar, exceptions, git, github, watch
from busyboy.config import ConfigError, load_config
```

Append at the end of the module:

```python
def _parse_repo(value: str) -> github.Repo:
    """Split an explicit --repo into owner and name."""
    owner, separator, name = value.partition("/")
    if not (owner and separator and name) or "/" in name:
        raise click.BadParameter("expected owner/name", param_hint="--repo")
    return github.Repo(owner=owner, name=name)


@main.group()
def gh() -> None:
    """Show GitHub information on the bar."""


@gh.command()
@click.argument("workflow_reference", metavar="WORKFLOW")
@click.option(
    "--branch",
    default=None,
    help="Branch to watch. Defaults to the current checkout's branch.",
)
@click.option(
    "--repo",
    "repo_option",
    default=None,
    help="Repository as owner/name. Defaults to origin's.",
)
@click.option(
    "--interval",
    type=click.IntRange(min=1),
    default=watch.DEFAULT_INTERVAL_SECONDS,
    show_default=True,
    help="Seconds between polls.",
)
@_connection_options
@_handle_errors
def workflow(
    workflow_reference: str,
    branch: str | None,
    repo_option: str | None,
    interval: int,
    host: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """
    Watch a GitHub Actions workflow on the bar until Ctrl+C.

    WORKFLOW is a workflow id, filename, or display name.
    """
    _configure_logging(verbose=verbose)
    config = load_config(host=host, token=token)
    github_token = github.resolve_token()
    if repo_option:
        repo = _parse_repo(repo_option)
    else:
        owner, name = git.origin_repo()
        repo = github.Repo(owner=owner, name=name)
    target = watch.Target(
        repo=repo,
        branch=branch or git.current_branch(),
        workflow_id=github.resolve_workflow(github_token, repo, workflow_reference).id,
    )
    watch.watch(config, github_token, target, interval=interval)
```

`Repo` is a pydantic model, so it must be constructed with keyword arguments — never `github.Repo(*git.origin_repo())`.

- [ ] **Step 4: Run the tests**

Run: `make test`
Expected: PASS. `bar` is still imported by `cli.py` for the `text` command's font choices, so the unused-import lint does not fire.

- [ ] **Step 5: Check coverage has not dropped**

Run: `make coverage`
Expected: total at or above the 75% floor. If any new module is materially below the rest, add the missing case rather than lowering the floor.

- [ ] **Step 6: Commit**

```bash
git add src/busyboy/cli.py tests/test_cli.py
git commit -m "feat: add the gh workflow command"
```

---

### Task 10: Calibrate the layout against a real bar and record what it shows

Every number in the two-row layout is a hypothesis. `CLAUDE.md` records the `condensed` glyph box as 9 rows, too tall to stack twice in 16 pixels, and neither `tiny` nor `small` has ever been measured on this hardware. This task replaces the guesses with measurements.

**Files:**
- Modify: `src/busyboy/bar.py` (the row constants, if calibration disagrees)
- Modify: `tools/generate_icons.py` (only if the icons need resizing)
- Modify: `CLAUDE.md` (hardware facts, architecture, CLI contract)

**Interfaces:**
- Consumes: the finished command (Task 9).
- Produces: verified values for `ROW_FONT`, `ROW_ONE_Y`, `ROW_TWO_Y`, `TEXT_X`, `TEXT_WIDTH`, `ICON_X`, `ICON_Y`.

- [ ] **Step 1: Point the CLI at the real bar**

Run: `source ~/.zshrc && busybarenv`

This exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN` for the real device. Never print or commit the token value.

- [ ] **Step 2: Watch a workflow and look at the bar**

```bash
uv run --frozen busyboy gh workflow main.yaml --branch main
```

Check, in order:

1. Both rows are fully visible — no glyphs clipped off the top or bottom of the 16-row display.
2. The two rows do not overlap each other.
3. The icon is clear of the text column and is not clipped.
4. A long repository slug scrolls, and short text does not.

- [ ] **Step 3: Adjust the constants until all four hold**

If rows clip or collide, try `ROW_FONT = "tiny"` with `ROW_ONE_Y` / `ROW_TWO_Y` of `(0, 8)`, then `(1, 9)`, then `(2, 10)`. If `tiny` is unreadable, try `"small"` and accept whatever row offsets fit; if `small` cannot fit twice in 16 rows, keep `tiny`.

After each change, re-run Step 2. Update the assertions in `tests/test_bar.py` that name these constants — they assert the layout is self-consistent (`TEXT_X + TEXT_WIDTH == FRONT_DISPLAY_WIDTH`), so they stay correct as long as the constants stay consistent.

- [ ] **Step 4: Verify the redraw assumption**

The whole diff-before-draw design rests on redraws restarting the scroll animation. Confirm it: with a repo slug long enough to scroll, watch a full poll interval and check the text does not jump back to its starting position every 10 seconds.

If redraws turn out **not** to reset scroll position, keep the diff anyway — it removes pointless traffic — but record the correction in `CLAUDE.md` so nobody re-derives it.

- [ ] **Step 5: Verify Ctrl+C leaves the bar clean**

Press Ctrl+C. The display must return to whatever it was showing before, with no busyboy elements left behind, and the shell must get exit code 0 (`echo $?`).

- [ ] **Step 6: Record the measurements in `CLAUDE.md`**

Add to the **Hardware facts** section, with the values actually measured:

```markdown
- **Two rows fit on the front display** with the `<font>` font at `y=<a>` and `y=<b>`. The `<font>`
  glyph box is `<n>` rows tall. `condensed` (9 rows) cannot be stacked twice in 16 rows.
- **Redrawing an element `<does|does not>` restart its scroll animation.** This is why
  `watch.tick` compares the rendered state and skips the draw when nothing changed.
- **Image elements reference an uploaded asset by bare filename** (`success.png`), after a
  `POST /api/assets/upload?application_name=busyboy&file=success.png` with an
  `application/octet-stream` body. There is no endpoint that lists an app's existing assets, so
  busyboy re-uploads all six icons at every startup.
```

Update the **Architecture** section to list `git.py`, `github.py`, `watch.py`, and `assets/`, and note that `_request` now takes a path.

Update the **CLI contract** section:

```markdown
`busyboy gh workflow WORKFLOW` departs from the one-shot contract on purpose: it runs until Ctrl+C,
and inside the loop it swallows transient GitHub and bar failures (logged at DEBUG, visible under
`--verbose`) rather than exiting. Startup failures — no token, no git repo, unknown workflow,
rejected credentials — still print one line and exit 1. The GitHub token comes from `gh auth token`,
then `GITHUB_TOKEN`; it is never written to a log or an error message.
```

Add to **Testing**:

```markdown
- Subprocesses (`git`, `gh auth token`) are monkeypatched at the `subprocess.run` boundary only —
  never at the `git.py` / `github.py` function boundary, so the argv and parsing stay under test.
- `watch.watch` takes an injected `sleep`, so tests drive the loop synchronously and end it by
  raising `KeyboardInterrupt` the way Ctrl+C does.
```

- [ ] **Step 7: Update the README**

Add the new command to the README's usage section, showing the default (current branch) form and the `--repo` / `--branch` overrides.

- [ ] **Step 8: Final verification and commit**

```bash
make test
make coverage
git add -A
git commit -m "docs: record the measured two-row layout and the gh workflow contract"
```

- [ ] **Step 9: Open the pull request**

```bash
git push -u origin feature/gh-workflow-watch
gh pr create --fill
```

Then watch CI with the thing you just built:

```bash
uv run --frozen busyboy gh workflow main.yaml
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: command surface → Task 9; data flow → Tasks 2, 4, 7; redraw-on-change → Task 7 (verified in Task 10); authentication → Task 3; modules → Tasks 1–8 with the one documented deviation; display layout → Task 6, calibrated in Task 10; icons and status mapping → Tasks 5 and 7; asset upload → Task 6; error handling → every row of the spec's table has a test in Tasks 2, 3, 4, 7, 8, or 9; testing → the test steps throughout. The spec's "out of scope" list is not implemented anywhere, as intended.

**Placeholders.** None. Every code step carries the actual code; every test step carries the actual test.

**Type consistency.** `Repo` is a pydantic model constructed with keywords everywhere (the one place Task 9 nearly used positional args is called out and corrected in that task's Step 3). `IconName` values match the six asset filenames in Task 5, the six `ICON_NAMES` in Task 6, and every value in `STATUS_ICONS` / `CONCLUSION_ICONS` in Task 7. `_request`'s signature in Task 1 matches all four call sites (Tasks 1 and 6). `git.origin_repo() -> tuple[str, str]` is unpacked into `Repo(owner=..., name=...)` in Task 9, consistent with the documented deviation.
