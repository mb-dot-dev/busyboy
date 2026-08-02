# Workflow row font calibration

Date: 2026-08-02

## Problem

`busyboy gh workflow` draws two text rows on the 72x16 front display, both in the `tiny` font
(`bar.ROW_FONT`) at `y=1` and `y=9`. `tiny` is the smallest font the bar offers, chosen because it was the
only size measured to stack twice within 16 rows. It is legible but cramped, and the top row — the repository
slug, the line you read first — has no more weight than the branch below it.

The goal is a larger top row: `normal` on top, `small` beneath. Neither font's glyph geometry has ever been
measured, so it is unknown whether the pair stacks unclipped in 16 rows, and unknown what `y` values would
place them if it does.

The blocker is that this cannot be derived from the code or the vendor OpenAPI spec. Font metrics are a
property of the device firmware and are documented nowhere. They have to be measured against real hardware —
and the frame-capture capability that produced the existing hardware facts was lost when busylib stopped being
a dependency (see the hardware-facts section of `CLAUDE.md`).

## Scope

In scope: the two-row workflow layout's fonts and vertical placement, and the tooling needed to measure them.

Out of scope:

- `busyboy text` — `DEFAULT_FONT` (`condensed`) and `DEFAULT_TEXT_Y` are already calibrated and unchanged.
- Horizontal layout — `ICON_SIZE`, `ICON_X`, `ICON_Y`, `TEXT_X`, `TEXT_WIDTH` are unchanged. This is a
  vertical calibration only.
- `scroll_rate` — unchanged at `DEFAULT_SCROLL_RATE`.
- `watch.py`, `cli.py`, `github.py`, `git.py`, `config.py` — none of them mention fonts.

## Architecture

Two pieces, joined only by a measurement recorded in code and prose.

### `tools/capture_screen.py` (new)

A development tool, not part of the shipped package — the same status as `tools/generate_icons.py`. Run with
`uv run --frozen python tools/capture_screen.py`. Never imported by anything under `src/busyboy/`.

It may import from `busyboy` (a tool consuming the package is fine; the reverse would invert the dependency),
and reuses `config.load_config` for `BUSYBOY_HOST`/`BUSYBOY_TOKEN` handling plus `bar`'s public names —
`TextElement`, `DisplayElements`, `APPLICATION_NAME`, `FONT_NAMES`, `clear`.

It performs its own `requests` calls rather than reaching for `bar._request`. It needs `GET /api/screen`, an
endpoint `bar.py` has no reason to know about, so it owns HTTP in both directions instead of half-borrowing a
private helper. `bar.py`'s transport is not modified.

### `src/busyboy/bar.py` (modified)

The outcome of the calibration:

- `ROW_FONT` splits into `ROW_ONE_FONT` and `ROW_TWO_FONT`.
- `ROW_ONE_Y` and `ROW_TWO_Y` take values derived from the measured geometry.
- `_row()` gains a `font: DisplayFontName` parameter; `build_workflow_payload` passes each row its own.
- `FONT_GLYPH_HEIGHTS` and `FONT_GLYPH_OFFSETS` are added: `dict[DisplayFontName, int]` over the seven real
  fonts, from the measured table.
- The comment block currently at `bar.py:45-58` is rewritten to describe the new layout and cite the
  measurement.

## Measurement procedure

Glyph height does not depend on `y`, so one capture per font is sufficient. A sweep over font-and-`y`
combinations would re-measure the same fact repeatedly.

One probe cycle, repeated for each name in `bar.FONT_NAMES` except `global`, which names no fixed size and is
excluded from the table for the same reason it is excluded from selection below:

1. `bar.clear(config)` — scoped to `application_name`, so a previous probe cannot linger in the frame.
2. POST `/api/display/draw` with a single `TextElement`: `x=0`, `y=0`, `scroll_rate=0`, text
   `AXgjpqy019|_`. Caps, descenders, digits, a pipe and an underscore, so the measured range is the font's
   full glyph box rather than one letter's extent.
3. Sleep 0.5s for the panel to settle, then GET `/api/screen`.
4. Decode: the response body is base64-encoded raw framebuffer bytes — the `Content-Type: image/bmp` header
   is misleading, there is no BMP header. The front display is RGB888, so the buffer must be exactly
   `72 * 16 * 3 = 3456` bytes. A row is occupied if any of its 72 pixels is non-black.
