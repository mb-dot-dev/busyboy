"""Tests for the workflow poll loop."""

from datetime import UTC, datetime, timedelta
import itertools
import json
import time

import httpx2
import pytest
import requests
import responses

from busyboy import bar, claude, exceptions, github, watch
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


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def usage_body(
    session=3.0, weekly=26.0, session_resets=timedelta(hours=3, minutes=59), weekly_resets=timedelta(days=2)
):
    def window(utilization, resets):
        return {"utilization": utilization, "resets_at": None if resets is None else (NOW + resets).isoformat()}

    return {"five_hour": window(session, session_resets), "seven_day": window(weekly, weekly_resets)}


@pytest.fixture
def claude_token(monkeypatch):
    """Stand in for Claude credentials, counting how often the token is resolved."""
    calls = []

    def resolve():
        calls.append(1)
        return "claude-test-token"

    monkeypatch.setattr(watch.claude, "resolve_token", resolve)
    return calls


def draw_bodies(bar_transport):
    return [
        json.loads(call.content) for call in bar_transport.calls if call.method == "POST" and call.url.path == DRAW_PATH
    ]


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(hours=3, minutes=59), "3h59m"),
        (timedelta(hours=3, minutes=5), "3h05m"),
        (timedelta(minutes=60), "1h00m"),
        (timedelta(minutes=59), "59m"),
        (timedelta(0), "0m"),
        (timedelta(minutes=-5), "0m"),
    ],
)
def test_session_countdown_format(remaining, expected):
    assert watch.format_session_countdown(remaining) == expected


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (timedelta(days=6, hours=3), "6d"),
        (timedelta(hours=48), "2d"),
        (timedelta(hours=47, minutes=59), "1d 23h"),
        (timedelta(days=1, hours=5), "1d 5h"),
        (timedelta(hours=24), "1d 0h"),
        (timedelta(hours=23, minutes=59), "23h59m"),
        (timedelta(hours=9, minutes=5), "9h05m"),
        (timedelta(minutes=59), "59m"),
        (timedelta(0), "0m"),
        (timedelta(hours=-3), "0m"),
    ],
)
def test_weekly_countdown_format(remaining, expected):
    assert watch.format_weekly_countdown(remaining) == expected


def usage(session, weekly, session_resets=timedelta(hours=1), weekly_resets=timedelta(days=3)):
    return claude.Usage(
        session=claude.Window(utilization=session, resets_at=None if session_resets is None else NOW + session_resets),
        weekly=claude.Window(utilization=weekly, resets_at=None if weekly_resets is None else NOW + weekly_resets),
    )


@pytest.mark.parametrize(
    ("kind", "utilization", "remaining", "expected"),
    [
        ("session", 85.0, timedelta(hours=3, minutes=49), "5h 85% 3h49m"),
        ("weekly", 40.0, timedelta(days=6, hours=2), "wk 40% 6d"),
        ("weekly", 40.0, timedelta(hours=29), "wk 40% 1d 5h"),
        ("session", 99.6, timedelta(hours=1), "5h 100% 1h00m"),
        ("session", 150.0, timedelta(hours=1), "5h 100% 1h00m"),
        ("session", -3.0, timedelta(hours=1), "5h 0% 1h00m"),
        ("session", 0.4, timedelta(hours=1), "5h 0% 1h00m"),
        ("session", 85.0, None, "5h 85%"),
        ("session", 85.0, timedelta(hours=-1), "5h 85% 0m"),
    ],
)
def test_format_limit_label(kind, utilization, remaining, expected):
    window = claude.Window(utilization=utilization, resets_at=None if remaining is None else NOW + remaining)

    assert watch.format_limit_label(kind, window, NOW) == expected


def test_render_limits_picks_the_requested_window():
    session = watch.render_limits(usage(90, 40), NOW, "session")
    weekly = watch.render_limits(usage(90, 40), NOW, "weekly")

    assert session.view == bar.LimitView("5h 90% 1h00m", 90)
    assert weekly.view == bar.LimitView("wk 40% 3d", 40)
    assert weekly.usage == usage(90, 40)


def label_of(body):
    return next(element["text"] for element in body["elements"] if element["id"] == "limit-label")


@responses.activate
@pytest.mark.parametrize(("kind", "prefix"), [("session", "5h 3%"), ("weekly", "wk 26%")])
def test_limits_tick_draws_the_selected_kind_on_the_first_poll(config, bar_transport, claude_token, kind, prefix):
    responses.get(USAGE_URL, json=usage_body())
    bar_transport.add("POST", DRAW_PATH)

    result = watch.limits_tick(config, None, kind=kind, now=NOW)

    assert result.screen is not None
    assert result.screen.view.label.startswith(prefix)
    [body] = draw_bodies(bar_transport)
    assert label_of(body).startswith(prefix)


