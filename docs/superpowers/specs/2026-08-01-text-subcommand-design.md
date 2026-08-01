# `busyboy text` — design

**Date:** 2026-08-01
**Branch:** `feat/text-subcommand`

## Goal

Give busyboy its first real capability: put a short string on the BUSY Bar's front
display from the command line, and take it down again.

```
busyboy text "BUILD OK"
busyboy text "deploy failed" --color red --timeout 30
busyboy clear
```

The BUSY Bar is reached over HTTP through `busylib`. Its hostname and API token come
from the environment, so the command works unattended in scripts and CI.

## Scope

In scope:

- A `text` subcommand taking one positional string, drawn on the 72x16 front display.
- A `clear` subcommand removing what busyboy drew.
- Configuration from `BUSYBOY_HOST` / `BUSYBOY_TOKEN`, overridable per invocation
  with `--host` / `--token`.
- Flags on `text`: `--color`, `--font`, `--timeout`, `--scroll-rate`.

Out of scope: the back display, images, animations, audio, countdowns, element
placement flags, priority control, and any long-running or streaming mode. Those wait
until a subcommand needs them.

## Behavior

**Drawing is one-shot.** `text` sends the payload and exits. The text stays on the bar
until it is replaced, cleared, or its timeout expires — the command does not block or
clean up after itself.

**Overflow scrolls.** At the default `condensed` font roughly a dozen characters fit
across 72 px. Rather than silently truncating, every text element carries a scroll rate,
so text that doesn't fit marquees and short text sits still. `--scroll-rate 0` disables it.

**Success is silent.** Exit 0, no output, Unix-style. `--verbose` turns on request
logging for diagnosis.

**Redrawing replaces.** Every draw uses the same element id, so a second `busyboy text`
supersedes the first instead of stacking elements on the display.

## Configuration

`BUSYBOY_HOST` and `BUSYBOY_TOKEN` are both required. When either is missing the command
fails immediately with a message naming both the environment variable and the equivalent
flag — an early, actionable failure rather than a connection timeout against some default
address.

busyboy does not inherit `busylib`'s own `BUSYLIB_URL` setting or its no-argument
fallbacks. Passing both a host and a token puts `busylib` in "network" mode, which sends
the token as `X-API-Token`; leaving the host unset would silently switch it to cloud mode
and a `Bearer` header. Requiring both keeps that choice unambiguous.

## Architecture

```
busyboy text "BUILD OK" --color red --timeout 30
        |
        v
    cli.py ---- Click group, text/clear commands, exit codes
        |
        +--> config.py -- BusyboyConfig(BaseSettings): flags > env
        |                  host: str, token: SecretStr
        |
        +--> bar.py ------ open_client(config) -> BusyBar
                           build_text_payload(...) -> DisplayElements
                           draw_text(client, payload) / clear(client)
```

Three modules, each with one job and no knowledge of the others' concerns. `config.py`
imports neither Click nor busylib. `bar.py` knows nothing about argv or the environment.
`cli.py` holds no BUSY Bar payload knowledge.

### `config.py`

A single pydantic-settings model:

```python
class BusyboyConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUSYBOY_", frozen=True)

    host: str
    token: SecretStr
```

Both fields are required; a missing value raises `ValidationError`. Flags win over the
environment because pydantic-settings ranks init arguments above env sources, so the
caller passes only the flags that were actually supplied:

```python
BusyboyConfig(**{k: v for k, v in (("host", host), ("token", token)) if v is not None})
```

`SecretStr` keeps the token out of reprs, logs, and tracebacks. It is unwrapped with
`.get_secret_value()` at exactly one place: where `bar.py` constructs the client.

### `bar.py`

- `open_client(config, *, transport=None) -> BusyBar` — constructs
  `BusyBar(config.host, token=config.token.get_secret_value())`. The `transport`
  parameter is passed straight through to busylib, which accepts an
  `httpx.BaseTransport`; it exists so tests can substitute `httpx.MockTransport`.
- `build_text_payload(text, *, font, color, timeout, scroll_rate) -> DisplayElements` —
  a pure function, no I/O. This is where layout decisions live.
- `draw_text(client, payload)` and `clear(client)` — thin wrappers over
  `display_draw` and `display_clear`.

The payload:

| Field | Value | Reason |
| --- | --- | --- |
| `application_name` | `"busyboy"` | How the bar groups what an app draws |
| element `id` | `"text"` | Stable, so redraws replace rather than stack |
| `type` | `"text"` | |
| `display` | `FRONT` | The 72x16 RGB matrix |
| `font` | `"condensed"` by default | Fits more on a small display than `normal` |
| `align` | `"center"` | Centered is right for a single short string |
| `width` | `72` | Full display width, so scrolling spans it |
| `scroll_rate` | default, `--scroll-rate` overrides | Overflow marquees instead of truncating |
| `color` | omitted unless `--color` | Firmware default applies |
| `timeout` | omitted unless `--timeout` | Persists by default |

Omission matters: busylib serializes with `exclude_none`, so a field we don't set is
absent from the request body and the firmware's own default governs.

### `cli.py`

`main` becomes a `click.Group`. This replaces the current placeholder `main` command and
its `--count` option.

- `busyboy text TEXT` — options `--color`, `--font`, `--timeout`, `--scroll-rate`.
- `busyboy clear` — no options of its own.
- Both — `--host`, `--token`, `--verbose`.

