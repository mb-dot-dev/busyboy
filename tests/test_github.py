"""Tests for GitHub token resolution and REST queries."""

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
