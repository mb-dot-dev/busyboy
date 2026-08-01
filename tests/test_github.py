"""Tests for GitHub token resolution and REST queries."""

import pytest
import requests
import responses

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
    assert responses.calls[0].request.url is not None
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
    assert responses.calls[0].request.url is not None
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


@responses.activate
def test_a_403_with_an_exhausted_rate_limit_header_is_transient():
    responses.add(
        responses.GET,
        WORKFLOWS_URL,
        json={"message": "rate limited"},
        status=403,
        headers={"x-ratelimit-remaining": "0"},
    )

    with pytest.raises(exceptions.GitHubTransientError):
        github.resolve_workflow(TOKEN, REPO, "CI")


@responses.activate
def test_a_403_without_rate_limit_evidence_is_still_a_fatal_auth_error():
    responses.add(responses.GET, WORKFLOWS_URL, json={"message": "Bad credentials"}, status=403)

    with pytest.raises(exceptions.GitHubAuthError):
        github.resolve_workflow(TOKEN, REPO, "CI")


@responses.activate
def test_a_429_is_transient_even_without_rate_limit_headers():
    responses.add(responses.GET, WORKFLOWS_URL, json={"message": "rate limited"}, status=429)

    with pytest.raises(exceptions.GitHubTransientError):
        github.resolve_workflow(TOKEN, REPO, "CI")


@responses.activate
def test_a_non_list_pulls_payload_is_transient_not_a_keyerror():
    responses.add(responses.GET, PULLS_URL, json={"message": "unexpected shape"}, status=200)

    with pytest.raises(exceptions.GitHubTransientError):
        github.pull_request_number(TOKEN, REPO, "feature/x")


@responses.activate
def test_a_run_with_a_non_numeric_id_is_transient_not_a_validation_error():
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": "not-an-int", "status": "completed", "conclusion": "success"}]},
        status=200,
    )

    with pytest.raises(exceptions.GitHubTransientError):
        github.latest_run(TOKEN, REPO, 42, "main")
