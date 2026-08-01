# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

busyboy is a console application (CLI) that displays information on a BUSY Bar. It depends on `busylib`
(`https://github.com/busy-app/busylib-py`), which wraps the bar's HTTP API.

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

Three modules with one job each. Keep the boundaries: `config.py` imports neither Click nor busylib, `bar.py`
knows nothing about argv or the environment, and `cli.py` holds no BUSY Bar payload knowledge.

- `src/busyboy/config.py` — `BusyboyConfig` (pydantic-settings, `env_prefix="BUSYBOY_"`, frozen) plus
  `load_config(*, host, token)` and `ConfigError`. Explicit arguments beat environment variables because
  pydantic-settings ranks init arguments above env sources.
- `src/busyboy/bar.py` — `open_client`, `build_text_payload` (pure, no I/O), `draw_text`, `clear`, and the
  payload constants. This is where BUSY Bar knowledge lives.
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
`--verbose` enables busylib request logging and lets the original exception propagate for a traceback.

Configuration is `BUSYBOY_HOST` and `BUSYBOY_TOKEN`, both required, overridable per invocation with
`--host` / `--token`. Passing both a host and a token puts busylib in "network" mode, which sends the token as
`X-API-Token`; supplying only a token silently switches it to cloud mode and a `Bearer` header, which is why
both are mandatory.

## Gotchas

These cost real time to discover. Check here before re-deriving them.

**busylib's context manager loses the subtype.** `SyncClientBase.__enter__` is annotated to return
`SyncClientBase`, not `Self`, and `display_draw`/`display_clear` live on `BusyBar`. So
`with open_client(...) as client:` types `client` as `SyncClientBase` and fails `ty check`. Bind first, then use
the binding as the context manager:

```python
client = bar.open_client(config)
with client:
    bar.draw_text(client, payload)
```

Do not reach for `cast`, `Any`, or `# noqa` here — they only hide it.

**Unpacking into `BaseSettings` needs `dict[str, Any]`.** `BaseSettings.__init__` also accepts private keyword
arguments (`_case_sensitive`, `_env_prefix`, ...), so unpacking a `dict[str, str]` makes `ty` report
`Expected 'bool | None', found 'str'`.

**Ruff's `force-sort-within-sections` sorts `import x` and `from x import y` together** by module name. So
`from busylib import ...` comes before `import click`, and `from collections.abc import Callable` before
`import functools`. Write imports that way or `make format` will rewrite them.

**busylib logs API failures itself**, at error level. With no logging configured those reach stderr through
Python's last-resort handler and duplicate the CLI's own message. `cli.py` sets the `busylib` logger to
`CRITICAL` unless `--verbose`.

**Never let pydantic's `ValidationError` into a traceback that a user sees.** Its `missing` errors carry the
entire pre-coercion input dict as `input_value` — including the API token. `SecretStr` does not help, because
the leak is of the raw input rather than the coerced field. `load_config` raises `ConfigError(...) from None`
specifically to break that chain; keep it that way, and keep the regression test in `tests/test_cli.py`.

**`click.Choice` returns `str`**, so passing it to a busylib `Literal` field needs a `cast`. That cast is
legitimate — the parser has already restricted the value.

## Testing

`busylib`'s client accepts `transport=`, so tests drive a real `BusyBar` against an `httpx.MockTransport` rather
than mocking busylib internals. `bar.open_client` exposes that parameter for exactly this reason.

- Mock responses must match the real shape: `SuccessResponse` requires a `result` field, so return
  `{"result": "ok"}`, not `{"success": True}`.
- Use **401** for failure-path tests, not 500. busylib retries `408, 429, 500, 502, 503, 504` up to
  `max_retries=2` with a 0.25 s backoff, so a 500 just makes the test sleep.
- `tests/test_cli.py` monkeypatches `cli.bar.open_client` to inject the mock transport and record requests.
- Click 8.2+ removed `CliRunner(mix_stderr=...)`. `result.output` is stdout only; `result.stderr` is separate.
- Tests that touch config must clear `BUSYBOY_HOST` / `BUSYBOY_TOKEN` (there is an autouse fixture for this) or
  they will pick up a real environment.

## Hardware facts

Measured against a real bar by capturing frames from `/api/screen` and analysing pixel shift. busylib's schema
documents none of this.

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

`source ~/.zshrc && busybarenv` exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN` for a real device. Frames can be read
back with `BusyBar.screen(0)`, which returns decoded RGB888 bytes — useful for asserting what actually rendered.
Never print or commit the token value.

## CI

`.github/workflows/main.yaml` runs on every push/PR to `main`: installs deps, lints, runs coverage, then submits
results to SonarQube/SonarCloud (project key `mb-dot-dev_busyboy`) with the quality gate blocking the workflow.
`.github/workflows/release.yaml` publishes to PyPI on `v*.*.*` tags via `uv build` / `uv publish`, using the
tag as the version.

## Docs

Design specs and implementation plans live under `docs/superpowers/`. Read the relevant spec before changing
behaviour it describes.
