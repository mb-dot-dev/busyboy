# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

busyboy is a console application (CLI) that displays information on a BUSY Bar. It talks to the bar's HTTP
API directly over `requests`, following the bar's own OpenAPI spec (vendored at
`docs/superpowers/references/openapi.yaml`) for field names, patterns, and enums.

Requires Python >=3.14. Dependency management, builds, and packaging all go through `uv`.

## Commands

All commands run through `make` (which wraps `uv run --frozen ...`):

- `make install-dev` — install all dependencies (including dev group) from the lockfile.
- `make install` — install production dependencies only.
- `make unit` — run the test suite (`pytest`).
- `make lint` — run `ruff check`, `ruff format --check`, and `ty check` (type checking).
- `make format` — auto-format code with `ruff format`.
- `make test` — run `lint` then `unit`; this is the full local check before committing.
- `make coverage` — run tests with coverage, writing an XML report to `build/coverage.xml`.
- `make upgrade` — upgrade dependencies and refresh `uv.lock`.

To run a single test file or test: `uv run --frozen pytest tests/test_bar.py::test_clear_deletes_the_drawing`.

Always run `uv sync` (via `make install-dev`) after pulling changes that touch `pyproject.toml` or `uv.lock`, since
commands use `--frozen` and expect the lockfile to already match.

## Architecture

Four modules with one job each. Keep the boundaries: `config.py` imports neither Click nor `bar.py`, `bar.py`
knows nothing about argv or the environment, and `cli.py` holds no BUSY Bar payload knowledge.

- `src/busyboy/config.py` — `BusyboyConfig` (pydantic-settings, `env_prefix="BUSYBOY_"`, frozen), defaulting
  `host` to `10.0.4.20` and `token` to `None`, plus `load_config(*, host, token)` and `ConfigError`. Explicit
  arguments beat environment variables because pydantic-settings ranks init arguments above env sources.
  `BusyboyConfig.token_value` encapsulates unwrapping the `SecretStr` (or returning `None`) so callers never
  touch `get_secret_value()` directly.
- `src/busyboy/bar.py` — `build_text_payload` (pure, no I/O), `draw_text`, `clear`, the payload constants, and
  the `requests`-based transport itself: a `requests.request(...)` call per delivery, retried up to
  `MAX_RETRIES` times with backoff on transport-level failures, raising `busyboy.exceptions.BarAPIError` or
  `BarRequestError` on failure. This is where BUSY Bar knowledge lives — payload shape and delivery both.
- `src/busyboy/exceptions.py` — `BarError` (base), `BarAPIError`, `BarRequestError`, and
  `format_delivery_error(error) -> str`, the one-line renderer `cli.py` prints to stderr.
- `src/busyboy/cli.py` — the `main` Click group, the `text` and `clear` subcommands, and the
  error-to-exit-code mapping in `_handle_errors`.
- `src/busyboy/__init__.py` — re-exports `main` so `[project.scripts] busyboy = "busyboy:main"` keeps working.
  Do not put logic here.
- Tests live in `tests/`, one module per source module.
- Coverage config (`[tool.coverage.*]` in `pyproject.toml`) requires 75% coverage and measures branches over
  `src`, excluding `if TYPE_CHECKING:` blocks. It currently sits around 95%.
- Ruff is configured with a 120-char line length, double quotes, PEP 257 docstring convention, and isort settings
  that force sorting within sections and split on trailing commas.

### CLI contract

