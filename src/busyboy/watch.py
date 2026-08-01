"""The workflow poll loop: fetch, translate to a display state, draw when it changes."""

from collections.abc import Callable
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


@dataclasses.dataclass(frozen=True)
class TickResult:
    """
    The outcome of one poll cycle.

    `retry_after` is None on the ordinary path, meaning the caller should wait
    the usual interval. It carries a value only when GitHub rate-limited the
    request and told busyboy how many seconds to back off — see
    `exceptions.GitHubTransientError.retry_after`. watch.py parses no HTTP
    headers itself; it only reads this already-decoded value.
    """

    screen: Screen | None
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
) -> TickResult:
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
        run = github.latest_run(token, target.repo, target.workflow_id, target.branch)
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
    pause = sleep if sleep is not None else time.sleep
    bar.clear(config)
    bar.upload_icons(config)
    screen: Screen | None = None
    try:
        while True:
            result = tick(config, token, target, screen)
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