5. Print the first occupied row, the last occupied row, the derived height, and a 16-line `#`/`.` occupancy
   map.

### `y` is not the first inked row

`bar.py:32-35` records that `condensed` has a 9-row glyph box but `y=2` places glyphs on rows 4-12 — a +2
offset between the `y` sent and the pixels produced. The probe at `y=0` therefore yields two numbers per font:

- `offset` — the first inked row when `y=0`.
- `height` — the count of inked rows spanned.

Placement is computed in inked-row space and converted back with `y = target_row - offset`.

## Font selection

Applied to the measured table:

1. Candidates exclude `global`. It selects the device's configured font rather than naming a size, so it has
   no fixed height to stack.
2. If `height(normal) + height(small) <= 16`, use `normal` for row one and `small` for row two. This is the
   stated goal and takes precedence over any other fitting pair.
3. Otherwise, choose the pair maximising combined height subject to fitting within 16 rows, tie-broken toward
   the taller top row — the top row carries the repository slug, the line the calibration exists to enlarge.

## Placement

With `L = 16 - height(row one font) - height(row two font)`, distribute `L` as evenly as possible in the
order top margin, then gap, then bottom margin. So `L=2` gives top 1, gap 1, bottom 0; `L=3` gives 1, 1, 1;
`L=1` gives 1, 0, 0.

Those margins are in inked-row space; each row's `y` is then `target_row - offset(font)`. A font whose offset
exceeds its target row would give a negative `y`. Rather than send one — the firmware's behaviour there is
unmeasured — clamp that row's `y` to 0 and accept the resulting inked position, then confirm against the
capture that neither row is clipped or overlapping.

**Validation gate.** Applied to `tiny`/`tiny`, this rule must reproduce the current, known-good `y=1` and
`y=9`. If it does not, the model of the `y`-to-inked-row offset is wrong. In that case, stop and report the
measurements rather than shipping a layout derived from a rule that fails its own check.

## Error handling

The tool is dev tooling and carries none of the CLI contract's obligations, but it fails loudly rather than
producing quietly wrong measurements:

- Config comes from `load_config()`, so `BUSYBOY_HOST`/`BUSYBOY_TOKEN` (and `busybarenv`) work unchanged. The
  token value is never printed.
- Any non-2xx from the draw or from `/api/screen` raises, reporting the status code. No retries: a developer
  at a terminal reruns it.
- A framebuffer that is not exactly 3456 bytes raises immediately. A short or misdecoded buffer would
  otherwise produce plausible-looking but wrong row numbers — the one failure mode that could silently corrupt
  the entire calibration.
- An all-black frame raises rather than reporting height 0; it means the draw did not land or the settle delay
  was too short.

## Testing

No test touches a real bar.

### Unit tests (`tests/test_bar.py`)

- `test_the_two_rows_share_a_text_column_beside_the_icon` currently asserts both rows carry `bar.ROW_FONT`.
  That assertion splits: row one asserts `ROW_ONE_FONT`, row two asserts `ROW_TWO_FONT`. The shared `x`,
  `width`, and `display` assertions stay as they are.
- New test: the two rows fit within the 16-row front display without overlapping — the vertical analogue of
  the existing `test_the_icon_and_text_column_fit_the_front_display`. It reads `FONT_GLYPH_HEIGHTS` and
  `FONT_GLYPH_OFFSETS`, which is why those tables live in `bar.py` rather than only in prose: the invariant
  can be asserted instead of trusted.

### No tests for the tool

`tools/capture_screen.py` gets none, matching `tools/generate_icons.py`. Coverage measures `src` only, so this
does not affect the 75% gate.

### Manual verification

The calibration is its own test. The `tiny`/`tiny` validation gate above must pass. A final capture of the
real two-row layout — real repository slug, real branch — is rendered as an ASCII occupancy map and checked
against the physical bar.

## Documentation

`CLAUDE.md` is updated in two places:

- The hardware-facts section gains the full measured font table (offset and height for all seven real fonts)
  and the new two-row layout, replacing the note that only `tiny` stacks twice. The paragraph explaining that
  frame capture is no longer available is replaced by a pointer to `tools/capture_screen.py`.
- The architecture section's `src/busyboy/assets/` bullet is joined by a note on `tools/capture_screen.py`.
