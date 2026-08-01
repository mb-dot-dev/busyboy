# Replace `busylib` with `requests` — design

**Date:** 2026-08-01
**Branch:** `feat/replace-busylib-with-requests`

## Goal

busyboy currently reaches the BUSY Bar through `busylib` (`git+https://github.com/busy-app/busylib-py`),
which wraps the bar's HTTP API over `httpx`. This design replaces that dependency with a direct integration
built on `requests`, using the bar's own OpenAPI spec (`http://busybar.infra.home/openapi.yaml`) as the
reference for field names, patterns, and enums. Tests move from `httpx.MockTransport` to `responses`.

This is an internal swap, not a feature change: `busyboy text` and `busyboy clear` keep the exact same
flags, defaults, and observable behavior described in
`docs/superpowers/specs/2026-08-01-text-subcommand-design.md`. Nothing here alters the CLI contract, the
payload the bar receives, or the README.

## Scope

In scope:

- `bar.py` talks to `POST`/`DELETE /api/display/draw` directly via `requests`, replicating the parts of
  busylib's behavior busyboy actually depends on:
  - URL scheme normalization (bare host/IP defaults to `http://`).
  - `X-API-Token` header sent when a token is configured, omitted otherwise.
  - Retrying transport-level failures (connection errors, timeouts) up to 2 extra attempts with
    `0.25 * (attempt + 1)` second backoff. HTTP error responses (4xx/5xx) are not retried.
  - Mapping HTTP error responses and exhausted-retry transport failures to a small exception hierarchy.
- A new `busyboy/exceptions.py`, replacing `busylib.exceptions` for the types `cli.py` catches.
- Hand-written pydantic models for exactly what busyboy sends: `DisplayElements` and `TextElement` (plus
  the font `Literal`), checked against the OpenAPI spec's schema definitions — not generated from it.
- `--color` keeps accepting CSS names and hex via `pydantic_extra_types.color.Color`, now a direct
  dependency instead of a transitive one through busylib.
