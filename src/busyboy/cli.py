"""Command-line entry points for busyboy."""

from collections.abc import Callable
import functools
from importlib import metadata
import logging
from typing import Annotated, Any, NoReturn

from pydantic import ValidationError
import typer

from busyboy import bar, exceptions, git, github, watch
from busyboy.config import ConfigError, load_config

# Shared by every subcommand. Typer declares parameters from the signature rather than from stacked
# decorators, so the three connection options are reusable annotations instead of a decorator factory.
HostOption = Annotated[str | None, typer.Option("--host", help="BUSY Bar hostname or IP. Overrides BUSYBOY_HOST.")]
TokenOption = Annotated[str | None, typer.Option("--token", help="BUSY Bar API token. Overrides BUSYBOY_TOKEN.")]
VerboseOption = Annotated[bool, typer.Option("--verbose", help="Log requests and show tracebacks.")]


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


def _fail(message: str) -> NoReturn:
    """Print one line to stderr and exit 1, the way an expected failure ends."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _handle_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Turn expected failures into a one-line message and exit code 1.

    Under --verbose the original exception propagates so the traceback is
    available. Anything not listed here is a bug, not a user error, and is left
    to propagate as well.

    functools.wraps copies __annotations__ along with the rest, which is what
    lets Typer read the wrapped command's signature through this decorator and
    still build its parameters from it.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        verbose = bool(kwargs.get("verbose"))
        try:
            return func(*args, **kwargs)
        except ConfigError as error:
            if verbose:
                raise
            _fail(str(error))
        except ValidationError as error:
            if verbose:
                raise
            details = "; ".join(
                f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}" for detail in error.errors()
            )
            _fail(f"Invalid value: {details}")
        except exceptions.BusyboyError as error:
            if verbose:
                raise
            _fail(exceptions.format_delivery_error(error))

    return wrapper


main = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
gh = typer.Typer(no_args_is_help=True, help="Show GitHub information on the bar.")
main.add_typer(gh, name="gh")


def _show_version(value: bool) -> None:
    """Print the installed version and exit, before any other parameter is processed."""
    if value:
        typer.echo(f"busyboy, version {metadata.version('busyboy')}")
        raise typer.Exit


@main.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_show_version, is_eager=True, help="Show the version and exit."),
    ] = False,
) -> None:
    """Display information on a BUSY Bar."""


@main.command()
@_handle_errors
def text(
    text: Annotated[str, typer.Argument(metavar="TEXT", help="Text to show on the front display.")],
    font: Annotated[bar.DisplayFontName, typer.Option(help="Font to render the text in.")] = bar.DEFAULT_FONT,
    color: Annotated[str | None, typer.Option(help="CSS color name or hex value, e.g. red or #FF0000.")] = None,
    timeout: Annotated[
        int | None, typer.Option(help="Seconds before the text disappears. Persists when unset.")
    ] = None,
    scroll_rate: Annotated[
        int,
        typer.Option(help="Scroll speed in pixels per minute for text wider than the display. 0 disables scrolling."),
    ] = bar.DEFAULT_SCROLL_RATE,
    host: HostOption = None,
    token: TokenOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Show TEXT on the front display."""
    _configure_logging(verbose=verbose)
    config = load_config(host=host, token=token)
    payload = bar.build_text_payload(
        text,
        font=font,
        color=color,
        timeout=timeout,
        scroll_rate=scroll_rate,
    )
    bar.draw_text(config, payload)


@main.command()
@_handle_errors
def clear(
    host: HostOption = None,
    token: TokenOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Remove what busyboy drew from the display."""
    _configure_logging(verbose=verbose)
    config = load_config(host=host, token=token)
    bar.clear(config)


def _parse_repo(value: str) -> github.Repo:
    """
    Split an explicit --repo into owner and name.

    This is the option's parser — the callable Typer turns into the parameter's
    click type — rather than a plain helper called from the command body, so it
    runs while the command line is parsed, before the body resolves a GitHub
    token. A malformed option is a usage error whatever the environment; if it
    were validated in the body instead, a developer with no gh login would get
    exit 1 about a missing token rather than exit 2 about the option they
    actually got wrong.

    An omitted --repo never reaches here: parameters skip type conversion when
    their value is None, so the default passes straight through.
    """
    owner, separator, name = value.partition("/")
    if not (owner and separator and name) or "/" in name:
        raise typer.BadParameter("expected owner/name", param_hint="--repo")
    return github.Repo(owner=owner, name=name)


@gh.command()
@_handle_errors
def workflow(
    workflow_reference: Annotated[
        str,
        typer.Argument(metavar="WORKFLOW", help="Workflow id, filename, or display name."),
    ],
    branch: Annotated[
        str | None,
        typer.Option(help="Branch to watch. Defaults to the current checkout's branch."),
    ] = None,
    repo_option: Annotated[
        github.Repo | None,
        typer.Option(
            "--repo",
            parser=_parse_repo,
            metavar="OWNER/NAME",
            help="Repository as owner/name. Defaults to origin's.",
        ),
    ] = None,
    interval: Annotated[int, typer.Option(min=1, help="Seconds between polls.")] = watch.DEFAULT_INTERVAL_SECONDS,
    host: HostOption = None,
    token: TokenOption = None,
    verbose: VerboseOption = False,
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
        workflow=github.resolve_workflow(github_token, repo, workflow_reference),
    )
    watch.watch(config, github_token, target, interval=interval)