Success is silent with exit 0. Expected failures print one line to stderr and exit 1; Click usage errors exit 2.
`--verbose` sets the root logger to `DEBUG` (surfacing `urllib3`'s own per-connection request logging) and lets
the original exception propagate for a traceback.

Configuration is `BUSYBOY_HOST` and `BUSYBOY_TOKEN`, both optional and overridable per invocation with
`--host` / `--token`. `host` defaults to `10.0.4.20`, the bar's own USB-subnet address, so a USB-connected bar
needs no configuration at all. `token` defaults to unset. A configured token is always sent as `X-API-Token`;
an unset one is simply omitted from the request — there is no separate client "mode" to be in. Over WiFi (a
non-default host) the token remains optional — supply one if the bar requires it.

## Gotchas

These cost real time to discover. Check here before re-deriving them.

**Unpacking into `BaseSettings` needs `dict[str, Any]`.** `BaseSettings.__init__` also accepts private keyword
arguments (`_case_sensitive`, `_env_prefix`, ...), so unpacking a `dict[str, str]` makes `ty` report
`Expected 'bool | None', found 'str'`.

**Ruff's `force-sort-within-sections` sorts `import x` and `from x import y` together** by module name. So
`from pydantic import ...` comes before `from pydantic_extra_types.color import Color`, which comes before
`import requests`; and `from collections.abc import Callable` before `import functools`. Write imports that
way or `make format` will rewrite them.

**Never let pydantic's `ValidationError` into a traceback that a user sees.** Its `missing` errors carry the
entire pre-coercion input dict as `input_value` — including the API token. `SecretStr` does not help, because
the leak is of the raw input rather than the coerced field. `load_config` raises `ConfigError(...) from None`
specifically to break that chain; keep it that way even though `host` and `token` both have defaults now and no
`missing` error can currently be produced — a future required field would reintroduce the same risk.

**`click.Choice` returns `str`**, so passing it to `bar.DisplayFontName` (a `Literal`) needs a `cast`. That
cast is legitimate — the parser has already restricted the value.

## Testing

Tests drive the real `bar.py` functions against `responses`-registered endpoints rather than mocking anything
internal to `bar.py` or `requests` itself.

- Response bodies don't need to match any particular shape — busyboy only checks the HTTP status code, and
  discards the response body entirely on success.
- Use **401** for failure-path tests, not 500 or a registered connection error, unless the test specifically
  targets retry behavior — `bar.py` retries transport-level failures (connection errors, timeouts) up to
  `MAX_RETRIES` times with a `RETRY_BACKOFF_SECONDS * (attempt + 1)` sleep between attempts, so provoking that
  path sleeps the test unless `time.sleep` is monkeypatched (see `test_a_connection_failure_retries_then_raises`
  in `tests/test_bar.py`). HTTP error responses are never retried, regardless of status code.
- `tests/test_cli.py` registers a `responses` callback matching any host (`re.compile(r"^http://[^/]+/api/display/draw")`)
  so the `--host` override test doesn't need special-casing.
- Click 8.2+ removed `CliRunner(mix_stderr=...)`. `result.output` is stdout only; `result.stderr` is separate.
- Tests that touch config must clear `BUSYBOY_HOST` / `BUSYBOY_TOKEN` (there is an autouse fixture for this) or
  they will pick up a real environment.

## Hardware facts

Measured against a real bar by capturing frames from `/api/screen` and analysing pixel shift. The OpenAPI spec
(`docs/superpowers/references/openapi.yaml`) documents none of this.

- The front display is **72x16** RGB; the back display is 160x80 monochrome. busyboy only uses the front.
- **`scroll_rate` is pixels per minute** — px/sec is roughly `rate / 60`. Higher is faster; text scrolls
  leftward. `DEFAULT_SCROLL_RATE = 1200` gives ~18 px/s, about four seconds to cross the display. Small values
  look frozen: 20 is ~0.33 px/s, over three minutes per traverse.
- **`timeout` is in seconds.** Confirmed: `--timeout 10` cleared between t=10s and t=12s.
- **Do not use `align`.** `align="center"` clips text off the top of the front display (glyphs land in rows 0-3).
  Position with an explicit `y` instead. The `condensed` glyph box is 9 rows tall, so `y=2` centres it on the
  16-row display (rows 4-12).
- **Scope every element to `application_name`.** `display_clear()` without it wipes the whole display, including
  other applications' elements — pass `application_name=APPLICATION_NAME`, as the draw path does.
- Elements carry a stable `id`, so redrawing replaces rather than stacks.

### Testing against a real bar

`source ~/.zshrc && busybarenv` exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN` for a real device. Never print or
commit the token value.

Frame capture for visual verification (`GET /api/screen`, base64-encoded RGB888/L4 framebuffer bytes) previously
used busylib's `BusyBar.screen(0)` helper during hardware calibration. That helper is no longer available in
this project's environment now that busylib isn't a dependency — busyboy's own CLI never called it, so it
wasn't reimplemented. If frame capture is needed again, either `pip install busylib` in a scratch virtualenv
outside this project's lockfile, or decode `/api/screen` directly: the response body's `Content-Type:
image/bmp` header is misleading (there's no real BMP header), it's base64-encoded raw framebuffer bytes,
RGB888 for the front display (`display=0`) and L4-packed (2 pixels/byte) for the back display (`display=1`).

## CI

`.github/workflows/main.yaml` runs on every push/PR to `main`: installs deps, lints, runs coverage, then submits
results to SonarQube/SonarCloud (project key `mb-dot-dev_busyboy`) with the quality gate blocking the workflow.
`.github/workflows/release.yaml` publishes to PyPI on `v*.*.*` tags via `uv build` / `uv publish`, using the
tag as the version.

## Docs

Design specs and implementation plans live under `docs/superpowers/`. Read the relevant spec before changing
behaviour it describes.
