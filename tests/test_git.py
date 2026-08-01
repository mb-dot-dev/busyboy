"""Tests for local git repository inspection."""

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


def test_a_credentialed_unparseable_url_does_not_leak_the_credential():
    secret = "s3cr3t-token"

    with pytest.raises(exceptions.GitError) as exc_info:
        git.parse_remote_url(f"https://user:{secret}@host")

    assert secret not in str(exc_info.value)


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


def test_a_non_executable_git_binary_raises(monkeypatch):
    """git present on PATH but not executable raises PermissionError, an OSError subclass, not FileNotFoundError."""

    def run(argv, **kwargs):
        raise PermissionError("git")

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
