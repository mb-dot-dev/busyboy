# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

busyboy is a console application (CLI) that displays information on a BUSY Bar. It talks to the bar's HTTP
API through [`busylib`](https://github.com/busy-app/busylib-py), the vendor's own client library, though
`bar.py`'s own payload models (`TextElement`, `ImageElement`, `DisplayElements`) still encode field names,
patterns, and enums learned from the bar's OpenAPI spec (vendored at `docs/superpowers/references/openapi.yaml`)
where busylib's own schema is laxer than busyboy wants. `github.py` talks to the GitHub REST API directly over
`requests`, unrelated to the bar.

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

Seven modules (plus a packaged asset directory) with one job each. Keep the boundaries: `config.py` imports
neither Typer nor `bar.py`; `bar.py` knows nothing about argv or the environment; `cli.py` holds no BUSY Bar
payload knowledge; `git.py` knows nothing about GitHub, the bar, or Typer; `github.py` knows nothing about the
bar, Typer, or `git.py`; and `watch.py` is the **only** module allowed to import both `bar` and `github` — it
knows nothing about Typer or argv itself. `git.py` and `github.py` must never import each other. That rule is
why `git.origin_repo()` returns a bare `tuple[str, str]` rather than a `github.Repo`: a `git.py` function
returning a GitHub-typed value would create exactly the import edge the boundary forbids, so `cli.py` — which
is allowed to know about both — does the `github.Repo(owner=owner, name=name)` construction itself.

- `src/busyboy/config.py` — `BusyboyConfig` (pydantic-settings, `env_prefix="BUSYBOY_"`, frozen), defaulting
  `host` to `10.0.4.20` and `token` to `None`, plus `load_config(*, host, token)` and `ConfigError`. Explicit
  arguments beat environment variables because pydantic-settings ranks init arguments above env sources.
  `BusyboyConfig.token_value` encapsulates unwrapping the `SecretStr` (or returning `None`) so callers never
  touch `get_secret_value()` directly.
- `src/busyboy/git.py` — local checkout inspection only: `current_branch()`, `origin_repo()`, and
  `parse_remote_url()` for the owner/name out of any remote URL form git accepts. Shells out to `git` via
  `subprocess`, redacting any userinfo credentials embedded in a remote URL before they can reach a log line or
  an exception message. Raises `GitError`. Knows nothing about GitHub's API, the bar, or Typer.
- `src/busyboy/github.py` — GitHub token resolution (`resolve_token`: `gh auth token`, then `GITHUB_TOKEN`) and
  REST queries (`resolve_workflow`, `latest_run`, `pull_request_number`) via plain `requests` calls through the
  single `_get` helper, which classifies every failure as fatal (`GitHubError`/`GitHubAuthError`) or retryable
  (`GitHubTransientError`) — see Exception hierarchy below. Knows nothing about the bar, Typer, or `git.py`.
- `src/busyboy/bar.py` — `build_text_payload` and `build_workflow_payload` (both pure, no I/O), `draw_text`,
  `clear`, `upload_icons`, and the payload models (`TextElement`, `ImageElement`, `DisplayElements`). These
  models are deliberately stricter than the vendored [`busylib`](https://github.com/busy-app/busylib-py)
  client's own — the ASCII-only text pattern and the narrowed image-path pattern below are validated here,
  before a payload ever reaches busylib, which is laxer on both. Delivery itself goes through
  `busylib.BusyBar`: `_client` builds one per call from `config.host`/`config.token_value`, and `draw_text`,
  `clear`, and `upload_icons` call its `display_draw`, `display_clear`, and `assets_upload` methods (passing
  a plain `dict` via `payload.model_dump(exclude_none=True)` rather than busylib's own `types.DisplayElements`,
  so busyboy's stricter validation is what actually ran). busylib owns retries, timeouts, and the request/
  response shape now; `_reraise_as_bar_error` maps its `busylib.exceptions.BusyBarAPIError` onto
  `busyboy.exceptions.BarAPIError` and everything else under `BusyBarError` onto `BarRequestError`, so the
  rest of busyboy still only ever sees the two-branch hierarchy described below. This is where all BUSY Bar
  knowledge lives — text and image element shape, the two-row workflow layout, asset upload, and delivery.
- `src/busyboy/watch.py` — the poll loop: `tick` (one fetch-render-diff-draw cycle) and `watch` (the `while
  True`, Ctrl+C, and cleanup around it). The only module that imports both `bar` and `github`, because turning a
  GitHub run into bar pixels is its entire job. Knows nothing about Typer or argv — `cli.py` builds its
  `Target` and passes it in. `render` composes the bottom row as the ref (`#123` for an open pull request,
  otherwise the branch) followed by the workflow's display name, so two workflows in the same repository on
  the same branch are distinguishable on the bar.
- `src/busyboy/exceptions.py` — the exception hierarchy (see below) and `format_delivery_error(error) -> str`,
  the one-line renderer `cli.py` prints to stderr.
- `src/busyboy/cli.py` — the `main` Typer app; the `text` and `clear` subcommands; the `gh` sub-app (added
  with `main.add_typer`) and its `workflow` subcommand; the `--version` callback; and the error-to-exit-code
  mapping in `_handle_errors`. Typer builds each command's parameters from its signature rather than from
  stacked decorators, so the three shared connection options live in the `HostOption`, `TokenOption`, and
  `VerboseOption` `Annotated` aliases at the top of the module, spelled into every command's signature.
- `src/busyboy/__init__.py` — re-exports `main` so `[project.scripts] busyboy = "busyboy:main"` keeps working.
  Do not put logic here.
- `src/busyboy/assets/` — six packaged status icon PNGs (`success`, `failure`, `pending`, `in_progress`,
  `cancelled`, `skipped`), 12x12, read by `bar.icon_bytes` via `importlib.resources`. Generated by
  `tools/generate_icons.py` (stdlib-only, no image library dependency); regenerate rather than hand-edit.
- `tools/capture_screen.py` — development tool, not part of the package: draws a probe string in each font and
  reads the framebuffer back through `GET /api/screen` to measure glyph offsets and heights. Run with
  `uv run --frozen python tools/capture_screen.py` against a real bar. This is how the font table in the
  hardware-facts section was produced; rerun it rather than guessing when a layout question comes up.
- Tests live in `tests/`, one module per source module.
- Coverage config (`[tool.coverage.*]` in `pyproject.toml`) requires 75% coverage and measures branches over
  `src`, excluding `if TYPE_CHECKING:` blocks. It currently sits around 95%.
- Ruff is configured with a 120-char line length, double quotes, PEP 257 docstring convention, and isort settings
  that force sorting within sections and split on trailing commas.

### Exception hierarchy

The root is `BusyboyError`, not `BarError`. Beneath it: `BarError` (BUSY Bar delivery failures —
`BarAPIError`, `BarRequestError`), `GitError` (local checkout inspection failures), and `GitHubError` (GitHub
API failures), with `GitHubAuthError` and `GitHubTransientError` beneath `GitHubError`.

This split is load-bearing, not cosmetic. `watch.tick` catches `GitHubTransientError` and `BarError` and
swallows both, leaving the display as it was: a watch process is expected to outlive a laptop sleeping or a
wifi hiccup, so a rate limit or a dropped bar connection just means "try again next tick." `GitHubAuthError` is
deliberately **not** caught anywhere in `tick` — it propagates out and ends the watch, because a rejected token
never self-heals. Writing `except GitHubError:` in that path to mean "fatal, stop the watch" would also catch
`GitHubTransientError`, its own subclass, and swallow it there instead of re-raising — the loop would then
retry forever against a revoked token rather than exiting, silently spinning instead of telling anyone the
token is dead. Always catch `GitHubTransientError` and `GitHubAuthError` by their specific names when the
retry-versus-exit distinction matters; catching their shared parent erases it.

### CLI contract

Success is silent with exit 0. Expected failures print one line to stderr and exit 1 (`_fail` writes the line
and raises `typer.Exit(1)`); Typer usage errors exit 2, rendered by Rich as a bordered panel.
`--verbose` sets the root logger to `DEBUG` (surfacing `urllib3`'s own per-connection request logging) and lets
the original exception propagate for a traceback.

Configuration is `BUSYBOY_HOST` and `BUSYBOY_TOKEN`, both optional and overridable per invocation with
`--host` / `--token`. `host` defaults to `10.0.4.20`, the bar's own USB-subnet address, so a USB-connected bar
needs no configuration at all. `token` defaults to unset. A configured token is always sent as `X-API-Token`;
an unset one is simply omitted from the request — there is no separate client "mode" to be in. Over WiFi (a
non-default host) the token remains optional — supply one if the bar requires it.

`busyboy gh workflow WORKFLOW` deliberately departs from the contract above once it's running: it does not
exit after one action, it runs until Ctrl+C. Startup still follows the normal contract — resolving the GitHub
token, the repo, the branch, and the workflow; the initial `bar.clear`; and `bar.upload_icons` all print one
line to stderr and exit 1 on failure (no token, no git repo, an unknown workflow, a rejected credential, a
failed icon upload). Once inside the poll loop, it stops behaving like every other command: transient GitHub
failures (`GitHubTransientError`) and bar delivery failures (`BarError`) are swallowed rather than raised, and
only logged at DEBUG — visible under `--verbose`, but they never print to stderr or end the process on their
own. See Exception hierarchy above for why `GitHubAuthError` is the one GitHub failure that still ends the
watch. A malformed `--repo` (anything that isn't `owner/name`) is a Typer usage error and exits 2, like any
other bad option.

The GitHub token comes from `gh auth token` first, then the `GITHUB_TOKEN` environment variable
(`github.resolve_token`). Neither source's value is ever written to a log line or an error message: `gh`'s own
stderr is discarded specifically so a login diagnostic from the CLI can't carry a token into busyboy's output.

Rate-limit handling lives in `github._get`: a 403 or 429 response that carries rate-limit evidence (a
`Retry-After` header, or `x-ratelimit-remaining: 0`) is treated as transient and retried; `watch.watch` honours
any `Retry-After` value by waiting at least that long before the next poll (`max(interval, retry_after)`
instead of just `interval`). A 403 with neither signal present is GitHub rejecting the token outright, not
rate limiting, and raises `GitHubAuthError` — fatal, per the hierarchy above.

## Gotchas

These cost real time to discover. Check here before re-deriving them.

**`except OSError, subprocess.TimeoutExpired:` is correct, not a Python 2 relic.** PEP 758, accepted for
Python 3.14, allows `except` to list multiple exception types without parentheses, and this project requires
`>=3.14`. More to the point, `ruff format` actively *rewrites* the parenthesized form into this one, so
"fixing" it back to `except (OSError, ...)` makes `ruff format --check` fail and breaks the build. It reads
like a syntax error to anyone carrying Python 2 habits — it has been flagged three separate times — but it
parses, imports, and passes CI on 3.14. Leave it.

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

**Typer turns a `Literal` annotation into a choice, so `--font` needs no `cast`.** `font` is annotated
`bar.DisplayFontName` directly; Typer derives the same choice list the old `click.Choice(bar.FONT_NAMES)`
produced, and hands back a value the type checker already knows is one of the literals. That is why
`bar.FONT_NAMES` is gone — it existed only because `click.Choice` wanted a `Sequence[str]`. Do not
reintroduce a parallel tuple or enum of font names; the `Literal` is the single source.

**`_handle_errors`' `functools.wraps` is load-bearing under Typer, not a nicety.** Typer reads a command's
parameters from `inspect.signature` and `get_type_hints` of whatever function it is handed — here, the
wrapper. `functools.wraps` copies `__annotations__` and `__wrapped__` across, which is the only reason the
wrapped command's signature still reaches Typer. Drop it and every command loses all its options.

**`--repo` uses `typer.Option(parser=...)`, not a Click-style `(ctx, param, value)` callback.** The parser is
a plain `str -> github.Repo` callable that Typer turns into the parameter's click type, which is what lets
the parameter be annotated `github.Repo | None` and still type-check. It still runs during parsing, which is
the point — see `_parse_repo`'s docstring. A parser is never called for an omitted option: parameters skip
type conversion when their value is `None`, so no `None` branch is needed (or reachable) inside it.

**Typer's `--version` is hand-rolled; there is no `click.version_option` equivalent.** `_show_version` is an
eager option callback on the `@main.callback()` group that echoes `importlib.metadata.version("busyboy")` and
raises `typer.Exit`. It must stay `is_eager=True` so `--version` works without a subcommand.

**`typer.Typer(pretty_exceptions_enable=False)` is required by the CLI contract.** Typer's default Rich
traceback handler would reformat the traceback that `--verbose` exists to expose. `no_args_is_help=True` is
also explicit on both apps: Typer's own default is `False`, which would turn a bare `busyboy` into an error
instead of the help screen Click's `Group` printed.

**`watch.watch`'s `sleep` parameter defaults to `None`, not `time.sleep`.** The body resolves it with `sleep if
sleep is not None else time.sleep`. A default bound at function-definition time (`sleep: Callable = time.sleep`)
would capture the real `time.sleep` at import time, before any test gets a chance to monkeypatch it — and the
CLI tests depend on patching `time.sleep` to drive the loop without waiting, ending it by raising
`KeyboardInterrupt` the way Ctrl+C does. Do not "simplify" this to a bound default; it would silently break
every test that drives the loop.

**`ruff format` formats Python code blocks inside Markdown files.** That's why `docs` and `.superpowers` are in
`[tool.ruff] exclude` in `pyproject.toml` — without it, `make format` would rewrite fenced code examples in the
design docs to match ruff's own style, including ones not meant to be syntactically valid Python on their own.

**`ImageElement.path`'s pattern is deliberately narrower than the bar's own OpenAPI contract.** The vendor spec
allows a broader character set there (letters, digits, `.`, `_`, `/`, `-`), which would let a traversal value
like `../../etc/passwd` through. busyboy only ever constructs `path` as `f"{icon}.png"` from the closed
`IconName` Literal, so the narrower pattern (`^[a-zA-Z0-9_-]+\.png$`) costs nothing and closes that hole. Do
not widen it back to match the vendor spec.

**`bar._to_displayable_ascii` sanitizes the workflow rows to `?`; `build_text_payload` still rejects non-ASCII
outright.** That asymmetry is deliberate, not an inconsistency to fix. `busyboy text`'s input came from someone
at a keyboard a moment ago, so rejecting it with a validation error is useful, actionable feedback. A workflow
row's text is a repository or branch name fetched from GitHub in the middle of the poll loop — GitHub permits
Unicode in both, and there is no user present at that moment to see or act on a rejection — so raising would
just kill the watch on a value nobody typed. Sanitizing one-for-one instead keeps the loop alive and the
display legible. `bar.strip_undisplayable` is a third point on that same spectrum, and the three are not
redundant. Reject outright (`build_text_payload`) for text a human typed a moment ago and can retype.
Replace one-for-one (`_to_displayable_ascii`) for a whole row, where dropping characters could empty it
and trip `TextElement.text`'s `min_length=1` mid-poll-loop, killing the watch over a value nobody typed.
Drop and collapse (`strip_undisplayable`) for one *component* of a row — the workflow name — where the
ref beside it already guarantees the row is non-empty, so a name like "🚀 Deploy" can read as "Deploy"
instead of "? Deploy". Collapsing the two private-looking helpers into one would either put stray `?`s
back in front of emoji-prefixed workflow names or reintroduce the empty-row crash. Keep all three.

## Testing

Tests drive the real `bar.py` functions against a mock HTTP transport rather than mocking anything internal to
`bar.py` itself. `bar.py` talks to the bar through `busylib`, which uses `httpx2` rather than `requests`, so
`responses` (which only patches `requests`) cannot intercept those calls — GitHub calls in `github.py` still go
through `requests` and are still mocked with `responses` directly. The `bar_transport` fixture in
`tests/conftest.py` is the shared seam: it monkeypatches `bar.busylib.BusyBar` to a factory that always injects
an `httpx2.MockTransport`, then exposes a small `responses`-style router (`.add(method, path, ...)`, `.calls`)
on top of it. Route registration is required — a call to an unregistered method+path raises `AssertionError`
from the fixture rather than silently reaching the network.

**No test may depend on ambient GitHub auth.** A test that reaches the real `github.resolve_token` shells out to
`gh auth token`, so it passes on a logged-in developer's machine and fails on a CI runner that has neither a gh
login nor `GITHUB_TOKEN`. Any test invoking a `gh workflow` command must monkeypatch `cli.github.resolve_token`,
and `tests/test_cli.py`'s autouse fixture clears `GITHUB_TOKEN` alongside the `BUSYBOY_*` pair. To check a
change against the CI environment rather than yours, put a `gh` that exits non-zero first on `PATH`:
`env -u GITHUB_TOKEN PATH=/dir/with/failing/gh:$PATH uv run --frozen pytest`.

- Response bodies don't need to match any particular shape — busyboy only checks the HTTP status code, and
  discards the response body entirely on success.
- Use **401** for failure-path tests, not 500 or a simulated transport error, unless the test specifically
  targets retry behavior — busylib retries transport-level failures (raised from the mock transport as, e.g.,
  `httpx2.ConnectError`) with a growing backoff sleep between attempts, so provoking that path sleeps the test
  unless `time.sleep` is monkeypatched (see `test_a_connection_failure_retries_then_raises` in
  `tests/test_bar.py`; patch the real `time` module, not `bar.time` — `bar.py` no longer imports `time` itself,
  since busylib's own retry loop is the one sleeping). HTTP error responses are never retried, regardless of
  status code.
- `bar_transport` matches routes by method and path only, not by host — the same registration works
  regardless of which `--host`/`BUSYBOY_HOST` value a test exercises, so the `--host` override test needs no
  special-casing.
- `tests/test_cli.py` drives the app with `typer.testing.CliRunner`, which takes the `typer.Typer` instance
  (`cli.main`) directly. Click 8.2+ removed `CliRunner(mix_stderr=...)`: `result.stderr` is its own stream,
  and `result.output` is **both** streams interleaved in write order — not stdout alone.
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
- Elements carry a stable `id`, so redrawing replaces rather than stacks. This holds for `ImageElement` as well
  as `TextElement` — it's why the two-row workflow layout's icon, repo row, and ref row (fixed ids `icon`,
  `repo`, `ref`) can be redrawn on every change without ever stacking a duplicate on the panel.
- **Font glyph geometry, measured with `tools/capture_screen.py`.** A font's `y` is not its first inked row —
  each font sits `offset` rows below the `y` it is given — so placing a row deliberately needs both numbers.
  `global` is absent: it selects the device's configured font rather than naming a size.

  | font | offset | height |
  |---|---|---|
  | tiny | 1 | 5 |
  | small | 2 | 7 |
  | normal | 2 | 9 |
  | condensed | 2 | 9 |
  | bold | 2 | 9 |
  | large | 2 | 11 |
  | extra_large | 2 | 10 |

  The workflow layout stacks `ROW_ONE_FONT` over `ROW_TWO_FONT` at `ROW_ONE_Y` / `ROW_TWO_Y` in `bar.py`, with
  leftover rows spread top margin, then gap, then bottom margin. Currently `ROW_ONE_FONT = "normal"` at
  `ROW_ONE_Y = -2` inks rows 0-8, and `ROW_TWO_FONT = "small"` at `ROW_TWO_Y = 7` inks rows 9-15: `normal`'s 9
  rows plus `small`'s 7 rows fill all 16 rows exactly, with no gap and no overlap. `ROW_ONE_Y` being negative is
  legal and deliberate — it is how the top row is made to ink from display row 0. Any font `condensed`'s height
  (9 rows) or taller — `condensed`, `normal`, `bold`, `extra_large`, and `large` — cannot be stacked twice:
  9+9=18 exceeds the 16-row display, and the others are taller still.
- **Redrawing an element restarts its scroll animation.** Confirmed on a real bar: when a watched workflow's
  status changed and `watch.tick` issued a redraw, the scrolling repo row jumped back to its starting position
  rather than continuing from where it was. This is why `tick` compares the freshly rendered `Screen` against
  the previous one and skips `bar.draw_text` entirely when nothing changed — an unconditional redraw every poll
  interval would restart the scroll every time, so a repo slug wider than the 54px text column would never
  finish a single traverse. The diff is load-bearing, not an optimisation. Do not remove it.

### Testing against a real bar

`source ~/.zshrc && busybarenv` exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN` for a real device. Never print or
commit the token value.

Frame capture for visual verification lives in `tools/capture_screen.py` (`GET /api/screen`). The response's
`Content-Type: image/bmp` header is misleading — there is no BMP header, just base64-encoded raw framebuffer
bytes: RGB888 for the front display (`display=0`), L4-packed (2 pixels/byte) for the back (`display=1`). The
tool decodes the front display only.

## CI

`.github/workflows/main.yaml` runs on every push/PR to `main`: installs deps, lints, runs coverage, then submits
results to SonarQube/SonarCloud (project key `mb-dot-dev_busyboy`) with the quality gate blocking the workflow.
`.github/workflows/release.yaml` publishes to PyPI on `v*.*.*` tags via `uv build` / `uv publish`, using the
tag as the version.

## Docs

Design specs and implementation plans live under `docs/superpowers/`. Read the relevant spec before changing
behaviour it describes.
