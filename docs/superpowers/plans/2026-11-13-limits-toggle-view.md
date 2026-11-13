# `claude limits` toggle view Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `busyboy claude limits` shows one limit at a time (5-hour by default) as a short text label over one bar, and the bar's Start/Pause button toggles it to the weekly limit and back.

**Architecture:** `bar.py` gets a one-limit layout (`LimitView`) and a `StartButtonListener` (a daemon thread running busylib's async status stream, signalling presses through a `threading.Event`). `watch.py` renders the selected limit, and its sleep becomes a wait that wakes on a press; a press redraws from the last fetched usage with no new API call. The poll loop stays synchronous.

**Tech Stack:** Python >=3.14, typer, busylib (`AsyncBusyBar.stream_status_ws`, `busylib.features.input_events`), pytest, `make test`.

**Spec:** `docs/superpowers/specs/2026-11-13-limits-toggle-view-design.md`

## Global Constraints

- Module boundaries (CLAUDE.md): `bar.py` knows nothing about argv or env; `watch.py` is the only module combining `bar` with `claude`; `claude.py` is not touched.
- Label text is ASCII-only (`TextElement` validation) and must fit 72px with no scrolling.
- Do not convert the poll loop to asyncio.
- Never redraw when the payload is unchanged (redraw restarts animations).
- Fill turns `LIMITS_HIGH_FILL_COLOR` above `LIMITS_HIGH_THRESHOLD` (85) as today.
- A draw never removes omitted ids; a hidden element must be redrawn as `BLANK_TEXT`.
- No token or response body in any log or message.
- `make test` (ruff, ruff format, ty, pytest, 75% coverage) passes at the end of every task. Imports follow `force-sort-within-sections`. `except A, B:` without parentheses is correct (PEP 758).
- Tests never touch the network or the real keychain: patch `claude.resolve_token`, use `bar_transport`.

## Review Focus

1. Usage above 100 or a float like 99.6: label shows `100%`, never `101%` or `99.6%`; bar fill is clamped.
2. A Start press before the first successful fetch (no cached usage): must fetch, not crash.
3. Listener stream drops or raises: retried quietly, never reaches the poll loop; Ctrl+C returns promptly with the thread stopped.
4. Release events, `ok`/`back` buttons, selector and wheel events: ignored. Two presses return to 5-hour.
5. A press right after a transient fetch failure: keeps the old screen, does not raise.

## Setup (once, before Task 1)

The spec and this plan are untracked in the main clone. From the repo root:

```bash
git switch -c limits-toggle-view
git add docs/superpowers/specs/2026-11-13-limits-toggle-view-design.md docs/superpowers/plans/2026-11-13-limits-toggle-view.md
git commit -m "docs: spec and plan for the limits toggle view"
git switch main
git worktree add ../busyboy.worktrees/limits-toggle-view limits-toggle-view
cd ../busyboy.worktrees/limits-toggle-view && make install-dev
```

(Worktree location follows the GitLens convention in the user's CLAUDE.md. No untracked local config exists in this repo to copy.) All later work happens in that worktree.

## File Structure

- `src/busyboy/bar.py`: one-limit layout and `StartButtonListener`.
- `src/busyboy/watch.py`: label formatting, render, `limits_tick`, `watch_limits` toggling.
- `src/busyboy/cli.py`: only the `limits` docstring/help text changes.
- `tests/test_bar.py`, `tests/test_watch.py`, `tests/test_cli.py`: updated per task.
- `CLAUDE.md`: updated in Task 5.

---

### Task 1: One-limit layout in `bar.py`

**Files:**
- Modify: `src/busyboy/bar.py` (limits constants ~lines 89-110, `LimitRow` and `build_limits_payload` ~lines 285-350)
- Test: `tests/test_bar.py` (replace the two-bar limits tests ~lines 370-446)

**Interfaces:**
- Produces: `LimitView(label: str, percent: float)` frozen dataclass; `build_limits_payload(view: LimitView) -> DisplayElements`; constants `LIMITS_TRACK_X=1`, `LIMITS_TRACK_WIDTH=70`, `LIMITS_TRACK_HEIGHT=7`, `LIMITS_BAR_Y=9`, `LIMITS_LABEL_X=1`, `LIMITS_LABEL_Y=-2`, `LIMITS_LABEL_FONT: MeasuredFontName = "small"`, `LIMITS_LABEL_COLOR = "#E6E6E6"` (author's call). Element ids `limit-label`, `limit-track`, `limit-fill`, in that draw order is track, fill, label.
- Removes: `LimitRow`, `LIMITS_SESSION_Y`, `LIMITS_WEEKLY_Y`, `LIMITS_COUNTDOWN_*`, `LIMITS_HIGH_COUNTDOWN_COLOR`, `LIMITS_COUNTDOWN_THRESHOLD`, `build_limits_payload(session=, weekly=)`.

- [ ] **Step 1: Write failing tests** (replace the old limits tests; keep the `limits_elements`-style helper but over one view)
  - `test_payload_is_track_fill_label_in_draw_order`: ids == `["limit-track","limit-fill","limit-label"]`.
  - `test_bar_is_on_the_bottom_rows`: track `y == bar.LIMITS_BAR_Y`, `LIMITS_BAR_Y + LIMITS_TRACK_HEIGHT <= bar.FRONT_DISPLAY_HEIGHT`, width `== bar.LIMITS_TRACK_WIDTH`.
  - `test_fill_width_follows_percent` parametrized `(0, 1), (50, 35), (100, 70), (150, 70), (-5, 1)`: fill width; the 0/negative cases have fill colour `LIMITS_TRACK_COLOR + "FF"`.
  - `test_fill_turns_red_above_85_percent`: `(85, "#CD6E58FF")`, `(85.1, "#B3261EFF")`.
  - `test_label_text_font_and_position`: text equals the view's label, font `"small"`, `x == LIMITS_LABEL_X`, `y == LIMITS_LABEL_Y`, colour `"#E6E6E6FF"`.
  - `test_payload_is_scoped_to_the_application`: `application_name == bar.APPLICATION_NAME`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_bar.py -k limit -v`. Expected: FAIL (no `LimitView`).
- [ ] **Step 3: Implement** the constants and `build_limits_payload(view: LimitView) -> DisplayElements` in `bar.py`, reusing the existing fill-width/colour logic from `_limit_row_elements` (delete that function). Label goes through `_to_displayable_ascii`.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_bar.py -v`. Expected: PASS (watch tests fail until Task 2; ok to defer `make test` to the end of Task 2, but commit only when green: do Tasks 1 and 2 as one commit).

---

### Task 2: Label, render and tick in `watch.py`

**Files:**
- Modify: `src/busyboy/watch.py` (`LimitsScreen`, `render_limits`, `_limits_payload`, `limits_tick`, ~lines 236-325)
- Test: `tests/test_watch.py` (replace `test_render_limits_*` and adjust `test_limits_tick_*`)

**Interfaces:**
- Consumes: `bar.LimitView`, `bar.build_limits_payload(view)`, existing `format_session_countdown`, `format_weekly_countdown`, `claude.Usage`/`claude.Window` (`utilization`, `resets_at`).
- Produces:
  - `LimitKind = Literal["session", "weekly"]`
  - `LimitsScreen(view: bar.LimitView, usage: claude.Usage)` frozen dataclass (replaces the old two-row one)
  - `format_limit_label(kind: LimitKind, window: claude.Window, now: datetime) -> str`
  - `render_limits(usage: claude.Usage, now: datetime, kind: LimitKind) -> LimitsScreen`
  - `limits_tick(config, previous: LimitsScreen | None, *, kind: LimitKind = "session", reuse_usage: bool = False, now: datetime | None = None) -> TickResult[LimitsScreen]`

- [ ] **Step 1: Write failing tests**
  - `test_format_limit_label` parametrized: session/85.0/3h49m -> `"5h 85% 3h49m"`; weekly/40/6 days -> `"wk 40% 6d"`; weekly/40/29h -> `"wk 40% 1d 5h"`; session/99.6 -> `"5h 100% ..."`; session/150 -> `"5h 100% ..."`; session/0.4 -> `"5h 0% ..."`; session with `resets_at=None` -> `"5h 85%"`; session with a past `resets_at` -> ends `"0m"`.
  - `test_render_limits_picks_the_requested_window`: same usage, kind `"weekly"` label starts `"wk "`.
  - `test_limits_tick_draws_the_selected_kind_on_the_first_poll`: patched `claude.resolve_token`, transport route for the draw; the posted payload's `limit-label` text is `"5h ..."` and for `kind="weekly"` is `"wk ..."`.
  - `test_limits_tick_reuse_usage_does_not_fetch`: previous screen given, `reuse_usage=True`, `claude.fetch_usage` patched to raise; the tick still draws the other kind.
  - `test_limits_tick_reuse_usage_without_a_previous_screen_fetches`: `previous=None`, `reuse_usage=True` -> fetches (Review Focus 2).
  - `test_limits_tick_skips_the_draw_when_nothing_changed` and the transient/auth/bar-failure/Retry-After tests keep their meaning with the new types. `test_limits_tick_does_not_redraw_when_only_invisible_values_changed` becomes: percent moving within the same rounded integer and the same minute does not redraw.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_watch.py -k "limit" -v`. Expected: FAIL.
- [ ] **Step 3: Implement** per the signatures. Label: `f"{name} {pct}%"` plus `f" {countdown}"` when `resets_at` exists, where `name` is `"5h"`/`"wk"` and `pct = round(min(max(utilization, 0), 100))`. When `reuse_usage and previous is not None`, use `previous.usage` instead of resolving a token and fetching. Keep the compare-the-payload-not-the-screen diff and the existing exception handling.
- [ ] **Step 4: Run** `make test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: show one limit at a time as a short label over a bar` (Tasks 1+2).

---

### Task 3: `StartButtonListener` in `bar.py`

**Files:**
- Modify: `src/busyboy/bar.py`
- Test: `tests/test_bar.py`

**Interfaces:**
- Consumes: `busylib.AsyncBusyBar` (same host/token as `_client`), `busylib.features.input_events.input_events`, `ButtonEvent`, `config.host`, `config.token_value`.
- Produces: `StartButtonListener(config: BusyboyConfig, *, stream: Callable[[], AsyncIterator[Mapping[str, object]]] | None = None)` with `start() -> None`, `stop() -> None`, `wait_for_press(timeout: float) -> bool` (blocks up to `timeout`, returns True and consumes the press if one arrived), and context-manager use (`with listener:` starts/stops). `stream=None` means busylib's status stream; tests inject a fake.

- [ ] **Step 1: Write failing tests** (fake `stream` yielding dict messages; build messages with a helper that matches busylib's shape `{"updates": [{"input": {"button_event": {"button": "START", "action": "PRESS"}}}]}`; confirm the shape against `busylib/features/input_events.py` before relying on it)
  - `test_a_start_press_wakes_wait_for_press` -> True.
  - `test_wait_for_press_times_out_with_false` (`timeout=0.05`).
  - `test_release_ok_and_back_are_ignored` (RELEASE of START, PRESS of OK/BACK, a selector and an encoder event -> False).
  - `test_two_presses_are_two_wakes`: each consumed once.
  - `test_a_stream_that_raises_is_retried_and_never_escapes`: stream raises `BusyBarWebSocketError` first, then yields a press on the second connection; `wait_for_press` eventually True; patch the retry sleep so the test is fast.
  - `test_stop_returns_promptly_and_ends_the_thread`: a stream that blocks forever; `stop()` returns within 1s and the thread is not alive.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_bar.py -k listener -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Daemon thread whose body is `asyncio.run(...)` over a loop that opens the stream, feeds each message through `input_events`, and sets a `threading.Event` for `ButtonEvent(button="start")` with `is_press`. On any exception, log at DEBUG (message text only, never the token) and reconnect with capped exponential backoff (1s doubling to 30s). `stop()` cancels the running task via `loop.call_soon_threadsafe` and joins with a short timeout. Default stream: `busylib.AsyncBusyBar(config.host, token=config.token_value).stream_status_ws()` (check the constructor in the installed busylib; close the client on exit).
- [ ] **Step 4: Run** `make test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: listen for the Start button on the bar`.

---

### Task 4: Toggle wiring in `watch_limits` and the CLI

**Files:**
- Modify: `src/busyboy/watch.py` (`watch_limits`), `src/busyboy/cli.py` (the `limits` docstring, `--interval` help unchanged)
- Test: `tests/test_watch.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `bar.StartButtonListener`, `limits_tick(..., kind=, reuse_usage=)`, `_poll`.
- Produces: `watch_limits(config, *, interval=DEFAULT_LIMITS_INTERVAL_SECONDS, sleep=None, listener: bar.StartButtonListener | None = None) -> None`. Behaviour: starts at `"session"`; if `sleep` is given it is used as the wait (no press ever); otherwise the wait is `listener.wait_for_press`, and a True result flips `kind` between `"session"` and `"weekly"` and makes the next tick `reuse_usage=True`, unless `interval` seconds have passed since the last fetch (`time.monotonic()`), in which case it fetches. When `listener is None` one is created from `config`, started before the loop and stopped in a `finally`. `_poll` is unchanged.

- [ ] **Step 1: Write failing tests** (fake listener with scripted `wait_for_press` results; `claude.fetch_usage` patched with a call counter)
  - `test_a_press_switches_to_weekly_and_redraws_without_fetching`: fetch count stays 1, second draw's label starts `"wk "`.
  - `test_a_second_press_returns_to_the_five_hour_view`.
  - `test_a_press_after_the_interval_has_elapsed_fetches_again` (patch `watch.time.monotonic`).
  - `test_a_press_with_nothing_changed_does_not_redraw` is not applicable (view differs); instead `test_a_timeout_poll_with_unchanged_data_does_not_redraw`.
  - `test_a_press_after_a_transient_failure_keeps_the_display_and_does_not_raise` (Review Focus 5).
  - `test_the_listener_is_started_and_stopped_even_on_interrupt`.
  - `test_a_failing_listener_never_ends_the_watch`: `wait_for_press` is wrapped so that when the listener thread dies the loop still polls on timeouts (listener swallows its own errors; assert the loop is unaffected by a listener whose `wait_for_press` simply returns False).
  - Update `test_watch_limits_*` and the CLI limits tests: those that drove the loop by patching `time.sleep` now patch `bar.StartButtonListener` with a fake whose `wait_for_press` raises `KeyboardInterrupt`, since the default wait no longer calls `time.sleep`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_watch.py tests/test_cli.py -k limits -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `watch_limits` per the interface; `limits` docstring in `cli.py` mentions the Start button.
- [ ] **Step 4: Run** `make test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: toggle between the 5-hour and weekly limit with the Start button`.

---

### Task 5: Real-bar check and docs

**Files:**
- Modify: `CLAUDE.md`, `src/busyboy/bar.py` (font constant only if needed), `docs/superpowers/specs/2026-11-13-limits-toggle-view-design.md` (note the measured result)

This task needs the user's physical bar (`source ~/.zshrc && busybarenv`; never print the token). If it is not available, stop and report that this task is pending.

- [ ] **Step 1: Measure glyph widths.** Run `uv run --frozen python tools/capture_screen.py` (extend its probe strings to include `5h 100% 3h49m` and `wk 40% 1d 5h` in `tiny` and `small`). Record the widest label's pixel width per font.
- [ ] **Step 2: Choose the font.** Keep `small` if both labels fit in 72px; else `tiny`; else shorten the label format (update Task 2 tests and `format_limit_label`) and say so in the report.
- [ ] **Step 3: Check `stream_status_ws(enable=False)`** still delivers Start presses; if it does, use it in the default stream and note why in a comment.
- [ ] **Step 4: Run it live** (`uv run --frozen busyboy claude limits`): confirm the label and bar at 0%, mid, and above 85% if reachable; press Start and confirm the instant switch to weekly and back; Ctrl+C clears the display.
- [ ] **Step 5: Update `CLAUDE.md`:** the `watch.py` and `bar.py` descriptions, the `busyboy claude limits` CLI-contract paragraph (one view, Start toggle, listener failures swallowed), and replace the two-track limits paragraph in Hardware facts with the new layout and the measured widths.
- [ ] **Step 6: Run** `make test`. Expected: PASS. **Commit** `docs: record the limits toggle layout and measured label widths`.