@responses.activate
def test_limits_tick_reuse_usage_does_not_fetch(config, bar_transport, claude_token):
    responses.get(USAGE_URL, json=usage_body())
    bar_transport.add("POST", DRAW_PATH)
    first = watch.limits_tick(config, None, now=NOW)

    second = watch.limits_tick(config, first.screen, kind="weekly", reuse_usage=True, now=NOW)

    assert len(responses.calls) == 1
    assert len(claude_token) == 1
    assert second.screen is not None
    assert second.screen.view.label.startswith("wk ")
    assert len(draw_bodies(bar_transport)) == 2


@responses.activate
def test_limits_tick_reuse_usage_without_a_previous_screen_fetches(config, bar_transport, claude_token):
    responses.get(USAGE_URL, json=usage_body())
    bar_transport.add("POST", DRAW_PATH)

    result = watch.limits_tick(config, None, kind="weekly", reuse_usage=True, now=NOW)

    assert len(responses.calls) == 1
    assert result.screen is not None


@responses.activate
def test_limits_tick_skips_the_draw_when_nothing_changed(config, bar_transport, claude_token):
    responses.get(USAGE_URL, json=usage_body())
    bar_transport.add("POST", DRAW_PATH)

    first = watch.limits_tick(config, None, now=NOW)
    second = watch.limits_tick(config, first.screen, now=NOW)

    assert second.screen == first.screen
    assert len(draw_bodies(bar_transport)) == 1


@responses.activate
def test_limits_tick_swallows_a_transient_failure_and_keeps_the_previous_screen(config, bar_transport, claude_token):
    responses.get(USAGE_URL, status=500)
    previous = watch.LimitsScreen(view=bar.LimitView("5h 1%", 1), usage=usage(1, 2))

    result = watch.limits_tick(config, previous, now=NOW)

    assert result.screen == previous
    assert bar_transport.calls == []


@responses.activate
def test_limits_tick_carries_retry_after(config, bar_transport, claude_token):
    responses.get(USAGE_URL, status=429, headers={"Retry-After": "60"})

    assert watch.limits_tick(config, None, now=NOW).retry_after == 60.0


@responses.activate
def test_limits_tick_does_not_swallow_a_rejected_token(config, bar_transport, claude_token):
    responses.get(USAGE_URL, status=401)

    with pytest.raises(exceptions.ClaudeAuthError):
        watch.limits_tick(config, None, now=NOW)


def test_limits_tick_does_not_swallow_a_missing_credential(config, bar_transport, monkeypatch):
    def expired():
        raise exceptions.ClaudeAuthError("expired")

    monkeypatch.setattr(watch.claude, "resolve_token", expired)

    with pytest.raises(exceptions.ClaudeAuthError):
        watch.limits_tick(config, None, now=NOW)


@responses.activate
def test_limits_tick_swallows_a_bar_failure_and_keeps_the_previous_screen(config, bar_transport, claude_token):
    responses.get(USAGE_URL, json=usage_body())
    bar_transport.add("POST", DRAW_PATH, status=401)

    result = watch.limits_tick(config, None, now=NOW)

    assert result.screen is None


@responses.activate
def test_limits_tick_resolves_the_token_on_every_call(config, bar_transport, claude_token):
    responses.get(USAGE_URL, json=usage_body())
    bar_transport.add("POST", DRAW_PATH)

    first = watch.limits_tick(config, None, now=NOW)
    watch.limits_tick(config, first.screen, now=NOW)

    assert len(claude_token) == 2