- Test suite ported from `httpx.MockTransport` to `responses`.
- A vendored copy of the fetched spec at `docs/superpowers/references/openapi.yaml`.
- `CLAUDE.md` updated: the project description, the Architecture section, and the Gotchas section (one
  gotcha removed because it no longer applies, one corrected because the installed busylib version's actual
  retry behavior differs from what's currently documented).

Out of scope, same as before: every other busylib-covered endpoint (audio, assets, account, wifi, storage,
BLE, etc.) — busyboy never called them and still won't. Response-body modeling stays minimal: busyboy
already discards `SuccessResponse.result` on success, so there is no pydantic model for it — only the HTTP
status code is checked. `config.py` is untouched; it has never imported busylib.

## Architecture

```
busyboy text "BUILD OK" --color red --timeout 30
        |
        v
    cli.py ---- Click group, text/clear commands, exit codes
        |
        +--> config.py -- unchanged: BusyboyConfig, host/token resolution
        |
        +--> bar.py ------ payload construction (unchanged) +
        |                  requests.Session, retry/backoff, auth header,
        |                  draw_text(config, payload) / clear(config)
        |
        +--> exceptions.py - BarError, BarAPIError, BarRequestError,
                             format_delivery_error()
```

`bar.py` keeps its current job description from CLAUDE.md — "this is where BUSY Bar knowledge lives" — and
keeps owning both payload construction and delivery, exactly as it does today with busylib. No new module
boundary is introduced for the transport layer; splitting transport from payload into a separate `client.py`
was considered and rejected as premature given busyboy talks to exactly one endpoint pair today (see
"Alternatives considered" below).

**`open_client` is removed.** busylib required an explicit client object (`BusyBar(...)`) with a
context-manager lifecycle, which is what produced the documented `ty` gotcha (`SyncClientBase.__enter__`
returning `SyncClientBase` instead of `Self`). `requests` doesn't need a comparable call-site-visible
object — `draw_text(config, payload)` and `clear(config)` open a `requests.Session` internally (or reuse a
module-level one) and there is no borrowed context-manager type to fight with `ty` over. This gotcha is
deleted from CLAUDE.md rather than corrected, since the underlying situation it described no longer exists.
`cli.py`'s call sites simplify accordingly, from `client = bar.open_client(config); with client:
bar.draw_text(client, payload)` down to a direct `bar.draw_text(config, payload)` (and likewise for
`clear`).

`exceptions.py` mirrors only what `cli.py` needs from `busylib.exceptions`:

| Type | Replaces | Raised when |
| --- | --- | --- |
| `BarError` | `BusyBarError` | Base class; everything below inherits from it |
| `BarAPIError` | `BusyBarAPIError` | HTTP response status >= 400. Carries status code and, when the body parses as JSON matching the `Error` schema, its `error`/`code` fields; falls back to the raw response text otherwise |
| `BarRequestError` | `BusyBarRequestError` | A `requests.exceptions.RequestException` (connection error, timeout) on the final attempt, after retries are exhausted |

`format_delivery_error(error)` renders either type as the one-line message `cli.py` prints to stderr,
replacing `busylib.exceptions.format_delivery_error`. `cli.py`'s `_handle_errors` wrapper changes its
`except exceptions.BusyBarError` clause to `except exceptions.BarError`, using the new module.

## Data model

Hand-written pydantic models, scoped to exactly what busyboy sends — not the full `DisplayElement`
discriminated union from the spec (`image`/`animation`/`countdown`/`rectangle` stay unmodeled, since
busyboy never constructs them):

- `DisplayFontName` — `Literal["tiny", "small", "normal", "condensed", "bold", "large", "extra_large",
  "global"]`, matching `TextElement.font`'s enum in the spec.
- `DisplayName` — `Literal["front", "back"]`, matching `DisplayElement.display`'s enum.
- `TextElement` — mirrors the spec's `TextElement` (`allOf` over `DisplayElement` + text-specific fields):
  `id`, `text` (`min_length=1`), `font`, `color` (`#RRGGBBAA` pattern, normalized from CSS/hex input before
  assignment — same job `_utils.normalize_rgba_color` does in busylib today), `timeout` (`ge=0`), `display`,
  `y`, `width` (`ge=1`), `scroll_rate` (`ge=0`). `type` is `Literal["text"] = "text"` — always present in the
  serialized wire payload (the API requires it as its discriminator field) but not a configurable input,
  matching busylib's own `TextElement.type` today. `x` and `align` are not modeled as fields at all — busyboy
  never sets them and relies on the spec's `x=0` default (matches current `build_text_payload` scope).
- `DisplayElements` — `application_name`, `elements: list[TextElement]` (narrowed from the spec's
  `oneOf` union since busyboy only ever sends text elements).

This preserves every validation `build_text_payload` currently performs (color format, non-negative
timeout/scroll_rate, non-empty text) and keeps `build_text_payload` itself pure and I/O-free, unchanged from
its current signature and behavior.

## Error handling & retries

Replicates the retry semantics of the installed `busylib`'s
`SyncClientBase.execute_prepared_request`: **only** `requests.exceptions.RequestException` triggers a retry
(connection errors, timeouts) — up to `max_retries=2` extra attempts, sleeping `0.25 * (attempt + 1)`
seconds between them. An HTTP response with status >= 400 raises `BarAPIError` immediately, with no retry
and no sleep.

