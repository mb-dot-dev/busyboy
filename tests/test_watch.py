"""Tests for the workflow poll loop."""

import time

import httpx2
import pytest
import requests
import responses

from busyboy import bar, exceptions, github, watch
from busyboy.config import load_config

REPO = github.Repo(owner="mb-dot-dev", name="busyboy")
WORKFLOW = github.Workflow(id=42, name="CI", path=".github/workflows/main.yaml")
TARGET = watch.Target(repo=REPO, branch="feature/x", workflow=WORKFLOW)
TOKEN = "gho_test"

RUNS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/actions/workflows/42/runs"
PULLS_URL = "https://api.github.com/repos/mb-dot-dev/busyboy/pulls"
DRAW_PATH = "/api/display/draw"
UPLOAD_PATH = "/api/assets/upload"


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

    assert screen == watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")


def test_without_a_pull_request_the_branch_name_is_shown():
    screen = watch.render(TARGET, run("in_progress"), None)

    assert screen.ref_label == "feature/x CI"


@responses.activate
def test_a_tick_draws_the_current_state(config, bar_transport):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", DRAW_PATH)

    result = watch.tick(config, TOKEN, TARGET, None)

    assert result.screen == watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")
    assert result.retry_after is None
    assert len(responses.calls) == 2
    assert len(bar_transport.calls) == 1


@responses.activate
def test_an_unchanged_state_is_not_redrawn(config, bar_transport):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", DRAW_PATH)

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")
    result = watch.tick(config, TOKEN, TARGET, previous)

    assert result.screen == previous
    assert bar_transport.calls == []


@responses.activate
def test_a_changed_state_is_redrawn(config, bar_transport):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "failure"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", DRAW_PATH)

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")
    result = watch.tick(config, TOKEN, TARGET, previous)

    assert result.screen is not None
    assert result.screen.icon == "failure"
    assert len(bar_transport.calls) == 1


@responses.activate
def test_a_transient_github_failure_keeps_the_previous_state(config):
    responses.add(responses.GET, RUNS_URL, json={"message": "oops"}, status=502)

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous).screen == previous


@responses.activate
def test_a_dropped_github_connection_keeps_the_previous_state(config):
    responses.add(responses.GET, RUNS_URL, body=requests.exceptions.ConnectionError("boom"))

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous).screen == previous


@responses.activate
def test_a_rate_limited_response_carries_retry_after_into_the_result(config):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"message": "rate limited"},
        status=429,
        headers={"Retry-After": "60"},
    )

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")
    result = watch.tick(config, TOKEN, TARGET, previous)

    assert result.screen == previous
    assert result.retry_after == 60.0


@responses.activate
def test_an_unparseable_retry_after_leaves_retry_after_none(config):
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"message": "rate limited"},
        status=429,
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )

    result = watch.tick(config, TOKEN, TARGET, None)

    assert result.retry_after is None


@responses.activate
def test_a_rejected_github_token_is_not_swallowed(config):
    responses.add(responses.GET, RUNS_URL, json={"message": "Bad credentials"}, status=401)

    with pytest.raises(exceptions.GitHubAuthError):
        watch.tick(config, TOKEN, TARGET, None)


@responses.activate
def test_a_non_ascii_branch_name_completes_and_draws_instead_of_raising(config, bar_transport):
    unicode_target = watch.Target(repo=REPO, branch="feature/café", workflow=WORKFLOW)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[])
    bar_transport.add("POST", DRAW_PATH)

    result = watch.tick(config, TOKEN, unicode_target, None)

    assert result.screen is not None
    assert result.screen.icon == "success"
    assert len(bar_transport.calls) == 1


