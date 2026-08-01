"""The workflow poll loop: fetch, translate to a display state, draw when it changes."""

import dataclasses
import logging

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
) -> Screen | None:
    """
    Run one poll cycle, returning the state now on the bar.

    Transient GitHub failures and bar delivery failures leave the display
    untouched and return `previous`: a watch process is expected to outlive a
    laptop sleeping or a wifi hiccup. Auth failures are not caught here — they
    never self-heal, so they propagate and end the watch.
    """
    try:
        run = github.latest_run(token, target.repo, target.workflow_id, target.branch)
        pull_request = github.pull_request_number(token, target.repo, target.branch)
    except exceptions.GitHubTransientError as error:
        LOGGER.debug("GitHub request failed, keeping the display as it is: %s", error)
        return previous

    screen = render(target, run, pull_request)
    if screen == previous:
        return previous

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
        return previous
    return screen
