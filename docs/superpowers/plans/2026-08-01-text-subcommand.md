# busyboy `text` Subcommand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `busyboy text "..."` to draw a short string on the BUSY Bar's 72x16 front display, and `busyboy clear` to remove it.

**Architecture:** Split the current single-module CLI into three focused modules — `config.py` resolves connection settings from flags and environment, `bar.py` builds and delivers BUSY Bar payloads, `cli.py` holds the Click group and exit-code policy. Each is testable without the others' concerns: config against a dict of env vars, bar against an `httpx.MockTransport`, cli against Click's `CliRunner`.

**Tech Stack:** Python 3.14, Click 8.4, pydantic 2.13 + pydantic-settings, busylib (BUSY Bar HTTP client), pytest, ruff, ty, uv.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-01-text-subcommand-design.md`. Branch: `feat/text-subcommand`.
- Python `>=3.14`. All commands go through `uv run --frozen`, or `make` targets that wrap it.
- Ruff: line length **120**, double quotes, PEP 257 docstrings, isort with `force-sort-within-sections = true`. That isort setting sorts `import x` and `from x import y` together by module name, so `from busylib import ...` precedes `import click`, and `from collections.abc import Callable` precedes `import functools`. Write imports in that order or `make format` will move them.
- `make test` (= `ruff check` + `ruff format --check` + `ty check` + `pytest`) must pass before every commit.
- Coverage gate: `fail_under = 75`, branch coverage over `src`.
- Success output is silent; failures print one line to stderr and exit 1; Click usage errors exit 2.
- Never log or print the API token. It is held as `SecretStr` and unwrapped only in `bar.open_client`.

All code in this plan has been run against the real busylib and passes `ruff check`, `ruff format --check`, and `ty check`.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/busyboy/config.py` | Create. `BusyboyConfig` settings model, `load_config`, `ConfigError`. No Click, no busylib. |
| `src/busyboy/bar.py` | Create. Payload construction and delivery. No argv, no environment. |
| `src/busyboy/cli.py` | Create. Click group, `text` and `clear` commands, error-to-exit-code mapping. No payload knowledge. |
| `src/busyboy/__init__.py` | Rewrite. Re-export `main` so `[project.scripts]` keeps working. |
| `tests/test_config.py` | Create. Env/flag resolution. |
| `tests/test_bar.py` | Create. Payload shape plus delivery through `httpx.MockTransport`. |
| `tests/test_cli.py` | Create. Exit codes and stderr through `CliRunner`. |
| `tests/test_main.py` | Delete. Its `main()` call breaks once `main` is a group. |
| `pyproject.toml` | Modify. Add `pydantic` and `pydantic-settings` as direct dependencies. |
| `README.md` | Modify. Usage section. |

---

### Task 1: Configuration resolution

Settings read from `BUSYBOY_HOST` / `BUSYBOY_TOKEN`, overridable by explicit arguments, failing with a message that names both the variable and the flag.

**Files:**
- Modify: `pyproject.toml` (dependencies)
- Create: `src/busyboy/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BusyboyConfig(BaseSettings)` with `host: str` and `token: SecretStr`, frozen.
  - `load_config(*, host: str | None = None, token: str | None = None) -> BusyboyConfig`
  - `ConfigError(Exception)` — raised by `load_config` with a formatted, user-facing message.

- [ ] **Step 1: Add the dependencies**

Both are already present transitively through busylib, but busyboy imports them directly and must declare them.

```bash
uv add pydantic pydantic-settings
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_config.py`:

```python
"""Tests for connection settings resolved from flags and the environment."""

import pytest

from busyboy.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


def test_reads_host_and_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config()

    assert config.host == "10.0.4.20"
    assert config.token.get_secret_value() == "envtoken"


def test_arguments_override_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config(host="192.168.1.5", token="flagtoken")

    assert config.host == "192.168.1.5"
    assert config.token.get_secret_value() == "flagtoken"


def test_a_single_override_leaves_the_rest_to_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config(host="192.168.1.5")

    assert config.host == "192.168.1.5"
    assert config.token.get_secret_value() == "envtoken"


def test_arguments_alone_are_enough():
    config = load_config(host="192.168.1.5", token="flagtoken")

    assert config.host == "192.168.1.5"
    assert config.token.get_secret_value() == "flagtoken"


def test_missing_host_names_both_the_variable_and_the_flag(monkeypatch):
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "BUSYBOY_HOST" in message
    assert "--host" in message


def test_missing_token_names_both_the_variable_and_the_flag(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "BUSYBOY_TOKEN" in message
    assert "--token" in message


def test_both_missing_reports_both():
    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "BUSYBOY_HOST" in message
    assert "BUSYBOY_TOKEN" in message


def test_the_token_stays_out_of_the_repr():
    config = load_config(host="10.0.4.20", token="supersecret")

    assert "supersecret" not in repr(config)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_config.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'busyboy.config'`.

- [ ] **Step 4: Write the implementation**

Create `src/busyboy/config.py`:

```python
"""BUSY Bar connection settings, resolved from CLI flags and the environment."""

from typing import Any

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class BusyboyConfig(BaseSettings):
    """Connection settings read from BUSYBOY_HOST and BUSYBOY_TOKEN."""

    model_config = SettingsConfigDict(env_prefix="BUSYBOY_", frozen=True)

    host: str
    token: SecretStr


class ConfigError(Exception):
    """Raised when required connection settings are missing or malformed."""


def load_config(
    *,
    host: str | None = None,
    token: str | None = None,
) -> BusyboyConfig:
    """
    Build the config, letting explicit values win over the environment.

    Only arguments that were actually supplied are passed through, because
    pydantic-settings ranks init arguments above environment variables.
    """
    # Typed as Any because BaseSettings.__init__ also accepts private
    # keyword arguments; a narrower dict trips the type checker on unpack.
    overrides: dict[str, Any] = {key: value for key, value in (("host", host), ("token", token)) if value is not None}
    try:
        return BusyboyConfig(**overrides)
    except ValidationError as error:
        raise ConfigError(_format_config_error(error)) from error


def _format_config_error(error: ValidationError) -> str:
    """
    Render a config ValidationError naming both the env var and the flag.

    A user hitting this may know about neither, so every missing field is
    reported on its own line.
    """
    lines: list[str] = []
    for detail in error.errors():
        field = str(detail["loc"][0])
        if detail["type"] == "missing":
            lines.append(f"Missing configuration: BUSYBOY_{field.upper()} is not set (or pass --{field})")
        else:
            lines.append(f"Invalid configuration: {field}: {detail['msg']}")
    return "\n".join(lines)
```

The `dict[str, Any]` annotation is not cosmetic. `BaseSettings.__init__` accepts private keyword arguments such as `_case_sensitive: bool | None` and `_env_prefix: str | None`; unpacking a `dict[str, str]` into it makes `ty` report `Expected 'bool | None', found 'str'` and fails `make lint`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_config.py -v`
Expected: 8 passed.

- [ ] **Step 6: Run the full check**

Run: `make test`
Expected: ruff, ty pass. `pytest` still runs the old `tests/test_main.py`, which passes at this point — it is removed in Task 3.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/busyboy/config.py tests/test_config.py
git commit -m "feat: resolve BUSY Bar connection settings from env and flags"
```

---

### Task 2: Payload construction and delivery

Everything BUSY-Bar-shaped: what a text element looks like, and the two calls that send it.

**Files:**
- Create: `src/busyboy/bar.py`
- Test: `tests/test_bar.py`

**Interfaces:**
- Consumes: `BusyboyConfig` and `load_config` from Task 1.
- Produces:
  - `APPLICATION_NAME: str = "busyboy"`, `TEXT_ELEMENT_ID: str = "text"`, `FRONT_DISPLAY_WIDTH: int = 72`
  - `DEFAULT_FONT: types.DisplayFontName = "condensed"`, `FONT_NAMES: tuple[str, ...]`, `DEFAULT_SCROLL_RATE: int`
  - `open_client(config: BusyboyConfig, *, transport: httpx.BaseTransport | None = None) -> BusyBar`
  - `build_text_payload(text: str, *, font: types.DisplayFontName = DEFAULT_FONT, color: str | None = None, timeout: int | None = None, scroll_rate: int = DEFAULT_SCROLL_RATE) -> types.DisplayElements`
  - `draw_text(client: BusyBar, payload: types.DisplayElements) -> None`
  - `clear(client: BusyBar) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bar.py`:

```python
"""Tests for BUSY Bar payload construction and delivery."""

import json

from busylib import exceptions
import httpx
from pydantic import ValidationError
import pytest

from busyboy import bar
from busyboy.config import load_config


@pytest.fixture
def config():
    return load_config(host="10.0.4.20", token="testtoken")


def element_of(payload):
    """Return the single element from a payload, as the wire dict."""
    return payload.model_dump(exclude_none=True)["elements"][0]


def test_payload_carries_one_text_element():
    payload = bar.build_text_payload("BUILD OK").model_dump(exclude_none=True)

    assert payload["application_name"] == "busyboy"
    assert len(payload["elements"]) == 1
    assert payload["elements"][0]["type"] == "text"
    assert payload["elements"][0]["text"] == "BUILD OK"


def test_element_id_is_stable_so_a_redraw_replaces():
    first = element_of(bar.build_text_payload("one"))
    second = element_of(bar.build_text_payload("two"))

    assert first["id"] == second["id"]


def test_defaults_centre_condensed_text_across_the_front_display():
    element = element_of(bar.build_text_payload("hi"))

    assert element["font"] == "condensed"
    assert element["display"] == "front"
    assert element["align"] == "center"
    assert element["width"] == 72
    assert element["scroll_rate"] == bar.DEFAULT_SCROLL_RATE


def test_colour_and_timeout_are_absent_when_unset():
    element = element_of(bar.build_text_payload("hi"))

    assert "color" not in element
    assert "timeout" not in element


def test_colour_and_timeout_are_sent_when_given():
    element = element_of(bar.build_text_payload("hi", color="red", timeout=30))

    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30


def test_scrolling_can_be_switched_off():
    element = element_of(bar.build_text_payload("hi", scroll_rate=0))

    assert element["scroll_rate"] == 0


def test_a_negative_timeout_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", timeout=-1)


def test_an_unrecognised_colour_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", color="definitely-not-a-colour")


def test_draw_text_posts_the_payload(config):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    payload = bar.build_text_payload("BUILD OK")
    with bar.open_client(config, transport=httpx.MockTransport(handler)) as client:
        bar.draw_text(client, payload)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/display/draw"
    assert requests[0].headers["X-API-Token"] == "testtoken"
    body = json.loads(requests[0].content)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_clear_deletes_the_drawing(config):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    with bar.open_client(config, transport=httpx.MockTransport(handler)) as client:
        bar.clear(client)

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/api/display/draw"


def test_a_rejected_request_raises(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with bar.open_client(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(exceptions.BusyBarError):
            bar.clear(client)
```

Use 401 rather than 500 for the failure case. busylib retries retryable statuses (`408, 429, 500, 502, 503, 504`) up to `max_retries=2` with a 0.25 s backoff, so a 500 makes the test sleep for no benefit. 401 is not retryable and fails immediately.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_bar.py -v`
Expected: collection error, `ImportError: cannot import name 'bar' from 'busyboy'`.

- [ ] **Step 3: Write the implementation**

Create `src/busyboy/bar.py`:

```python
"""Construction and delivery of BUSY Bar display payloads."""

from typing import get_args

from busylib import BusyBar, types
import httpx

from busyboy.config import BusyboyConfig

APPLICATION_NAME = "busyboy"
TEXT_ELEMENT_ID = "text"

# The front display is a 72x16 RGB LED matrix; spanning its full width gives
# scrolling text the whole panel to move across.
FRONT_DISPLAY_WIDTH = 72

DEFAULT_FONT: types.DisplayFontName = "condensed"
FONT_NAMES: tuple[str, ...] = get_args(types.DisplayFontName)

