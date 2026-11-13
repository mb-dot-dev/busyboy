"""The poll loops: fetch, translate to a display state, draw when it changes."""

from collections.abc import Callable
import dataclasses
from datetime import UTC, datetime, timedelta
import logging
import time
from typing import Literal

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
    the usual interval. It carries a value only when the data source (GitHub or
    Claude) rate-limited the request and told busyboy how many seconds to back
    off — see `exceptions.GitHubTransientError.retry_after` and
    `exceptions.ClaudeTransientError.retry_after`. watch.py parses no HTTP
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


type LimitKind = Literal["session", "weekly"]


@dataclasses.dataclass(frozen=True)
class LimitsScreen:
    """
    What the limits display shows, plus the usage it came from.

    `view` is what gets drawn; `usage` is kept so a Start-button press can redraw the other limit
    without another API call.
    """

    view: bar.LimitView
    usage: claude.Usage


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


def format_weekly_label_time(remaining: timedelta) -> str:
    """
    Format the time to the weekly reset for the bar label: `6d` from 48h, whole hours (`47h`) under that, `59m` under an hour.

    The countdown's `1d 23h` and `23h59m` forms make `wk 100% ...` wider than the 72px display in the label font
    (measured on a real bar), and a weekly reset does not need that precision on a bar.
    """
    if remaining >= timedelta(hours=48):
        return format_weekly_countdown(remaining)
    hours, _ = _clock(remaining)
    return f"{hours}h" if hours else format_session_countdown(remaining)


def format_limit_label(kind: LimitKind, window: claude.Window, now: datetime) -> str:
    """
    Format the short bar label: `5h 85% 3h49m`, `wk 40% 6d`; the time is left off without a reset time.

    The percent is rounded and clamped to 0-100 so the label never outgrows the display.
    """
    name, fmt = ("5h", format_session_countdown) if kind == "session" else ("wk", format_weekly_label_time)
    label = f"{name} {round(min(max(window.utilization, 0), 100))}%"
    return label if window.resets_at is None else f"{label} {fmt(window.resets_at - now)}"


def render_limits(usage: claude.Usage, now: datetime, kind: LimitKind) -> LimitsScreen:
    """Turn fetched usage into the one limit being shown."""
    window = usage.session if kind == "session" else usage.weekly
    return LimitsScreen(view=bar.LimitView(format_limit_label(kind, window, now), window.utilization), usage=usage)


def limits_tick(
    config: BusyboyConfig,
    previous: LimitsScreen | None,
    *,
    kind: LimitKind = "session",
    reuse_usage: bool = False,
    now: datetime | None = None,
) -> TickResult[LimitsScreen]:
    """
    Run one limits poll cycle for the selected limit, returning the state now on the bar.

    With `reuse_usage` and a previous screen, the usage that screen carries is rendered again instead of
    fetching: that is how a Start-button press redraws immediately. Without a previous screen there is
    nothing to reuse, so it fetches regardless.

    The Claude token is re-resolved on every fetch: Claude Code refreshes its own token in the keychain,
    so a token read once at startup would go stale in a long watch. Transient Claude failures and bar
    delivery failures leave the display untouched. ClaudeAuthError (and any other ClaudeError) is
    deliberately not caught: a rejected or expired token never self-heals, so it ends the watch.
    """
    if reuse_usage and previous is not None:
        usage = previous.usage
    else:
        try:
            usage = claude.fetch_usage(claude.resolve_token())
        except exceptions.ClaudeTransientError as error:
            LOGGER.debug("Claude usage request failed, keeping the display as it is: %s", error)
            return TickResult(screen=previous, retry_after=error.retry_after)

    screen = render_limits(usage, now or datetime.now(UTC), kind)
    payload = bar.build_limits_payload(screen.view)
    # Compare what would be drawn, not the raw screens: the usage and the unrounded percent differ on almost
    # every poll without changing a single lit pixel or character.
    if previous is not None and bar.build_limits_payload(previous.view) == payload:
        return TickResult(screen=screen)

    try:
        bar.draw_text(config, payload)
    except exceptions.BarError as error:
        LOGGER.debug("Draw failed, keeping the display as it is: %s", error)
        return TickResult(screen=previous)
    return TickResult(screen=screen)


def watch_limits(
    config: BusyboyConfig,
    *,
    interval: int = DEFAULT_LIMITS_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
    listener: bar.ButtonListener | None = None,
) -> None:
    """
    Poll Claude's usage limits until interrupted, then clear the display.

    One limit is shown at a time, the 5-hour one first. Each Start-button press on the bar switches to the
    other and redraws at once from the usage already fetched. A press never fetches and never shortens the
    wait: the wait is a deadline that presses are handled inside of, so the fetch cadence and any Retry-After
    back-off are exactly what they would be without the button.

    Like `watch`, it clears first (so nothing left by another busyboy invocation sits under the display)
    and clears again on any exit. There are no icons to upload.

    `sleep`, when given, replaces the wait entirely (tests drive the loop with it) and no button listener is
    created, so a press can never happen. Otherwise the wait uses the listener's `wait_for_press`.
    `listener` is resolved here so tests can inject a fake.
    """
    active = None if sleep is not None else listener if listener is not None else bar.StartButtonListener(config)
    kind: LimitKind = "session"
    shown: LimitsScreen | None = None

    def step(_previous: LimitsScreen | None) -> TickResult[LimitsScreen]:
        nonlocal shown
        result = limits_tick(config, shown, kind=kind)
        shown = result.screen
        return result

    def pause(seconds: float) -> None:
        nonlocal kind, shown
        if active is None:
            assert sleep is not None  # narrows the type: `active` is None only when `sleep` is given
            sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0 and active.wait_for_press(remaining):
            kind = "weekly" if kind == "session" else "session"
            if shown is not None:
                # `limits_tick` keeps the previous screen if the draw fails, so `shown` always matches the bar.
                shown = limits_tick(config, shown, kind=kind, reuse_usage=True).screen

    if active is not None:
        active.start()
    try:
        _poll(config, step, interval=interval, sleep=pause)
    finally:
        if active is not None:
            active.stop()
