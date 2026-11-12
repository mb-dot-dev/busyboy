# `busyboy claude limits` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `busyboy claude limits` keeps two progress bars (5-hour session, weekly) of the user's Claude usage on the BUSY Bar front display until Ctrl+C.

**Architecture:** A new `claude.py` resolves the Claude Code OAuth token and fetches usage (no knowledge of the bar). `bar.py` gains a `RectangleElement` and a pure `build_limits_payload`. `watch.py` gains a limits poll loop mirroring the workflow loop (fetch, render a frozen `LimitsScreen`, redraw only on change). `cli.py` adds a `claude` sub-app beside `gh`.

**Tech Stack:** Python 3.14, Typer, pydantic, `requests` + `responses` (Anthropic API), busylib + `bar_transport` fixture (bar), pytest, `uv`/`make`.

**Spec:** `docs/superpowers/specs/2026-11-12-claude-limits-design.md`

## Global Constraints

- Python >=3.14; `except A, B:` without parentheses is correct and required by `ruff format` (CLAUDE.md gotcha).
- Module boundaries: `claude.py` imports none of `bar`, `github`, `git`, `watch`, Typer; `claude.py` and `github.py` never import each other; `watch.py` is the only module importing both `bar` and a data source; `cli.py` holds no payload knowledge.
- Never write, log, or print the OAuth token; subprocess stderr is discarded; `ClaudeError`s never embed it.
- Catch `ClaudeTransientError` and `ClaudeAuthError` by name, never `ClaudeError`, in the poll path.
- Fill color `#CD6E58` at utilization <= 85, `#B3261E` above 85; countdown only above 75; `utilization` is a 0-100 float.
- Countdown text `#1A1A1A` on the orange fill, `#FFFFFF` on the red; font `tiny`.
- Default `--interval` 180, minimum 1. Usage request timeout 15s. Token re-resolved every poll.
- `bar.RectangleElement` mirrors the vendored OpenAPI schema (`docs/superpowers/references/openapi.yaml` ~L2118); filled rectangles need device API >= 24.3.0.
- A draw replaces elements by id and does **not** remove omitted ones (stated in `watch.watch`'s docstring); hide a vanished countdown by drawing it as a single space `" "`.
- All elements scoped to `application_name=APPLICATION_NAME`.
- `make test` (ruff, ruff format --check, ty, pytest, 75% branch coverage) must pass; no test may depend on ambient Claude credentials.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

- `resets_at` null or missing on a window: that bar renders with no countdown, no crash (Task 5).
- `resets_at` already in the past (clock skew, stale response): countdown reads `0m`, never negative (Task 5).
- Utilization below 0 or above 100: fill clamps to the track, never a negative or oversized rectangle (Task 4).
- Usage response with unknown extra keys, wrong types, or a non-JSON body: ignored extras; malformed raises `ClaudeTransientError`, never `KeyError`/`ValidationError` (Task 3).
- Countdown crossing back under 75% between polls: the old text is blanked, not left stuck on the panel (Task 4, 5).

---

### Task 1: Hardware spike (throwaway, not committed)

**Files:** scratch script in the scratchpad directory only; findings go into the constants of Task 4.

Goal: prove the layout and draw semantics on a real bar before building on them. Needs `source ~/.zshrc && busybarenv` for `BUSYBOY_HOST`/`BUSYBOY_TOKEN`. Never print the token.

- [ ] **Step 1:** In a scratch script, use `busylib.BusyBar` directly to `display_draw` a plain dict with `application_name="busyboy"` and two rectangles (`type: "rectangle"`, `fill: "solid"`, `fill_colors: ["#1F1F1FFF"]`, `border_width: 0`) plus one `tiny` text element on top. Record whether busylib accepts the dict and whether the bar renders it.
- [ ] **Step 2:** Run `uv run --frozen python tools/capture_screen.py` and confirm the first-guess geometry in Task 4 (track `x=1 w=70 h=7` at `y=1` and `y=9`; tiny text at `y=track_y`, inking rows track_y+1..+5). Adjust the constants in Task 4 if text clips or sits off-centre. Confirm `1d 5h` fits.
- [ ] **Step 3:** Draw the text element again with text `" "` and the same id; confirm the old text disappears. If it does not, switch Task 4/5 to `bar.clear` then redraw whenever a countdown disappears.
- [ ] **Step 4:** Check the bar's firmware API version is >= 24.3.0 (busylib `BusyBar.version()` or the web UI). Report findings to the user; no commit.

### Task 2: Claude exception hierarchy

**Files:**
- Modify: `src/busyboy/exceptions.py`
- Test: `tests/test_exceptions.py`

**Interfaces:**
- Produces: `class ClaudeError(BusyboyError)`, `class ClaudeAuthError(ClaudeError)`, `class ClaudeTransientError(ClaudeError)` with `__init__(self, message: str, *, retry_after: float | None = None)` storing `retry_after`.

- [ ] **Step 1: Write failing tests** `test_claude_auth_error_is_not_transient`, `test_claude_transient_error_carries_retry_after` (`ClaudeTransientError("x", retry_after=30).retry_after == 30`), and `test_claude_errors_render_as_their_message` (`format_delivery_error(ClaudeAuthError("expired")) == "expired"`). Also assert neither Claude class is a `GitHubError` subclass.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_exceptions.py -v`; expect FAIL (AttributeError).
- [ ] **Step 3: Implement** the three classes in `exceptions.py` with docstrings mirroring the GitHub trio, including the "never catch the shared parent" warning. `format_delivery_error` needs no change (falls through to `str(error)`).
- [ ] **Step 4: Run** the same command; expect PASS.
- [ ] **Step 5: Commit** `feat: add Claude exception hierarchy`.

### Task 3: `claude.py` — token and usage

**Files:**
- Create: `src/busyboy/claude.py`
- Test: `tests/test_claude.py`

**Interfaces:**
- Consumes: `exceptions.ClaudeError`, `ClaudeAuthError`, `ClaudeTransientError`.
- Produces:
  - `class Window(BaseModel, frozen=True)`: `utilization: float`, `resets_at: datetime | None = None` (tz-aware).
  - `class Usage(BaseModel, frozen=True)`: `session: Window`, `weekly: Window`.
  - `def resolve_token(*, now: datetime | None = None) -> str` — keychain first on `sys.platform == "darwin"` (`security find-generic-password -s "Claude Code-credentials" -w`, `capture_output=True`, stderr discarded, 10s timeout), then `~/.claude/.credentials.json`. Parse JSON, read `claudeAiOauth.accessToken` and `claudeAiOauth.expiresAt` (ms epoch). Fall through to the file only when the keychain entry is missing or the command fails/times out; an expired token raises `ClaudeAuthError` and never falls through. Nothing found raises `ClaudeAuthError("No Claude Code credentials found. Run `claude` to log in.")`.
  - `def fetch_usage(token: str) -> Usage` — `GET https://api.anthropic.com/api/oauth/usage` via `requests`, timeout 15, headers `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, `User-Agent: claude-code/2.1.220`, `Content-Type: application/json`. 401/403 -> `ClaudeAuthError`; 429 (with `Retry-After` seconds parsed into `retry_after`), 5xx, `requests.RequestException`, non-JSON body, or missing/mistyped `five_hour`/`seven_day` fields -> `ClaudeTransientError`; any other 4xx -> `ClaudeError`. Maps `five_hour`->`session`, `seven_day`->`weekly`; ignores every other key. Never include the token in a message.

- [ ] **Step 1: Write failing tests** with `responses` and a monkeypatched `subprocess.run`/`Path.home`:
  - `test_fetch_usage_parses_the_real_payload_shape` using `{"five_hour": {"utilization": 3.0, "resets_at": "2026-11-12T20:29:59.606009+00:00", "limit_dollars": None}, "seven_day": {"utilization": 26.0, "resets_at": "2026-11-15T01:59:59.606027+00:00"}, "tangelo": None, "limits": []}`; assert `usage.session.utilization == 3.0`, tz-aware `resets_at`.
  - `test_fetch_usage_sends_the_required_headers` (asserts all four headers).
  - `test_null_resets_at_is_allowed`.
  - parametrized status test: 401/403 -> `ClaudeAuthError`; 429 (with and without `Retry-After: 30`), 500, 503 -> `ClaudeTransientError` (assert `retry_after`); 404 -> `ClaudeError` that is neither subclass.
  - `test_connection_error_is_transient`, `test_non_json_body_is_transient`, `test_missing_five_hour_is_transient`.
  - `test_errors_never_contain_the_token`.
  - Token: `test_keychain_token_is_used_on_macos`, `test_missing_keychain_entry_falls_back_to_credentials_file`, `test_expired_keychain_token_does_not_fall_through` (file has a valid token, still raises), `test_expired_file_token_raises`, `test_no_credentials_anywhere_raises_auth_error`, `test_non_macos_skips_the_keychain` (patch `sys.platform`), `test_malformed_credentials_json_raises_auth_error`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_claude.py -v`; expect FAIL (no module).
- [ ] **Step 3: Implement** `claude.py` following `github._token_from_gh` / `_get` for structure (`except OSError, subprocess.TimeoutExpired:`). Use `now` injection only for expiry tests.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_claude.py -v`; expect PASS.
- [ ] **Step 5: Commit** `feat: fetch Claude usage limits`.

### Task 4: `bar.py` — rectangles and the limits layout

**Files:**
- Modify: `src/busyboy/bar.py`
- Test: `tests/test_bar.py`

**Interfaces:**
- Produces:
  - `class RectangleElement(BaseModel)`: `id: str`, `type: Literal["rectangle"] = "rectangle"`, `display: DisplayName = "front"`, `x: int = 0`, `y: int = 0`, `width: int = Field(ge=1)`, `height: int = Field(ge=1)`, `radius: int = Field(default=0, ge=0)`, `fill: Literal["none","solid","gradient_h","gradient_v"] = "solid"`, `fill_colors: list[str] = Field(min_length=1, max_length=2)` (each `^#[0-9A-F]{8}$` after the same normalisation as `TextElement.color`), `border_width: int = Field(default=0, ge=0)` (deliberately 0, unlike the vendor default of 1).
  - `DisplayElements.elements: list[TextElement | ImageElement | RectangleElement]`.
  - `@dataclasses.dataclass(frozen=True) class LimitRow: percent: float; countdown: str | None`.
  - `def build_limits_payload(*, session: LimitRow, weekly: LimitRow) -> DisplayElements`.
  - Constants (first-guess geometry, adjusted per Task 1): `LIMITS_TRACK_X = 1`, `LIMITS_TRACK_WIDTH = 70`, `LIMITS_TRACK_HEIGHT = 7`, `LIMITS_SESSION_Y = 1`, `LIMITS_WEEKLY_Y = 9`, `LIMITS_TRACK_COLOR = "#1F1F1F"`, `LIMITS_FILL_COLOR = "#CD6E58"`, `LIMITS_HIGH_FILL_COLOR = "#B3261E"`, `LIMITS_HIGH_THRESHOLD = 85`, `LIMITS_COUNTDOWN_THRESHOLD = 75`, `LIMITS_COUNTDOWN_X = 3`, element ids `session-track`, `session-fill`, `session-reset`, `weekly-track`, `weekly-fill`, `weekly-reset`.
- Behaviour: for each row emit track, then fill (omitted when width rounds to 0), then countdown, in that z-order. Fill width = `round(percent / 100 * LIMITS_TRACK_WIDTH)` clamped to `[0, LIMITS_TRACK_WIDTH]`. Fill color by `percent > 85`. Countdown element: `tiny` font at `y = track_y` (ink rows track_y+1..+5), text = `row.countdown` when `row.countdown is not None and percent > 75`, otherwise `" "`; color `#1A1A1A` on orange, `#FFFFFF` on red. Countdown text passes through `_to_displayable_ascii`.

- [ ] **Step 1: Write failing tests:**
  - `test_rectangle_element_defaults_to_no_border` and `test_rectangle_element_rejects_zero_width`.
  - `test_rectangle_serialises_for_the_wire` (`model_dump(exclude_none=True)` has `type == "rectangle"`, `fill == "solid"`, `border_width == 0`).
  - Parametrized fill width: 0 -> no fill element; 50 -> 35; 100 -> 70; 150 -> 70; -5 -> no fill element; 3.0 -> 2.
  - Parametrized color edge: 85 orange, 85.1 red.
  - Parametrized countdown edge: 75 -> `" "`, 75.1 with `"3h59m"` -> `"3h59m"`; countdown `None` above 75 -> `" "`.
  - `test_elements_have_stable_ids_and_draw_order` (fill index > track index, countdown last per row).
  - `test_limits_payload_is_scoped_to_the_application`.
  - `test_countdown_text_color_matches_fill` (dark on orange, white on red).
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_bar.py -v`; expect new tests FAIL.
- [ ] **Step 3: Implement** the model, union extension, `LimitRow`, constants, and `build_limits_payload` in `bar.py` with doc comments citing the Task 1 measurements.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_bar.py -v`; expect PASS (existing tests unchanged).
- [ ] **Step 5: Commit** `feat: add rectangle elements and the limits layout`.

### Task 5: `watch.py` — limits poll loop

**Files:**
- Modify: `src/busyboy/watch.py`
- Test: `tests/test_watch.py`

**Interfaces:**
- Consumes: `claude.resolve_token`, `claude.fetch_usage`, `claude.Usage`, `bar.LimitRow`, `bar.build_limits_payload`, `exceptions.ClaudeTransientError`, `exceptions.BarError`.
- Produces:
  - `DEFAULT_LIMITS_INTERVAL_SECONDS = 180`.
  - `def format_session_countdown(remaining: timedelta) -> str` — `"3h59m"` at >=1h (minutes zero-padded to 2), else `"59m"`; non-positive -> `"0m"`.
  - `def format_weekly_countdown(remaining: timedelta) -> str` — `>=48h` -> `"6d"`; `>=24h` -> `"1d 5h"`; `>=1h` -> `"23h59m"` / `"9h05m"`; else `"59m"`; non-positive -> `"0m"`.
  - `@dataclasses.dataclass(frozen=True) class LimitsScreen: session: bar.LimitRow; weekly: bar.LimitRow`.
  - `def render_limits(usage: claude.Usage, now: datetime) -> LimitsScreen` — countdown is `None` when `resets_at` is `None`.
  - `@dataclasses.dataclass(frozen=True) class LimitsTickResult: screen: LimitsScreen | None; retry_after: float | None = None`.
  - `def limits_tick(config: BusyboyConfig, previous: LimitsScreen | None, *, now: datetime | None = None) -> LimitsTickResult`. Resolves the token and fetches usage each call; swallows `ClaudeTransientError` (carrying `retry_after`) and `BarError` at DEBUG; lets `ClaudeAuthError` (and base `ClaudeError`) propagate; skips `bar.draw_text` when the rendered screen equals `previous`.
  - `def watch_limits(config: BusyboyConfig, *, interval: int = DEFAULT_LIMITS_INTERVAL_SECONDS, sleep: Callable[[float], None] | None = None) -> None` — same `sleep` default-`None` pattern, initial `bar.clear`, `wait = max(interval, retry_after)`, `KeyboardInterrupt` clean exit, final `bar.clear` swallowing `BarError`. Extract the shared loop/cleanup from `watch` into a private helper rather than copying it, keeping all existing `watch` tests green.

- [ ] **Step 1: Write failing tests:** countdown boundaries parametrized for both formatters (`59m`, `60m`, `3h05m`, `23h59m`, exactly 24h, 47h59m, exactly 48h, 0 and negative); `test_render_limits_with_null_resets_at_has_no_countdown`; `test_render_limits_with_past_resets_at_reads_0m`; `test_render_limits_below_threshold_still_carries_a_countdown_value` (thresholding is `bar`'s job); `test_limits_tick_draws_on_first_poll` and `test_limits_tick_skips_the_draw_when_unchanged` (via `bar_transport`, asserting draw call count); `test_limits_tick_swallows_transient_failure_and_keeps_previous`; `test_limits_tick_propagates_auth_error`; `test_limits_tick_swallows_bar_error`; `test_limits_tick_resolves_the_token_every_call` (monkeypatched `claude.resolve_token` counter); `test_countdown_dropping_below_threshold_redraws_a_blank` (two ticks, second draw body contains `" "` for the countdown id); `test_watch_limits_clears_on_start_and_on_interrupt`; `test_watch_limits_honours_retry_after` (sleep receives `max(interval, retry_after)`); `test_watch_limits_ends_on_auth_error_after_clearing`. Monkeypatch `claude.resolve_token` and mock the usage URL with `responses`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_watch.py -v`; expect new tests FAIL.
- [ ] **Step 3: Implement** the functions in `watch.py`; import `claude` alongside `bar`/`github`.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_watch.py -v`; expect PASS including all pre-existing tests.
- [ ] **Step 5: Commit** `feat: poll and draw Claude limits`.

### Task 6: `cli.py` — the `claude limits` command

**Files:**
- Modify: `src/busyboy/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `claude.resolve_token`, `watch.watch_limits`, `watch.DEFAULT_LIMITS_INTERVAL_SECONDS`.
- Produces: `claude = typer.Typer(no_args_is_help=True, help="Show Claude information on the bar.")` added with `main.add_typer(claude, name="claude")`; `@claude.command() @_handle_errors def limits(interval: Annotated[int, typer.Option(min=1, help="Seconds between polls.")] = watch.DEFAULT_LIMITS_INTERVAL_SECONDS, host: HostOption = None, token: TokenOption = None, verbose: VerboseOption = False) -> None`, docstring "Watch Claude usage limits on the bar until Ctrl+C."

Body: `_configure_logging`, `load_config`, call `claude.resolve_token()` once up front so missing/expired credentials fail at startup with one line and exit 1 (the poll loop re-resolves per tick), then `watch.watch_limits(config, interval=interval)`. Import `claude` in the existing `from busyboy import ...` line.

- [ ] **Step 1: Write failing tests** (monkeypatch `cli.claude.resolve_token`; in the existing autouse fixture style, never touch real credentials): `test_claude_help_lists_limits`; `test_limits_runs_until_interrupt_and_clears` (patch `time.sleep` to raise `KeyboardInterrupt`, register usage via `responses` and draw/clear via `bar_transport`, exit code 0); `test_limits_without_credentials_exits_1_with_one_line` (`resolve_token` raises `ClaudeAuthError`; stderr one line; exit 1); `test_limits_auth_rejected_mid_watch_exits_1`; `test_limits_interval_below_one_is_a_usage_error` (exit 2); `test_limits_host_and_token_overrides_reach_the_bar`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_cli.py -v`; expect new tests FAIL.
- [ ] **Step 3: Implement** the sub-app and command as specified above.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_cli.py -v`; expect PASS.
- [ ] **Step 5: Commit** `feat: add busyboy claude limits`.

### Task 7: Documentation and full verification

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1:** Update CLAUDE.md: project blurb and module count ("Eight modules"), a `claude.py` bullet, `bar.py` bullet (rectangles, limits layout), `watch.py` bullet (limits loop), `exceptions.py`/hierarchy section (`ClaudeError` split and why `ClaudeAuthError` is not caught in `limits_tick`), CLI contract (`busyboy claude limits` departs from the one-shot contract like `gh workflow`; the Claude token comes from the keychain then `~/.claude/.credentials.json` and is read-only), a Hardware-facts bullet for the firmware >= 24.3.0 rectangle requirement and the measured limits geometry, and a Gotcha that the usage endpoint is undocumented and returns `utilization` on a 0-100 scale with many unrelated keys that must be ignored.
- [ ] **Step 2: Run** `make test`; expect lint clean and all tests passing with coverage >= 75%.
- [ ] **Step 3: CI-like check:** `env -u GITHUB_TOKEN PATH=<dir with a failing gh>:$PATH uv run --frozen pytest`; expect PASS (no ambient auth dependence).
- [ ] **Step 4: Real-bar check:** run `uv run --frozen busyboy claude limits` against the bar; confirm the bars match the Claude usage dashboard, Ctrl+C clears the display. Report to the user.
- [ ] **Step 5: Commit** `docs: document claude limits`.