This corrects CLAUDE.md's current gotcha text, which states busylib "retries `408, 429, 500, 502, 503,
504`" — that describes `busylib.exceptions.is_retryable_delivery_error`, a classification helper that exists
in the installed library but is never actually called anywhere in its request-execution path. The real
behavior, confirmed by reading `client/base.py`, is: HTTP error statuses are never retried, regardless of
code; only network-level `RequestError`s are. The corrected CLAUDE.md text will state this plainly and drop
the reference to the unused helper, so a future reader doesn't waste time looking for retry-on-500 behavior
that was never actually wired up.

A default request timeout matching busylib's (`10s` overall, `5s` connect) is set via `requests`' timeout
tuple: `timeout=(5, 10)`.

## Testing

`responses` replaces `httpx.MockTransport`, keeping the existing principle: drive the real `bar.py`
functions against fake HTTP responses, not mocked internals.

- **`tests/test_bar.py`** — `build_text_payload` tests are unchanged (pure function, no transport
  involved). Delivery tests use `responses.activate`/`RequestsMock` to register
  `POST`/`DELETE http://10.0.4.20/api/display/draw`, asserting method, URL, `X-API-Token` header, and JSON
  body via `responses.calls`. A **401** response (not 500 — still avoids sleeping through retries, same
  reasoning as today) exercises `BarAPIError`. A new test registers a connection-error response on every
  attempt to exercise `BarRequestError` after retries exhaust — this is new coverage the old suite couldn't
  easily express with `httpx.MockTransport`, and is worth adding since retry/backoff is now hand-rolled
  logic instead of an inherited library behavior.
- **`tests/test_cli.py`** — same `Recorder`-style fixture and test cases, rebuilt on `responses`'s registry
  instead of monkeypatching `bar.open_client` (which no longer exists) — the fixture instead activates
  `responses` and registers the mock handler directly.
- **`tests/test_config.py`** — unchanged.
- Coverage target (`fail_under = 75`, currently ~95%) is expected to hold; the retry-path branches are the
  main new surface, covered by the added connection-error test.

## Dependencies

`pyproject.toml`:

- Remove `busylib @ git+https://github.com/busy-app/busylib-py`.
- Add `requests` and `pydantic-extra-types` (direct now; previously transitive through busylib) to
  `[project.dependencies]`.
- Add `responses` and `types-requests` to the `dev` dependency group (`types-requests` for `ty check`,
  since `requests` itself ships incomplete type information).

## Documentation

`CLAUDE.md` describes busylib in six places. Each needs an explicit change, not a blanket "update as needed"
— the implementation plan should treat this as its own checklist:

- **Project** (the opening paragraph): drop "It depends on `busylib` ..., which wraps the bar's HTTP API,"
  replace with a line naming the direct `requests` integration and the OpenAPI spec as its reference.
- **Architecture**: the intro sentence ("`config.py` imports neither Click nor busylib...") loses the
  busylib clause; the `bar.py` bullet drops `open_client` and gains a mention of the `requests.Session`
  transport and retry logic it now owns directly; a new bullet is added for `src/busyboy/exceptions.py`.
- **CLI contract**: "`--verbose` enables busylib request logging" needs a real replacement — the
  implementation plan must pick a concrete mechanism (e.g. `http.client.HTTPConnection.debuglevel = 1`, or
  `logging.getLogger("urllib3")` at `DEBUG`) and document whichever one is actually wired up, rather than
  leaving a vague claim. The paragraph describing busyboy always putting busylib in "network mode" is
  rewritten without that vocabulary: busyboy always builds the URL from `host` and sends `X-API-Token` only
  when a token is configured — no client "mode" concept exists anymore.
