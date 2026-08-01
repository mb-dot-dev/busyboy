"""Command-line entry points for busyboy."""

from collections.abc import Callable
import functools
import logging
from typing import Any, cast

import click
from pydantic import ValidationError

from busyboy import bar, exceptions, git, github, watch
from busyboy.config import ConfigError, load_config


def _configure_logging(*, verbose: bool) -> None:
    """
    Set up logging for one invocation.

    --verbose raises the level to DEBUG, which is what turns on urllib3's own
    per-connection request logging (the standard way to see requests traffic).
    Non-verbose runs need no configuration: requests/urllib3 doesn't log HTTP
    error responses at error level, so there's no duplicate message to silence.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)


def _handle_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Turn expected failures into a one-line message and exit code 1.

    Under --verbose the original exception propagates so the traceback is
    available. Anything not listed here is a bug, not a user error, and is left
    to propagate as well.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        verbose = bool(kwargs.get("verbose"))
        try:
            return func(*args, **kwargs)
        except ConfigError as error:
            if verbose:
                raise
            raise click.ClickException(str(error)) from error
        except ValidationError as error:
            if verbose:
                raise
            details = "; ".join(
                f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}" for detail in error.errors()
            )
            raise click.ClickException(f"Invalid value: {details}") from error
        except exceptions.BusyboyError as error:
            if verbose:
                raise
            message = exceptions.format_delivery_error(error)
            raise click.ClickException(message) from error

    return wrapper


def _connection_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Attach the options every subcommand shares."""
    func = click.option(
        "--verbose",
        is_flag=True,
        help="Log requests and show tracebacks.",
    )(func)
    func = click.option(
        "--token",
        default=None,
        help="BUSY Bar API token. Overrides BUSYBOY_TOKEN.",
    )(func)
    func = click.option(
        "--host",
        default=None,
        help="BUSY Bar hostname or IP. Overrides BUSYBOY_HOST.",
    )(func)
    return func


@click.group()
@click.version_option()
def main() -> None:
    """Display information on a BUSY Bar."""


@main.command()
@click.argument("text")
@click.option(
    "--font",
    type=click.Choice(bar.FONT_NAMES),
    default=bar.DEFAULT_FONT,
    show_default=True,
    help="Font to render the text in.",
)
@click.option(
    "--color",
    default=None,
    help="CSS color name or hex value, e.g. red or #FF0000.",
)
@click.option(
    "--timeout",
    type=int,
    default=None,
    help="Seconds before the text disappears. Persists when unset.",
)
@click.option(
    "--scroll-rate",
    type=int,
    default=bar.DEFAULT_SCROLL_RATE,
    show_default=True,
    help="Scroll speed in pixels per minute for text wider than the display. 0 disables scrolling.",
)
@_connection_options
@_handle_errors
def text(
    text: str,
    font: str,
    color: str | None,
    timeout: int | None,
    scroll_rate: int,
    host: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """Show TEXT on the front display."""
    _configure_logging(verbose=verbose)
    config = load_config(host=host, token=token)
    payload = bar.build_text_payload(
        text,
        font=cast(bar.DisplayFontName, font),
        color=color,
        timeout=timeout,
        scroll_rate=scroll_rate,
    )
    bar.draw_text(config, payload)


@main.command()
@_connection_options
@_handle_errors
def clear(
    host: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """Remove what busyboy drew from the display."""
    _configure_logging(verbose=verbose)
    config = load_config(host=host, token=token)
    bar.clear(config)


def _parse_repo(
    context: click.Context,
    parameter: click.Parameter,
    value: str | None,
) -> github.Repo | None:
    """
    Split an explicit --repo into owner and name, or pass None through.

    `context` and `parameter` go unused; Click passes all three to every
    parameter callback.

    This is a Click parameter callback rather than a plain helper so it runs
    while Click parses the command line, before the body resolves a GitHub
    token. A malformed option is a usage error whatever the environment; if it
    were validated in the body instead, a developer with no gh login would get
    exit 1 about a missing token rather than exit 2 about the option they
    actually got wrong.
    """
    if value is None:
        return None
    owner, separator, name = value.partition("/")
    if not (owner and separator and name) or "/" in name:
        raise click.BadParameter("expected owner/name", param_hint="--repo")
    return github.Repo(owner=owner, name=name)


@main.group()
def gh() -> None:
    """Show GitHub information on the bar."""


@gh.command()
@click.argument("workflow_reference", metavar="WORKFLOW")
@click.option(
    "--branch",
    default=None,
    help="Branch to watch. Defaults to the current checkout's branch.",
)
@click.option(
    "--repo",
    "repo_option",
    default=None,
    callback=_parse_repo,
    help="Repository as owner/name. Defaults to origin's.",
)
@click.option(
    "--interval",
    type=click.IntRange(min=1),
    default=watch.DEFAULT_INTERVAL_SECONDS,
    show_default=True,
    help="Seconds between polls.",
)
@_connection_options
@_handle_errors
def workflow(
    workflow_reference: str,
    branch: str | None,
    repo_option: github.Repo | None,
    interval: int,
    host: str | None,
    token: str | None,
    verbose: bool,
) -> None:
    """
    Watch a GitHub Actions workflow on the bar until Ctrl+C.

    WORKFLOW is a workflow id, filename, or display name.
    """
    _configure_logging(verbose=verbose)
    config = load_config(host=host, token=token)
    github_token = github.resolve_token()
    if repo_option is not None:
        repo = repo_option
    else:
        owner, name = git.origin_repo()
        repo = github.Repo(owner=owner, name=name)
    target = watch.Target(
        repo=repo,
        branch=branch or git.current_branch(),
        workflow_id=github.resolve_workflow(github_token, repo, workflow_reference).id,
    )
    watch.watch(config, github_token, target, interval=interval)
