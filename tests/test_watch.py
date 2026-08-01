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
def test_a_non_ascii_branch_name_completes_and_draws_instead_of_raising(config):
    unicode_target = watch.Target(repo=REPO, branch="feature/café", workflow_id=42)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    screen = watch.tick(config, TOKEN, unicode_target, None)

    assert screen is not None
    assert screen.icon == "success"
    assert [call for call in responses.calls if call.request.method == "POST"]


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
