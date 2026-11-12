# `busyboy claude limits` — Claude usage limits on the bar

Date: 2026-11-12
Branch: `claude-limits`

## Goal

Show the user's Claude Code usage limits on the BUSY Bar's front display and keep them fresh until the
user presses Ctrl+C: two progress bars, one for the 5-hour session limit and one for the weekly limit,
each with a reset countdown when the limit is close to exhausted.

This is a port of the "Claude Limits" app from the community gallery
(<https://maxswinkels.github.io/busybar-apps/apps/claude-limits/>, source
<https://github.com/rbhbokka/busybar-limits>), reduced to its two bars. It is built into busyboy so it
follows busyboy's conventions rather than being a separate script.

## Non-goals

- The animated mascot, its 21 animations, and the "mood" eyes.
- The back (160x80) display dashboard. busyboy only uses the front display.
- Session-activity detection (scanning `~/.claude/projects/*/*.jsonl` mtimes) and the "N SESSIONS /
  WORKING" status line.
- `--once`, `--mock`, `--mock-usage`, `--mock-auth-fail`, `--priority`, `--hold` flags.
- Refreshing or writing back the Claude OAuth token.

## Anti-goals

- Never write, log, or print the OAuth token, in any log line, error message, or traceback.
- Never let a transient failure (network, 429) kill the watch, and never let a rejected token spin
  forever silently.
- Never redraw when nothing changed; a redraw restarts element animations (see CLAUDE.md, hardware facts).

## Command surface

```
busyboy claude limits [--interval SECONDS]
                      [--host H] [--token T] [--verbose]
```

A new `claude` Typer sub-app (`claude = typer.Typer(no_args_is_help=True, help="Show Claude information
on the bar.")`, added with `main.add_typer(claude, name="claude")`) beside `gh`. `limits` is its only
subcommand. It runs until Ctrl+C, like `gh workflow`, and shares `HostOption`, `TokenOption`,
`VerboseOption`. `--interval` defaults to 180 seconds (minimum 1), matching the original app.

Startup follows the normal CLI contract: resolving the Claude token, the initial `bar.clear`, and the
first usage fetch print one line to stderr and exit 1 on failure. Once in the poll loop, behaviour
departs from the contract exactly as `gh workflow` does (see Error handling).

## Display

Full 72x16 front display. All positions below are the designer's first guess and **must be confirmed on a
real bar** with `tools/capture_screen.py` before being fixed in constants (the font-offset table in
CLAUDE.md applies to the `tiny` font: offset 1, height 5).

- Two rows, one per limit. Top = 5-hour session, bottom = weekly.
- Each row is a **track** `RectangleElement` (dark fill, e.g. `#1F1F1F`, optional border) spanning
  roughly the full width, with a **fill** `RectangleElement` inside it. Fill width is
  `round(utilization / 100 * inner_width)`, clamped to `[0, inner_width]`. A zero-width fill is omitted.
- Fill color: orange-brown `#CD6E58` at utilization <= 85%, dark red `#B3261E` above 85%.
- Reset countdown: a `TextElement` in the `tiny` font drawn over the track, **only when utilization > 75%**.
  Text is `#1A1A1A` on the orange fill and white on the red fill. Formats:
  - session: `3h59m`, or `59m` under an hour;
  - weekly: `6d` at >= 48h, `1d 5h` at >= 24h, otherwise `23h59m` / `9h05m` / `59m`.
- Elements carry fixed ids (e.g. `session-track`, `session-fill`, `session-reset`, `weekly-track`,
  `weekly-fill`, `weekly-reset`) so a redraw replaces rather than stacks. An element that disappears
  between frames (the countdown dropping below 75%) is cleared by sending the full element list on every
  draw, as `bar.draw_text` does today — the builder must verify on hardware whether a draw replaces the
  whole list or only matching ids and handle removal accordingly.
- Everything is scoped to `application_name=APPLICATION_NAME`.

## Architecture

Keep the module boundaries in CLAUDE.md.

- **`src/busyboy/claude.py` (new)**: Claude token resolution and the usage query. Knows nothing about the
  bar, Typer, git, or GitHub, and imports none of them.
  - `resolve_token()`: on macOS, `security find-generic-password -s "Claude Code-credentials" -w`;
    otherwise, or if that fails, read `~/.claude/.credentials.json`. Parse JSON, read
    `claudeAiOauth.accessToken` and `claudeAiOauth.expiresAt` (milliseconds). An expired token raises
    `ClaudeAuthError`. Subprocess stderr is discarded so a keychain diagnostic can never carry a token
    into busyboy's output (same reason as `gh auth token`).
  - `fetch_usage(token) -> Usage`: `GET https://api.anthropic.com/api/oauth/usage`, 15s timeout, headers
    `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, a `claude-code/<version>`
    `User-Agent`, `Content-Type: application/json`. Uses plain `requests` through a single `_get`-style
    helper that classifies failures. Reads `five_hour.utilization`, `five_hour.resets_at`,
    `seven_day.utilization`, `seven_day.resets_at` into a frozen `Usage` model (floats and tz-aware
    datetimes). The endpoint is **undocumented** (it is what Claude Code itself calls), so parsing must
    fail with a clear error, not a `KeyError`, if the shape changes.
- **`src/busyboy/bar.py`**: add `RectangleElement` (modelled on the vendored OpenAPI `RectangleElement`,
  `docs/superpowers/references/openapi.yaml` ~line 2118, validated as strictly as `TextElement`) and
  extend `DisplayElements` to accept it. Add pure `build_limits_payload(...)`-style helpers; no I/O.
  All bar pixel knowledge (geometry, colors, thresholds, countdown formatting inputs) stays here or in
  `watch.py`'s `render`, never in `cli.py`.
- **`src/busyboy/watch.py`**: a limits `tick`/`watch` pair (or a generalisation of the existing loop)
  that fetches usage, renders a `Screen`, diffs against the previous one, and draws only on change.
  Still the only module importing both `bar` and a data-source module (`github`, now also `claude`).
  `claude.py` and `github.py` must never import each other.
- **`src/busyboy/exceptions.py`**: add `ClaudeError(BusyboyError)` with `ClaudeAuthError` (fatal) and
  `ClaudeTransientError` (retryable) beneath it, mirroring the GitHub split, and extend
  `format_delivery_error`.
- **`src/busyboy/cli.py`**: the `claude` sub-app and `limits` command only; no payload knowledge.
- Update CLAUDE.md (architecture list, exception hierarchy, CLI contract) as part of the work.

## Error handling

Same load-bearing split as `gh workflow`:

- `ClaudeTransientError` (network failure, timeout, 429, 5xx) and `BarError`: swallowed in `tick`, logged
  at DEBUG only, display left as it was.
- `ClaudeAuthError` (no credentials found, expired `expiresAt`, 401/403): **not** caught in `tick`; it
  propagates, ends the watch, and prints one line to stderr (exit 1). Catch the two classes by name,
  never `ClaudeError`, so the retry-versus-exit distinction survives.
- Token is re-resolved on each poll rather than once at startup (**designer's call**): Claude Code
  refreshes its own token in the keychain, so a long-running watch would otherwise die on a token Claude
  Code has already replaced. An expired or missing token at poll time is still fatal.
- Staleness (**designer's call**): after a transient failure the last good bars stay up with no stale
  indicator. Acceptable because the countdown and bars are coarse (3-minute cadence).

## Testing

Follow CLAUDE.md's Testing section.

- Usage and token tests mock `requests` with `responses`; keychain access is a monkeypatched
  `subprocess.run`. **No test may depend on ambient Claude credentials** — monkeypatch `resolve_token`
  in every CLI test, as `gh workflow` tests do for `github.resolve_token`.
- Bar delivery tests go through the existing `bar_transport` fixture.
- Cover: threshold edges (75%, 85%), fill width rounding and clamping, every countdown format boundary,
  token expiry, malformed usage payload, transient vs auth failure in `tick`, the diff skipping redraws,
  and `RectangleElement` validation.
- Coverage must stay above the 75% gate; `make test` must pass.

## Resolved after builder review

- **busylib and rectangles:** the pinned busylib already ships `types.RectangleElement` and its
  `DisplayElements` union includes it, so no bump is needed. busyboy still passes a plain `dict` from its
  own stricter model, as it does for text. The plan's first task is a short spike confirming a
  rectangle-bearing dict is accepted by `display_draw` against a real bar.
- **Firmware:** busylib notes filled rectangles need device API version >= 24.3.0
  (`RECTANGLE_FILL_VERSION`). Document this requirement in CLAUDE.md; below it the bars cannot render.
- **Vanished elements (designer's call):** do not rely on a draw dropping unlisted ids. The spike also
  checks this on hardware. If a draw only replaces matching ids, the countdown is removed by drawing it
  with the same id and a blank-safe alternative, or by `bar.clear` then redraw (acceptable here: bars have
  no animations to restart). The plan must pick based on the spike's result.
- **Draw order (designer's call):** z-order follows list order, so each countdown comes after its fill.
- **Token sources (designer's call):** fall through from keychain to the credentials file only when the
  keychain entry is missing or the `security` command fails, never on an expired token. If both sources
  are missing, raise `ClaudeAuthError`.
- **Usage payload (confirmed against a real response on 2026-11-12):** `five_hour.utilization` and
  `seven_day.utilization` are floats on a **0-100 scale** (observed 3.0 and 26.0). `resets_at` is an ISO 8601
  string with a UTC offset (`2026-11-12T20:29:59.606009+00:00`) with fractional seconds. The response
  carries many other fields (a `limits` list, `spend`, `extra_usage`, and a number of obscurely named null
  keys); busyboy reads only the four fields above and must ignore everything else, including unknown
  keys. Whether `resets_at` can ever be null (e.g. an unused window) was not observed; treat null as "no
  countdown" for that bar rather than an error (designer's call).

## Left to the builder

- Exact pixel geometry and fonts (confirm on hardware with `tools/capture_screen.py`).
- Whether to generalise `watch.py`'s loop or add a sibling loop, as long as the module boundaries hold.
- Verifying the bar's draw semantics for removing a vanished element (see Display).
- The `User-Agent` version string and the precise `Usage` model shape.