@responses.activate
def test_an_unreachable_bar_keeps_the_previous_state(config, bar_transport, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "failure"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", DRAW_PATH, error=httpx2.ConnectError("boom"))

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12 CI", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous).screen == previous


@responses.activate
def test_the_loop_uploads_icons_then_polls_until_interrupted(config, bar_transport):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", DRAW_PATH)

    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    assert slept == [10, 10]
    uploads = [call for call in bar_transport.calls if call.url.path == UPLOAD_PATH]
    assert len(uploads) == len(bar.ICON_NAMES)
    deletes = [call for call in bar_transport.calls if call.method == "DELETE"]
    assert len(deletes) == 2


@responses.activate
def test_clear_precedes_the_first_draw(config, bar_transport):
    """A stale element from another busyboy invocation must be gone before the workflow layout is drawn."""
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    bar_transport.add("POST", DRAW_PATH)

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    draw_calls = [call for call in bar_transport.calls if call.url.path == DRAW_PATH]
    methods = [call.method for call in draw_calls]
    assert methods.index("DELETE") < methods.index("POST")


@responses.activate
def test_the_loop_waits_at_least_retry_after_when_rate_limited(config, bar_transport):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"message": "rate limited"},
        status=429,
        headers={"Retry-After": "60"},
    )

    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    assert slept == [60.0]


@responses.activate
def test_an_unparseable_retry_after_does_not_change_the_wait(config, bar_transport):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"message": "rate limited"},
        status=429,
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )

    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    assert slept == [10]


@responses.activate
def test_an_interrupt_clears_the_display(config, bar_transport):
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(responses.GET, RUNS_URL, json={"workflow_runs": []})
    responses.add(responses.GET, PULLS_URL, json=[])
    bar_transport.add("POST", DRAW_PATH)
    bar_transport.add("DELETE", DRAW_PATH)

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    # One DELETE clears stale elements before the loop starts, a second clears on the way out.
    deletes = [call for call in bar_transport.calls if call.method == "DELETE"]
    assert len(deletes) == 2


@responses.activate
def test_a_fatal_error_still_clears_the_display(config, bar_transport):
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(responses.GET, RUNS_URL, json={"message": "Bad credentials"}, status=401)
    bar_transport.add("DELETE", DRAW_PATH)

    with pytest.raises(exceptions.GitHubAuthError):
        watch.watch(config, TOKEN, TARGET, interval=10, sleep=lambda seconds: None)

    assert [call for call in bar_transport.calls if call.method == "DELETE"]


@responses.activate
def test_a_bar_that_dies_during_cleanup_does_not_mask_the_interrupt(config, bar_transport, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    # The first DELETE (the pre-loop clear) succeeds; the second (cleanup on exit) fails.
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", UPLOAD_PATH)
    responses.add(responses.GET, RUNS_URL, json={"workflow_runs": []})
    responses.add(responses.GET, PULLS_URL, json=[])
    bar_transport.add("POST", DRAW_PATH)
    bar_transport.add("DELETE", DRAW_PATH, error=httpx2.ConnectError("boom"))

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)


@responses.activate
def test_a_failed_icon_upload_stops_before_the_loop(config, bar_transport):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", UPLOAD_PATH, status=401, json={"error": "no"})

    with pytest.raises(exceptions.BarError):
        watch.watch(config, TOKEN, TARGET, interval=10, sleep=lambda seconds: None)

    assert not [call for call in responses.calls if "actions/workflows" in (call.request.url or "")]


def test_an_emoji_in_the_workflow_name_is_dropped_rather_than_replaced():
    target = watch.Target(
        repo=REPO,
        branch="main",
        workflow=github.Workflow(id=42, name="🚀 Deploy", path=".github/workflows/deploy.yaml"),
    )

    screen = watch.render(target, run("completed", "success"), 7)

    assert screen.ref_label == "#7 Deploy"


def test_a_workflow_name_with_nothing_displayable_is_omitted_entirely():
    """The row falls back to exactly the ref, with no orphaned separator."""
    target = watch.Target(
        repo=REPO,
        branch="main",
        workflow=github.Workflow(id=42, name="🚀", path=".github/workflows/deploy.yaml"),
    )

    screen = watch.render(target, run("completed", "success"), 7)

    assert screen.ref_label == "#7"