- **Gotchas**:
  - Delete "busylib's context manager loses the subtype" outright — the situation it describes no longer
    exists once `open_client` is gone.
  - Delete or rewrite "busylib logs API failures itself, at error level" — `requests`/`urllib3` does not
    log HTTP error responses at error level the way busylib did, so the CRITICAL-level silencing this
    gotcha describes has no direct equivalent. Confirm this during implementation (don't just assume) and
    either remove the gotcha or replace it with whatever's actually needed once real request logging is
    observed under `--verbose`.
  - Update "Ruff's `force-sort-within-sections` sorts `import x` and `from x import y` together" — its
    example (`from busylib import ...` before `import click`) references a module that no longer exists;
    replace with a real import ordering example from the new code (e.g. `busyboy.exceptions` /
    `requests`).
  - Update "`click.Choice` returns `str`, so passing it to a busylib `Literal` field needs a `cast`" — the
    `Literal` is now busyboy's own `DisplayFontName`, not busylib's; drop "busylib" from the wording.
  - Unaffected, keep as-is: "Unpacking into `BaseSettings` needs `dict[str, Any]`" and "Never let pydantic's
    `ValidationError` into a traceback a user sees" — both are about `config.py`, which never imported
    busylib.
- **Testing**: rewrite the opening paragraph for `responses` instead of `httpx.MockTransport`, drop the
  "`bar.open_client` exposes `transport=` for exactly this reason" sentence (that parameter is gone), and
  drop the "mock responses must match `SuccessResponse`'s shape" bullet — since busyboy no longer models or
  validates the response body at all (see Scope), mock responses only need a 2xx/4xx status, not a
  particular JSON shape. The retry-behavior correction is covered above. The
  `tests/test_cli.py` bullet about monkeypatching `cli.bar.open_client` is rewritten to describe the
  `responses`-registry-based fixture instead.
- **Hardware facts**: the measured facts themselves (display dimensions, `scroll_rate`/`timeout` units,
  the `align` and `application_name` gotchas) are unaffected — they're properties of the bar's firmware, not
  of busylib. Only the framing sentence "busylib's schema documents none of this" is reworded to reference
  the OpenAPI spec directly.
- **Testing against a real bar**: this section currently points at `BusyBar.screen(0)` for reading back
  frames during hardware calibration — a busylib method. Once busylib is removed from `pyproject.toml`, that
  method is no longer available in the project's own environment. busyboy's CLI never called `screen()`
  itself (it's a manual verification technique, not shipped code), so reimplementing `/api/screen` reading is
  out of scope for this change. The section is updated to say so plainly: future frame-capture verification
  needs either a scratch `pip install busylib` outside the project's lockfile, or a small standalone script
  reimplementing the base64/RGB888 decode against `/api/screen` directly. This is a real (if narrow) loss of
  a previously available dev convenience, called out here so it isn't discovered by surprise later.
- **`README.md`**: no change expected — the CLI's user-facing behavior is identical.
- **`docs/superpowers/references/openapi.yaml`**: vendored copy of the fetched spec (already saved as part
  of this design session), so the schema that justified field names, patterns, and enums survives if the
  bar's firmware or that URL changes, and is reviewable by anyone without home-network access.

## Alternatives considered

**Codegen from the OpenAPI spec** (e.g. `datamodel-code-generator`) instead of hand-written models — rejected.
The spec covers ~30 endpoints and a discriminated union of 5 display element types; busyboy uses exactly one
endpoint pair and one element type. Generating the full schema would produce far more code than busyboy
needs and add a codegen dependency and a generated-file-drift problem, for no benefit over hand-writing the
two models actually used — consistent with how `bar.py` already hand-writes its payload today.

**Splitting transport into its own `client.py`** module, separate from `bar.py`'s payload construction —
rejected for now. It would cleanly separate "how to talk HTTP to the bar" from "what a busyboy payload looks
like," and would be the natural choice if busyboy grows more endpoints. But CLAUDE.md already frames `bar.py`
as owning all BUSY Bar knowledge including the client (`open_client` already lived there wrapping busylib),
and busyboy calls exactly one endpoint pair — introducing a new module boundary for that is speculative
ahead of an actual second endpoint, which the project's own stated engineering principles argue against.

**Dropping retries entirely** (single attempt, fail fast) — rejected. Kept to preserve the existing
resilience to transient USB/WiFi hiccups between busyboy and the bar; a script invoking `busyboy text` in a
CI job or a hook shouldn't fail on one blip that a quarter-second retry would have absorbed.

**Restricting `--color` to hex only** (`#RRGGBB`/`#RRGGBBAA`), dropping the CSS-color-name dependency —
rejected. `--color red` is documented in the README and covered by existing tests; changing it would be a
user-facing breaking change with no upstream driver (the OpenAPI spec's pattern is a wire-format constraint
on the bar's side, not a CLI input constraint busyboy needs to inherit).
