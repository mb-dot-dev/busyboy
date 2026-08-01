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

    result = watch.tick(config, TOKEN, TARGET, None)

    assert result.screen == watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
    assert result.retry_after is None
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
    result = watch.tick(config, TOKEN, TARGET, previous)

    assert result.screen == previous
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
    result = watch.tick(config, TOKEN, TARGET, previous)

    assert result.screen is not None
    assert result.screen.icon == "failure"
    assert [call for call in responses.calls if call.request.method == "POST"]


@responses.activate
def test_a_transient_github_failure_keeps_the_previous_state(config):
    responses.add(responses.GET, RUNS_URL, json={"message": "oops"}, status=502)

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")

    assert watch.tick(config, TOKEN, TARGET, previous).screen == previous


@responses.activate
def test_a_dropped_github_connection_keeps_the_previous_state(config):
    responses.add(responses.GET, RUNS_URL, body=requests.exceptions.ConnectionError("boom"))

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")

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

    previous = watch.Screen(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
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
def test_a_non_ascii_branch_name_completes_and_draws_instead_of_raising(config):
    unicode_target = watch.Target(repo=REPO, branch="feature/café", workflow_id=42)
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    result = watch.tick(config, TOKEN, unicode_target, None)

    assert result.screen is not None
    assert result.screen.icon == "success"
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

    assert watch.tick(config, TOKEN, TARGET, previous).screen == previous


@responses.activate
def test_the_loop_uploads_icons_then_polls_until_interrupted(config):
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    assert slept == [10, 10]
    uploads = [call for call in responses.calls if "assets/upload" in (call.request.url or "")]
    assert len(uploads) == len(bar.ICON_NAMES)
    deletes = [call for call in responses.calls if call.request.method == "DELETE"]
    assert len(deletes) == 2


@responses.activate
def test_clear_precedes_the_first_draw(config):
    """A stale element from another busyboy invocation must be gone before the workflow layout is drawn."""
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(
        responses.GET,
        RUNS_URL,
        json={"workflow_runs": [{"id": 7, "status": "completed", "conclusion": "success"}]},
    )
    responses.add(responses.GET, PULLS_URL, json=[{"number": 12}])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    draw_calls = [
        call for call in responses.calls if call.request.url is not None and "/api/display/draw" in call.request.url
    ]
    methods = [call.request.method for call in draw_calls]
    assert methods.index("DELETE") < methods.index("POST")


@responses.activate
def test_the_loop_waits_at_least_retry_after_when_rate_limited(config):
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
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
def test_an_unparseable_retry_after_does_not_change_the_wait(config):
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
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
def test_an_interrupt_clears_the_display(config):
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"result": "ok"})
    responses.add(responses.GET, RUNS_URL, json={"workflow_runs": []})
    responses.add(responses.GET, PULLS_URL, json=[])
    responses.add(responses.POST, DRAW_URL, json={"result": "ok"})
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch(config, TOKEN, TARGET, interval=10, sleep=sleep)

    # One DELETE clears stale elements before the loop starts, a second clears on the way out.
    deletes = [call for call in responses.calls if call.request.method == "DELETE"]
    assert len(deletes) == 2


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
    # The first DELETE (the pre-loop clear) succeeds; the second (cleanup on exit) fails.
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})
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
    responses.add(responses.DELETE, DRAW_URL, json={"result": "ok"})
    responses.add(responses.POST, re.compile(r"^http://[^/]+/api/assets/upload"), json={"error": "no"}, status=401)

    with pytest.raises(exceptions.BarError):
        watch.watch(config, TOKEN, TARGET, interval=10, sleep=lambda seconds: None)

    assert not [call for call in responses.calls if "actions/workflows" in (call.request.url or "")]