`--font` uses `click.Choice` over busylib's font names (`tiny`, `small`, `normal`,
`condensed`, `bold`, `large`, `extra_large`, `global`) so invalid values are rejected at
parse time and the valid set appears in `--help`.

`--color` is a plain string, not a `click.Choice`. busylib normalizes it with pydantic's
`Color` type, which accepts CSS color names (`red`), hex (`#FF0000`, `#FF0000FF`), and
`rgb(...)` forms, then converts to `#RRGGBBAA`. An invalid color is therefore caught by
pydantic during payload construction, not by Click during parsing — so it exits 1 as a
payload validation error, not 2 as a usage error. `--help` should say "CSS color name or
hex" rather than enumerate values.

### `__init__.py`

Re-exports `main` from `cli`, leaving `[project.scripts] busyboy = "busyboy:main"`
unchanged.

## Data flow

For `busyboy text "BUILD OK" --timeout 30`:

1. Click parses argv. Usage errors — missing `TEXT`, an unknown `--font`, a non-integer
   `--timeout` — never reach busyboy code; Click reports them and exits 2.
2. `cli.py` builds a `BusyboyConfig` from flags and environment.
3. `bar.build_text_payload(...)` constructs the `DisplayElements`. Pydantic validates
   bounds (`timeout >= 0`, `scroll_rate >= 0`) and color format.
4. `bar.open_client(config)` opens the client as a context manager;
   `bar.draw_text(client, payload)` POSTs to `/api/display/draw`; the connection closes
   on exit.
5. Nothing is printed. Exit 0.

`busyboy clear` follows the same path minus payload construction, issuing DELETE
`/api/display/draw`.

## Error handling

One wrapper at the command boundary, shared by both subcommands:

| Caught | Message on stderr | Exit |
| --- | --- | --- |
| `ValidationError` from config | `Missing configuration: BUSYBOY_HOST is not set (or pass --host)` — every missing field is listed, one per line, when both are absent | 1 |
| `ValidationError` from payload | `Invalid value: <field>: <reason>` | 1 |
| `busylib.exceptions.BusyBarError` | busylib's own rendering via `format_delivery_error` | 1 |
| Click usage errors | Click's own message | 2 |

Anything else propagates as a traceback: an unexpected exception is a bug, not a user
error, and hiding it behind a friendly one-liner costs more than it saves.

A single exit code for all runtime failures is deliberate. Callers overwhelmingly branch
on zero versus non-zero, and Click already distinguishes usage errors as 2 for free.
Separate codes for auth failure versus an unreachable bar can be added when something
actually needs to tell them apart.

`--verbose` sets `logging.basicConfig(level=logging.INFO)`, which enables busylib's
request logging (method, path, auth headers masked) and prints the full traceback in
place of the one-line message.

The configuration error message names both the environment variable and the flag, since
a user hitting it may not know either exists.

## Testing

Three test modules mirroring the source split.

`tests/test_config.py` — no network, no Click. Env-only resolution; flag-only;
flag-overrides-env; missing host; missing token; token not exposed in `repr`. Uses
`monkeypatch.setenv` / `monkeypatch.delenv`.

`tests/test_bar.py` — `build_text_payload` asserted as a serialized dict: element id and
application name, `condensed` as the default font, `color` and `timeout` absent when not
passed and present when they are, `width` and `align` set, scroll rate applied. Then
`draw_text` and `clear` driven through a real `BusyBar` wired to an
`httpx.MockTransport` that captures the request — asserting POST to
`/api/display/draw` with the expected body, DELETE for clear, and the `X-API-Token`
header. Real client, real serialization, no network and no mocking of busylib internals.

`tests/test_cli.py` — Click's `CliRunner`: success is silent with exit 0; missing
configuration exits 1 with a message naming `BUSYBOY_HOST`; both variables missing lists
both; a device failure (mock transport returning 401 and 500) exits 1 with a readable
message; `busyboy text` with no argument exits 2; an unknown `--font` exits 2; an invalid
`--color` exits 1; `--help` lists both subcommands.

`tests/test_main.py` is deleted. Its `main()` call breaks once `main` is a group, and
every case it covered is subsumed above.

Coverage is configured at `fail_under = 75` over `src`. This structure should land well
above it; the only awkward branch is `--verbose` logging setup.

## Dependencies

`uv add pydantic pydantic-settings`. Both are already present transitively through
busylib, but busyboy imports them directly and should declare them rather than rely on a
transitive pin.

## Documentation

The README grows a usage section: the two commands, the two environment variables, and
one worked example. It is currently two lines.

## Open items for implementation

Two values cannot be determined from busylib's schema or its README and must be
confirmed against a real device before the defaults are fixed:

1. **`timeout` units** — seconds or milliseconds. The schema is a bare
   `int | None, ge=0`. `--timeout` should be documented in whatever unit the CLI
   exposes (seconds is the intent), converting if the firmware disagrees.
2. **`scroll_rate` units and a sensible default** — likewise a bare `int, ge=0` with no
   documented semantics. Pick the default by running it against the bar and choosing a
   speed that reads comfortably.

Treat both as an explicit implementation step: run it against the hardware, observe,
then commit the constant with a comment recording what was observed.