@responses.activate
def test_watch_limits_clears_on_start_and_on_interrupt(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch_limits(config, interval=180, sleep=sleep)

    methods = [call.method for call in bar_transport.calls if call.url.path == DRAW_PATH]
    assert methods == ["DELETE", "POST", "DELETE"]
    assert not [call for call in bar_transport.calls if call.url.path == UPLOAD_PATH]


@responses.activate
def test_watch_limits_waits_at_least_retry_after(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    responses.get(USAGE_URL, status=429, headers={"Retry-After": "600"})
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        raise KeyboardInterrupt

    watch.watch_limits(config, interval=180, sleep=sleep)

    assert slept == [600.0]


@responses.activate
def test_watch_limits_ends_on_a_rejected_token_after_clearing(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    responses.get(USAGE_URL, status=401)

    with pytest.raises(exceptions.ClaudeAuthError):
        watch.watch_limits(config, interval=180, sleep=lambda seconds: None)

    assert [call.method for call in bar_transport.calls] == ["DELETE", "DELETE"]


@responses.activate
def test_limits_tick_does_not_redraw_when_only_invisible_values_changed(config, bar_transport, claude_token):
    """Sub-integer percent drift and seconds inside the same minute change no lit pixel and no character."""
    responses.get(USAGE_URL, json=usage_body(session=10.0))
    bar_transport.add("POST", DRAW_PATH)
    first = watch.limits_tick(config, None, now=NOW - timedelta(seconds=30))
    responses.replace(responses.GET, USAGE_URL, json=usage_body(session=10.2))

    watch.limits_tick(config, first.screen, now=NOW)

    assert len(draw_bodies(bar_transport)) == 1


class FakeListener:
    """Stands in for bar.StartButtonListener: scripted `wait_for_press` results, then Ctrl+C."""

    def __init__(self, *results):
        self.results = list(results)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def wait_for_press(self, timeout):
        if not self.results:
            raise KeyboardInterrupt
        return self.results.pop(0)


def labels_drawn(bar_transport):
    return [label_of(body) for body in draw_bodies(bar_transport)]


def clock(monkeypatch, *readings):
    """Script `time.monotonic` for the watch: the given readings, then the last one forever."""
    values = itertools.chain(readings, itertools.repeat(readings[-1]))
    monkeypatch.setattr(watch.time, "monotonic", lambda: next(values))


@responses.activate
def test_a_press_switches_to_weekly_and_redraws_without_fetching(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())

    watch.watch_limits(config, interval=180, listener=FakeListener(True))

    assert len(responses.calls) == 1
    labels = labels_drawn(bar_transport)
    assert [label.split()[0] for label in labels] == ["5h", "wk"]


@responses.activate
def test_a_second_press_returns_to_the_five_hour_view(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())

    watch.watch_limits(config, interval=180, listener=FakeListener(True, True))

    assert [label.split()[0] for label in labels_drawn(bar_transport)] == ["5h", "wk", "5h"]
    assert len(responses.calls) == 1


@responses.activate
def test_a_press_after_the_interval_has_elapsed_fetches_again(config, bar_transport, claude_token, monkeypatch):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())
    clock(monkeypatch, 0, 1000)

    watch.watch_limits(config, interval=180, listener=FakeListener(True))

    assert len(responses.calls) == 2
    assert [label.split()[0] for label in labels_drawn(bar_transport)] == ["5h", "wk"]


@responses.activate
def test_a_timeout_poll_with_unchanged_data_does_not_redraw(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())

    watch.watch_limits(config, interval=180, listener=FakeListener(False))

    assert len(responses.calls) == 2
    assert len(draw_bodies(bar_transport)) == 1


@responses.activate
def test_a_press_after_a_transient_failure_keeps_the_display_and_does_not_raise(
    config, bar_transport, claude_token, monkeypatch
):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())
    responses.get(USAGE_URL, status=500)
    clock(monkeypatch, 0, 1000)

    watch.watch_limits(config, interval=180, listener=FakeListener(True))

    assert len(draw_bodies(bar_transport)) == 1
    assert bar_transport.calls[-1].method == "DELETE"


@responses.activate
def test_a_press_before_the_first_successful_fetch_fetches(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, status=500)
    responses.get(USAGE_URL, json=usage_body())

    watch.watch_limits(config, interval=180, listener=FakeListener(True))

    assert len(responses.calls) == 2
    assert [label.split()[0] for label in labels_drawn(bar_transport)] == ["wk"]


@responses.activate
def test_the_listener_is_started_and_stopped_even_on_interrupt(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())
    listener = FakeListener()

    watch.watch_limits(config, interval=180, listener=listener)

    assert listener.started
    assert listener.stopped


@responses.activate
def test_the_listener_is_stopped_when_a_rejected_token_ends_the_watch(config, bar_transport, claude_token):
    bar_transport.add("DELETE", DRAW_PATH)
    responses.get(USAGE_URL, status=401)
    listener = FakeListener()

    with pytest.raises(exceptions.ClaudeAuthError):
        watch.watch_limits(config, interval=180, listener=listener)

    assert listener.stopped


@responses.activate
def test_an_injected_sleep_runs_without_creating_a_listener(config, bar_transport, claude_token, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a listener must not be created when sleep is injected")

    monkeypatch.setattr(watch.bar, "StartButtonListener", forbidden)
    bar_transport.add("DELETE", DRAW_PATH)
    bar_transport.add("POST", DRAW_PATH)
    responses.get(USAGE_URL, json=usage_body())

    def sleep(seconds):
        raise KeyboardInterrupt

    watch.watch_limits(config, interval=180, sleep=sleep)

    assert len(draw_bodies(bar_transport)) == 1
