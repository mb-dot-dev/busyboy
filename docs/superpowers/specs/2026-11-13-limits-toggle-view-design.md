# `claude limits`: one-limit view with a Start-button toggle

Builds on `2026-11-12-claude-limits-design.md`.

## Intent

`busyboy claude limits` currently draws two bars (5-hour session, weekly), with a reset countdown that only appears
above 75% usage. The user wants to see the time left until the **5-hour reset** at all times, and to flip to the
weekly limit on demand. So the display shows **one limit at a time**, and the bar's **Start / Pause button** switches
between them.

## Behaviour (decided by the user)

- One limit on screen at a time. Default at launch: the **5-hour** limit.
- Each press of the Start/Pause button toggles 5-hour <-> weekly. The screen redraws immediately, not at the next poll.
- Top row: a **short text label** with the limit name, percent used and time to reset, e.g. `5h 85% 3h49m`. It must fit
  the 72px display without scrolling.
- Bottom row: one progress bar, fill = percent used.
- The time to reset is always shown (the 75% threshold is gone).

## Goals

- Time to the 5-hour reset visible at all times.
- Toggle responds to a press without waiting for the poll interval (default 180s).
- Keep the existing guarantees: transient errors swallowed, auth errors end the run, clear on start and exit, no
  token in any output.

## Non-goals / anti-goals

- No scrolling text. A label that does not fit is a failure.
- Selected view is not persisted across runs.
- No other buttons, selector, or wheel handling.
- Must not redraw when nothing changed (redraw restarts animations; the existing diff stays).

## How it works

Calls marked *(my call)* are the author's, not the user's.

**Button listening.** busylib exposes physical input only on the async status WebSocket
(`AsyncBusyBar.stream_status_ws()`); the sync client raises `NotImplementedError`. The Start button is
`busylib.features.input_events.ButtonEvent(button="start")`; match `is_press` to ignore releases. *(my call)* Run a
daemon thread owning its own asyncio loop that consumes the stream and, on each Start press, flips a thread-safe
selection (`threading.Event`/lock-guarded value) and wakes the poll loop. The poll loop stays synchronous. The
listener lives in `bar.py` (all busylib knowledge stays there); `watch.py` consumes it through a small interface.
Streaming is local-only; it needs a direct host connection, which busyboy always uses.

**Listener failure.** *(my call)* If the stream drops or errors, the listener retries with backoff and logs at DEBUG;
polling continues and the button simply stops working until it reconnects. It never raises into the poll loop and
never ends the process. Stream frames are mostly screen updates, not input: ignore everything but button events.

**Poll loop.** `limits_tick` fetches usage, renders the *selected* limit, diffs against the previous screen, and
draws only on change. A button press re-renders from the last fetched usage (no new API call) and draws. *(my call)*
The sleep between polls becomes an interruptible wait (wake on press) rather than a bare `time.sleep`; tests still
drive the loop by patching `time.sleep` per CLAUDE.md, so the builder must keep a seam that tests can drive.

**Layout** (72x16 front display; geometry from CLAUDE.md "Hardware facts").
- Bottom row: track and fill in rows 9-15 (7 rows, as today), full width.
  *(my call)* Track back to the full 70px at x=1 since the countdown no longer lives beside it.
- Top row: one text element in rows 0-7, id stable (`limit-label`), `y` chosen with the font's `offset`
  (small: offset 2, 7 rows tall, so `y=-2` inks rows 0-6; tiny: offset 1, 5 rows).
- Elements keep stable ids; elements for the unused limit do not exist, so on switch the single
  `label`/`track`/`fill` set is simply redrawn (replace by id). Remove ids from the old two-bar layout that would
  otherwise linger on the panel: the first draw after launch follows the existing clear-first, so no leftovers.
- Colours: fill red above 85% as today. *(my call)* Label colour is one fixed colour (no flip to white).

**Label format.** *(my call, exact strings)* `5h 85% 3h49m` and `wk 40% 6d` / `wk 40% 1d 5h`. Percent rounded to an
integer. Time format reuses `format_session_countdown` / `format_weekly_countdown`. No reset time: drop the time
(`5h 85%`).

## Constraints

- Module boundaries in CLAUDE.md hold: `bar.py` knows nothing about argv or the environment; `watch.py` is the only
  module combining `bar` with `claude`; `claude.py` is untouched.
- Text must pass `TextElement`'s ASCII-only validation.
- `uv`/`make` workflow; `make test` must pass; coverage stays above 75%.
- The sync-vs-async mismatch is real: do not convert the poll loop to asyncio.

## Left to the builder

- **Font and exact width.** The label must fit 72px. Measure `tiny` vs `small` glyph widths with
  `tools/capture_screen.py` against a real bar (`busybarenv`), pick the largest font that fits the longest label
  (`wk 40% 1d 5h` / `5h 100% 3h49m`), and shorten the format only if neither fits. Record the measured widths in
  CLAUDE.md's hardware facts.
- Verify on a real bar whether `stream_status_ws(enable=False)` still delivers button events with fewer frames;
  use it if so.
- The listener thread's shutdown on Ctrl+C and its interaction with the final clear.
- Replace the tests that assume two bars and the 75% threshold; add tests for the toggle, immediate redraw on press,
  no-redraw-when-unchanged, listener failure not ending the watch, and label formatting.
- Update CLAUDE.md (watch/bar descriptions, the 70x7 two-track layout in Hardware facts) and the CLI help.

## Measured on a real bar (2026-11-13)

- Widest labels: `wk 100% 23h59m` is 58px in `tiny` and 55px in `small`; `5h 100% 3h49m` is 52px and 49px. All fit
  72px, so `small` (the larger font) was kept and no label was shortened.
- `small` at `y=-2` inks rows 0-4 for digits and lowercase letters (descenders reach row 6).
- The real `StartButtonListener` connected to the bar's status WebSocket for 6s with no stream failures and
  stopped in under 0.1s. A physical Start press was not exercised by the author; `enable=False` was not tried
  (it would have needed an injected press, which would act on the user's device).
- `busyboy claude limits` ran against the bar, drew the label and bar, and on Ctrl+C exited 0 with the display
  cleared.

## Follow-up after trying it on the bar (2026-11-13)

The `small` label (5 rows tall) looked small and left a 4-row gap above the bar. Changed to `condensed` (7 rows
inked), with the bar moved to rows 8-15 so one blank row separates them. `normal` and `bold` were measured and
clip at 72px. At 71px, `wk 100% 23h59m` and `wk 100% 1d 23h` lose their last character in `condensed`, so the
weekly label time is now `6d` from 48h, whole hours (`47h`) under that, and `59m` under an hour.
