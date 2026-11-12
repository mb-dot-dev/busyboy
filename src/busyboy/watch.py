"""The poll loops: fetch, translate to a display state, draw when it changes."""

from collections.abc import Callable
import dataclasses
from datetime import UTC, datetime, timedelta
import logging
import time

from busyboy import bar, claude, exceptions, github
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
    """
    What is being watched. Resolved once at startup; none of it changes mid-watch.

    The whole `github.Workflow` is held rather than just its id, so the id used
    to fetch runs and the name shown on the display can only ever come from the
    same `resolve_workflow` call and cannot drift apart.
    """

    repo: github.Repo
    branch: str
    workflow: github.Workflow


@dataclasses.dataclass(frozen=True)
class Screen:
    """Exactly what is on the bar. Comparing two of these decides whether to redraw."""

    repo_label: str
    ref_label: str
    icon: bar.IconName


@dataclasses.dataclass(frozen=True)
class TickResult[ScreenT]:
    """
    The outcome of one poll cycle, generic over the kind of screen being kept up to date.

    `retry_after` is None on the ordinary path, meaning the caller should wait
    the usual interval. It carries a value only when GitHub rate-limited the
    request and told busyboy how many seconds to back off — see
    `exceptions.GitHubTransientError.retry_after`. watch.py parses no HTTP
    headers itself; it only reads this already-decoded value.
    """

    screen: ScreenT | None
    retry_after: float | None = None


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
    """
    Turn a fetched run into the three things the display shows.

    The workflow name is stripped rather than sanitized in place, and dropped
    entirely when nothing displayable survives — the ref is always present, so
    the row cannot end up empty either way. Neither part is ASCII-sanitized
    here; `bar._row` does that over the whole composed label. See
    `bar.strip_undisplayable`.
    """
    ref = f"#{pull_request}" if pull_request is not None else target.branch
    name = bar.strip_undisplayable(target.workflow.name)
    return Screen(
        repo_label=target.repo.slug,
        ref_label=f"{ref} {name}" if name else ref,
        icon=icon_for(run),
    )


def tick(
    config: BusyboyConfig,
    token: str,
    target: Target,
    previous: Screen | None,
) -> TickResult[Screen]:
    """
    Run one poll cycle, returning the state now on the bar and how long to wait before the next one.

    Transient GitHub failures and bar delivery failures leave the display
    untouched and return `previous`: a watch process is expected to outlive a
    laptop sleeping or a wifi hiccup. Auth failures are not caught here — they
    never self-heal, so they propagate and end the watch.

    A rate-limited GitHub response carries its `retry_after` through in the
    result; every other path leaves it None, meaning "use the normal interval".
    """
    try:
        run = github.latest_run(token, target.repo, target.workflow.id, target.branch)
        pull_request = github.pull_request_number(token, target.repo, target.branch)
    except exceptions.GitHubTransientError as error:
        LOGGER.debug("GitHub request failed, keeping the display as it is: %s", error)
        return TickResult(screen=previous, retry_after=error.retry_after)

    screen = render(target, run, pull_request)
    if screen == previous:
        return TickResult(screen=previous)

    try:
        bar.draw_text(
            config,
            bar.build_workflow_payload(
                repo_label=screen.repo_label,
                ref_label=screen.ref_label,
                icon=screen.icon,
            ),
        )
    except exceptions.BarError as error:
        LOGGER.debug("Draw failed, keeping the display as it is: %s", error)
        return TickResult(screen=previous)
    return TickResult(screen=screen)