# Units are undocumented in busylib; see Task 5 for calibration against a
# real bar.
DEFAULT_SCROLL_RATE = 20


def open_client(
    config: BusyboyConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> BusyBar:
    """
    Build a client for the configured bar.

    Passing both a host and a token puts busylib in network mode, which sends
    the token as X-API-Token. `transport` exists so tests can substitute an
    httpx.MockTransport.
    """
    return BusyBar(
        config.host,
        token=config.token.get_secret_value(),
        transport=transport,
    )


def build_text_payload(
    text: str,
    *,
    font: types.DisplayFontName = DEFAULT_FONT,
    color: str | None = None,
    timeout: int | None = None,
    scroll_rate: int = DEFAULT_SCROLL_RATE,
) -> types.DisplayElements:
    """
    Build the draw payload for a single centered line on the front display.

    `color` and `timeout` stay None when unset so busylib drops them from the
    request body and the firmware's own defaults apply.
    """
    element = types.TextElement(
        id=TEXT_ELEMENT_ID,
        text=text,
        font=font,
        color=color,
        timeout=timeout,
        display=types.DisplayName.FRONT,
        align="center",
        width=FRONT_DISPLAY_WIDTH,
        scroll_rate=scroll_rate,
    )
    return types.DisplayElements(
        application_name=APPLICATION_NAME,
        elements=[element],
    )


def draw_text(client: BusyBar, payload: types.DisplayElements) -> None:
    """Send a draw payload to the bar."""
    client.display_draw(payload)


def clear(client: BusyBar) -> None:
    """Remove what busyboy drew."""
    client.display_clear()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_bar.py -v`
Expected: 11 passed.

- [ ] **Step 5: Run the full check**

Run: `make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/busyboy/bar.py tests/test_bar.py
git commit -m "feat: build and deliver BUSY Bar text payloads"
```

---

### Task 3: Command-line interface

Turn `main` into a group with `text` and `clear`, mapping failures to exit codes.

**Files:**
- Create: `src/busyboy/cli.py`
- Modify: `src/busyboy/__init__.py` (replace entirely)
- Delete: `tests/test_main.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2.
- Produces: `main` — a `click.Group` re-exported from `busyboy/__init__.py`, keeping `[project.scripts] busyboy = "busyboy:main"` valid.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
"""Tests for exit codes and output of the busyboy command line."""

import json

from click.testing import CliRunner
import httpx
import pytest

from busyboy import bar, cli

ENV = {"BUSYBOY_HOST": "10.0.4.20", "BUSYBOY_TOKEN": "testtoken"}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


class Recorder:
    """An in-memory stand-in for the bar, capturing what the CLI sends."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: dict[str, str] = {"result": "ok"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def recorder(monkeypatch):
    """Route the CLI's client through a mock transport."""
    recorder = Recorder()
    real_open_client = bar.open_client

    def fake_open_client(config, *, transport=None):
        return real_open_client(config, transport=httpx.MockTransport(recorder.handle))

    monkeypatch.setattr(cli.bar, "open_client", fake_open_client)
    return recorder


def test_a_successful_draw_says_nothing(recorder):
    result = CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""


def test_text_sends_the_string_to_the_bar(recorder):
    CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "POST"
    body = json.loads(recorder.requests[0].content)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_options_reach_the_payload(recorder):
    CliRunner().invoke(
        cli.main,
        ["text", "hi", "--color", "red", "--timeout", "30", "--font", "small"],
        env=ENV,
    )

    element = json.loads(recorder.requests[0].content)["elements"][0]
    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30
    assert element["font"] == "small"


def test_clear_issues_a_delete(recorder):
    result = CliRunner().invoke(cli.main, ["clear"], env=ENV)

    assert result.exit_code == 0
    assert recorder.requests[0].method == "DELETE"


def test_host_flag_overrides_the_environment(recorder):
    CliRunner().invoke(cli.main, ["text", "hi", "--host", "192.168.1.5"], env=ENV)

    assert recorder.requests[0].url.host == "192.168.1.5"


def test_missing_configuration_exits_one_and_names_the_variable():
    result = CliRunner().invoke(cli.main, ["text", "hi"], env={})

    assert result.exit_code == 1
    assert "BUSYBOY_HOST" in result.stderr
    assert "BUSYBOY_TOKEN" in result.stderr


def test_a_rejected_request_exits_one(recorder):
    recorder.status = 401
    recorder.body = {"error": "unauthorized"}

    result = CliRunner().invoke(cli.main, ["text", "hi"], env=ENV)

    assert result.exit_code == 1
    assert "401" in result.stderr


def test_a_missing_argument_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text"], env=ENV)

    assert result.exit_code == 2


def test_an_unknown_font_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text", "hi", "--font", "cursive"], env=ENV)

    assert result.exit_code == 2


def test_an_invalid_colour_exits_one(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi", "--color", "chartreuseish"], env=ENV)

    assert result.exit_code == 1
    assert "color" in result.stderr


def test_help_lists_both_subcommands():
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0
    assert "text" in result.output
    assert "clear" in result.output
```

`result.stderr` is a separate stream: Click 8.2 removed `CliRunner(mix_stderr=...)` and captures the two independently. `result.output` is stdout only.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_cli.py -v`
Expected: collection error, `ImportError: cannot import name 'cli' from 'busyboy'`.

- [ ] **Step 3: Write the implementation**

Create `src/busyboy/cli.py`:

```python
"""Command-line entry points for busyboy."""

from collections.abc import Callable
import functools
import logging
from typing import Any, cast

from busylib import exceptions, types
import click
from pydantic import ValidationError

from busyboy import bar
from busyboy.config import ConfigError, load_config


def _configure_logging(*, verbose: bool) -> None:
    """
    Set up logging for one invocation.

    busylib logs API failures at error level. Without a level override those
    reach stderr through Python's last-resort handler and duplicate the message
    we print ourselves.
    """
    if verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.getLogger("busylib").setLevel(logging.CRITICAL)


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
        except exceptions.BusyBarError as error:
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
    help="Scroll speed for text wider than the display. 0 disables scrolling.",
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
        font=cast(types.DisplayFontName, font),
        color=color,
        timeout=timeout,
        scroll_rate=scroll_rate,
    )
    with bar.open_client(config) as client:
        bar.draw_text(client, payload)


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
    with bar.open_client(config) as client:
        bar.clear(client)
```

The `cast` on `font` is needed because `click.Choice` hands back a plain `str` while `TextElement.font` is a `Literal`. `click.Choice(bar.FONT_NAMES)` has already restricted the value to that set, so the cast asserts something the parser guarantees.

- [ ] **Step 4: Replace the package entry point**

Overwrite `src/busyboy/__init__.py` — the old `main` command and its `--count` option are gone:

```python
"""busyboy: display information on a BUSY Bar."""

from busyboy.cli import main

__all__ = ["main"]
```

- [ ] **Step 5: Delete the obsolete test**

`tests/test_main.py` calls `main()` directly, which now starts a Click group and exits. Every case it covered is subsumed by `tests/test_cli.py`.

```bash
git rm tests/test_main.py
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -v`
Expected: 30 passed across the three test modules.

- [ ] **Step 7: Verify the installed command works end to end**

Run:

```bash
uv run --frozen busyboy --help
uv run --frozen busyboy text --help
```

Expected: the group lists `text` and `clear`; `text --help` shows `--font` with its eight choices, `--color`, `--timeout`, `--scroll-rate`, `--host`, `--token`, `--verbose`.

- [ ] **Step 8: Run the full check**

Run: `make test`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/busyboy/cli.py src/busyboy/__init__.py tests/test_cli.py
git add -u tests/test_main.py
git commit -m "feat: add text and clear subcommands"
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the CLI from Task 3.
- Produces: nothing code depends on.

- [ ] **Step 1: Write the usage section**

Replace `README.md` with the following (the outer fence is four backticks; the
file itself uses three):

````markdown
# busyboy

Console application to display various information on BUSYBar.

## Installation

```bash
uv tool install busyboy
```

## Configuration

Both variables are required:

| Variable | Meaning |
| --- | --- |
| `BUSYBOY_HOST` | Hostname or IP of the bar, e.g. `10.0.4.20` |
| `BUSYBOY_TOKEN` | API token for the bar |

`--host` and `--token` override them for a single invocation.

## Usage

```bash
export BUSYBOY_HOST=10.0.4.20
export BUSYBOY_TOKEN=your-token

# Show a message on the front display
busyboy text "BUILD OK"

# In colour, disappearing after 30 seconds
busyboy text "deploy failed" --color red --timeout 30

# Take it down again
busyboy clear
```

Text stays on the display until it is replaced, cleared, or its `--timeout`
expires. Anything too wide for the 72x16 front display scrolls; pass
`--scroll-rate 0` to switch that off.

Successful commands print nothing and exit 0. Failures print one line to
stderr and exit 1. Use `--verbose` to see the underlying requests.
````

- [ ] **Step 2: Verify the examples**

Run each command in the Usage block against a real bar, or with `BUSYBOY_HOST`/`BUSYBOY_TOKEN` set and the bar reachable. Confirm `--help` output matches what the README claims.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document the text and clear subcommands"
```

---

### Task 5: Calibrate the undocumented defaults against hardware

The spec flags two values that cannot be derived from busylib's schema. Both are bare `int` fields with no documented semantics. This task requires a physical BUSY Bar.

**Files:**
- Modify: `src/busyboy/bar.py` (the `DEFAULT_SCROLL_RATE` constant and its comment)
- Modify: `src/busyboy/cli.py` (the `--timeout` help text, if the unit turns out not to be seconds)

**Interfaces:**
- Consumes: the CLI from Task 3.
- Produces: no signature changes — only constant values and help text.

- [ ] **Step 1: Determine the timeout unit**

Run, with a stopwatch:

```bash
busyboy text "TEN" --timeout 10
```

If the text clears after roughly 10 seconds, the field is seconds and `--timeout`'s help text is already correct. If it clears almost immediately, the field is milliseconds: convert in `build_text_payload` by multiplying by 1000, keep the flag in seconds, and add a comment recording the observation.

- [ ] **Step 2: Determine a readable scroll rate**

Draw a string comfortably wider than the display at several rates:

```bash
for rate in 5 10 20 40 80; do
  echo "rate=$rate"
  busyboy text "the quick brown fox jumps over the lazy dog" --scroll-rate "$rate"
  sleep 8
done
```

Note which is comfortably readable, and whether higher numbers mean faster or slower.

- [ ] **Step 3: Record the finding**

Update `DEFAULT_SCROLL_RATE` in `src/busyboy/bar.py` to the chosen value and replace its comment with what was actually observed, for example:

```python
# Units are undocumented in busylib. Measured on firmware <version>: higher is
# faster, and 20 scrolls a full-width string across in roughly four seconds.
DEFAULT_SCROLL_RATE = 20
```

- [ ] **Step 4: Update the test if the default changed**

`tests/test_bar.py::test_defaults_centre_condensed_text_across_the_front_display` asserts against `bar.DEFAULT_SCROLL_RATE`, so it follows the constant automatically. If Step 1 introduced a seconds-to-milliseconds conversion, add a test for it:

```python
def test_timeout_is_converted_to_the_firmware_unit():
    element = element_of(bar.build_text_payload("hi", timeout=10))

    assert element["timeout"] == 10000
```

- [ ] **Step 5: Run the full check**

Run: `make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/busyboy/bar.py src/busyboy/cli.py tests/test_bar.py
git commit -m "fix: calibrate timeout and scroll rate against hardware"
```

---

## Verification

After Task 5, the branch should satisfy:

- `make test` passes: ruff check, ruff format check, ty check, and the full pytest suite.
- `make coverage` passes the 75% gate.
- `uv run --frozen busyboy text "hello"` puts "hello" on a real bar and exits 0 silently.
- `uv run --frozen busyboy clear` removes it.
- `busyboy text "hi"` with `BUSYBOY_TOKEN` unset exits 1 with a message naming the variable and the flag.