def watch(
    config: BusyboyConfig,
    token: str,
    target: Target,
    *,
    interval: int = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """
    Poll until interrupted, then clear the display.

    `sleep` is injected so tests can drive the loop without waiting, and can
    end it by raising KeyboardInterrupt the way Ctrl+C does. It defaults to
    None rather than to time.sleep directly: a default bound at definition
    time cannot be monkeypatched, and the CLI tests patch time.sleep.

    The display is cleared once before the loop starts, not just on the way
    out: draw replaces elements by id, it does not remove ones absent from the
    payload, so any element left over from another busyboy invocation (e.g. a
    persistent `busyboy text`) would otherwise sit on the panel, overlapping
    the workflow layout, for the whole watch. This clear is scoped to
    busyboy's own application_name (see bar.clear), so it cannot disturb
    other applications' elements, and it is unconditional like upload_icons
    right after it — if the bar cannot be reached before the loop even starts,
    failing fast beats limping into a poll loop that can never draw.

    The display is cleared again on the way out however the loop ends,
    including on a fatal error: leaving a stale workflow status on the bar
    after the process is gone would be worse than showing nothing.
    """
    _poll(
        config,
        lambda previous: tick(config, token, target, previous),
        interval=interval,
        sleep=sleep,
        before_loop=lambda: bar.upload_icons(config),
    )


def _poll[ScreenT](
    config: BusyboyConfig,
    step: Callable[[ScreenT | None], TickResult[ScreenT]],
    *,
    interval: int,
    sleep: Callable[[float], None] | None,
    before_loop: Callable[[], None] | None = None,
) -> None:
    """
    Clear, run `before_loop`, then call `step` every interval until interrupted, clearing again on the way out.

    Shared by both watches so the clear-first / clear-last guarantees, the Retry-After
    back-off, and the Ctrl+C handling live in exactly one place. `sleep` is resolved
    here, at call time, so tests that monkeypatch `time.sleep` still take effect.
    """
    pause = sleep if sleep is not None else time.sleep
    bar.clear(config)
    if before_loop is not None:
        before_loop()
    screen: ScreenT | None = None
    try:
        while True:
            result = step(screen)
            screen = result.screen
            wait = interval if result.retry_after is None else max(interval, result.retry_after)
            pause(wait)
    except KeyboardInterrupt:
        LOGGER.debug("Interrupted, clearing the display")
    finally:
        try:
            bar.clear(config)
        except exceptions.BarError as error:
            # Never let cleanup mask why the loop actually ended.
            LOGGER.debug("Could not clear the display on exit: %s", error)


DEFAULT_LIMITS_INTERVAL_SECONDS = 180


@dataclasses.dataclass(frozen=True)
class LimitsScreen:
    """Exactly what the limits bars show. Comparing two of these decides whether to redraw."""

    session: bar.LimitRow
    weekly: bar.LimitRow


def _clock(remaining: timedelta) -> tuple[int, int]:
    """Whole hours and minutes left, floored, never negative."""
    minutes = max(int(remaining.total_seconds() // 60), 0)
    return divmod(minutes, 60)


def format_session_countdown(remaining: timedelta) -> str:
    """Format the time to the 5-hour reset: `3h59m`, `3h05m`, or `59m`; `0m` once past."""
    hours, minutes = _clock(remaining)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def format_weekly_countdown(remaining: timedelta) -> str:
    """Format the time to the weekly reset: `6d` from 48h, `1d 5h` from 24h, else as the session form."""
    if remaining >= timedelta(hours=48):
        return f"{remaining.days}d"
    if remaining >= timedelta(hours=24):
        hours, _ = _clock(remaining)
        return f"{hours // 24}d {hours % 24}h"
    return format_session_countdown(remaining)


def render_limits(usage: claude.Usage, now: datetime) -> LimitsScreen:
    """
    Turn fetched usage into the two bar rows.

    The countdown is always computed when a reset time exists; whether it is shown is
    `bar`'s decision (it only draws above the threshold). A missing reset time means no
    countdown rather than an error.
    """

    def row(window: claude.Window, fmt: Callable[[timedelta], str]) -> bar.LimitRow:
        countdown = None if window.resets_at is None else fmt(window.resets_at - now)
        return bar.LimitRow(window.utilization, countdown)

    return LimitsScreen(
        session=row(usage.session, format_session_countdown),
        weekly=row(usage.weekly, format_weekly_countdown),
    )


def limits_tick(
    config: BusyboyConfig,
    previous: LimitsScreen | None,
    *,
    now: datetime | None = None,
) -> TickResult[LimitsScreen]:
    """
    Run one limits poll cycle, returning the state now on the bar.

    The Claude token is re-resolved every call: Claude Code refreshes its own token in
    the keychain, so a token read once at startup would go stale in a long watch.
    Transient Claude failures and bar delivery failures leave the display untouched.
    ClaudeAuthError (and any other ClaudeError) is deliberately not caught: a rejected
    or expired token never self-heals, so it ends the watch.
    """
    try:
        usage = claude.fetch_usage(claude.resolve_token())
    except exceptions.ClaudeTransientError as error:
        LOGGER.debug("Claude usage request failed, keeping the display as it is: %s", error)
        return TickResult(screen=previous, retry_after=error.retry_after)

    screen = render_limits(usage, now or datetime.now(UTC))
    if screen == previous:
        return TickResult(screen=previous)

    try:
        bar.draw_text(config, bar.build_limits_payload(session=screen.session, weekly=screen.weekly))
    except exceptions.BarError as error:
        LOGGER.debug("Draw failed, keeping the display as it is: %s", error)
        return TickResult(screen=previous)
    return TickResult(screen=screen)


def watch_limits(
    config: BusyboyConfig,
    *,
    interval: int = DEFAULT_LIMITS_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """
    Poll Claude's usage limits until interrupted, then clear the display.

    Like `watch`, it clears first (so nothing left by another busyboy invocation sits under
    the bars) and clears again on any exit. There are no icons to upload.
    """
    _poll(config, lambda previous: limits_tick(config, previous), interval=interval, sleep=sleep)
