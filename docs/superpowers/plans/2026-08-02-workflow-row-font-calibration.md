# Workflow Row Font Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the BUSY Bar front display's font geometry against real hardware, then use it to give `busyboy gh workflow` a larger top row (`normal`) over a smaller bottom row (`small`).

**Architecture:** A new development tool, `tools/capture_screen.py`, draws a probe string in each font at `y=0`, reads the framebuffer back through `GET /api/screen`, and reports which pixel rows the glyphs occupy. The resulting offset/height table drives a font choice and two `y` values, which land in `src/busyboy/bar.py` as constants. The tool is dev tooling in the mould of `tools/generate_icons.py` — never imported by the package.

**Tech Stack:** Python 3.14, `uv` (frozen lockfile), `requests`, `pydantic` / `pydantic-settings`, `pytest`, `responses`, `ruff`, `ty`.

## Global Constraints

- Python `>=3.14`. `except OSError, subprocess.TimeoutExpired:` (PEP 758, unparenthesized) is correct — `ruff format` produces it. Do not "fix" it.
- All commands run through `make`, which wraps `uv run --frozen`. Never invoke `pytest`/`ruff`/`ty` bare.
- `make test` (= `lint` then `unit`) must pass before every commit.
- Ruff: 120-char lines, double quotes, PEP 257 docstrings, `force-sort-within-sections` (sorts `import x` and `from x import y` together by module name).
- `ty` and `ruff` both check `tools/` — it is not excluded. New tool code must pass `make lint`.
- Coverage gate is 75% over `src` only; `tools/` is not measured.
- Module boundaries: `bar.py` knows nothing about argv or the environment. A tool may import `busyboy`; `busyboy` must never import a tool.
- Never print or commit the BUSY Bar API token.
- Hardware steps need a real bar. `source ~/.zshrc && busybarenv` exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN`.

## File Structure

| File | Responsibility |
|---|---|
| `src/busyboy/bar.py` (modify) | Gains `FRONT_DISPLAY_HEIGHT`; later, per-row font constants and the measured geometry tables. |
| `tools/capture_screen.py` (create) | Probe, capture, decode, measure, and recommend a layout. Dev tool, not packaged. |
| `tests/test_bar.py` (modify) | Per-row font assertions and the vertical-fit invariant. |
| `CLAUDE.md` (modify) | Records the measured font table and the restored capture capability. |

**A note on Task 2.** This plan cannot contain the measured numbers — they are a property of the device firmware and do not exist until the tool runs. Task 2 produces them; Task 3 consumes them. An agent executing Task 3 **must** use the values recorded by Task 2 and must not invent, estimate, or carry over plausible-looking numbers from this document's worked examples.

---

### Task 1: Frame capture and font measurement tool

**Files:**
- Modify: `src/busyboy/bar.py:17-19` (add `FRONT_DISPLAY_HEIGHT`)
- Create: `tools/capture_screen.py`
- Test: `tests/test_bar.py` (one assertion for the new constant)

**Interfaces:**
- Consumes: `busyboy.bar.{FRONT_DISPLAY_WIDTH, APPLICATION_NAME, FONT_NAMES, DisplayFontName, TextElement, DisplayElements, draw_text, clear}`, `busyboy.config.{BusyboyConfig, load_config}`.
- Produces: `bar.FRONT_DISPLAY_HEIGHT: int = 16`. In the tool: `capture_front_frame(config) -> bytes`, `occupied_rows(frame) -> list[int]`, `render_occupancy(frame) -> str`, `probe_font(config, font) -> tuple[int, int]` returning `(offset, height)`, `select_pair(heights) -> tuple[str, str]`, `place_rows(top, bottom, offsets, heights) -> tuple[int, int]` returning `(row_one_y, row_two_y)`.

- [ ] **Step 1: Write the failing test for the new constant**

In `tests/test_bar.py`, add beside `test_the_icon_and_text_column_fit_the_front_display`:

```python
def test_the_front_display_is_seventy_two_by_sixteen():
    assert bar.FRONT_DISPLAY_WIDTH == 72
    assert bar.FRONT_DISPLAY_HEIGHT == 16
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --frozen pytest tests/test_bar.py::test_the_front_display_is_seventy_two_by_sixteen -v`
Expected: FAIL — `AttributeError: module 'busyboy.bar' has no attribute 'FRONT_DISPLAY_HEIGHT'`

- [ ] **Step 3: Add the constant**

In `src/busyboy/bar.py`, replace the existing width comment and constant (lines 17-19) with:

```python
# The front display is a 72x16 RGB LED matrix; spanning its full width gives
# scrolling text the whole panel to move across.
FRONT_DISPLAY_WIDTH = 72
FRONT_DISPLAY_HEIGHT = 16
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run --frozen pytest tests/test_bar.py::test_the_front_display_is_seventy_two_by_sixteen -v`
Expected: PASS

- [ ] **Step 5: Write the tool**

Create `tools/capture_screen.py`:

```python
"""
Measure the BUSY Bar front display's font geometry against real hardware.

Run with `uv run --frozen python tools/capture_screen.py`. Connection settings
come from BUSYBOY_HOST / BUSYBOY_TOKEN, exactly as the CLI reads them.

The bar's font metrics are a firmware property. Neither the vendor OpenAPI spec
nor busyboy records them, so the only way to learn a font's height is to draw
it and read the pixels back. For each font this draws a probe string at y=0,
captures the framebuffer through GET /api/screen, and reports which rows the
glyphs actually occupy.

Drawing and clearing go through bar's public API. Capture does not: /api/screen
is an endpoint bar.py has no reason to know about, so this owns that one
request itself rather than reaching into bar's private transport.
"""

import base64
import time
from typing import cast

import requests

from busyboy import bar
from busyboy.config import BusyboyConfig, load_config

# Caps, descenders, digits, a pipe and an underscore, so the measured range is
# the font's full glyph box rather than one letter's extent.
PROBE_TEXT = "AXgjpqy019|_"
PROBE_ELEMENT_ID = "probe"
SETTLE_SECONDS = 0.5
SCREEN_PATH = "/api/screen"
FRONT_DISPLAY_INDEX = "0"
BYTES_PER_PIXEL = 3
FRAME_SIZE = bar.FRONT_DISPLAY_WIDTH * bar.FRONT_DISPLAY_HEIGHT * BYTES_PER_PIXEL
REQUEST_TIMEOUT = (5, 10)

# "global" selects whatever font the device is configured for rather than
# naming a size, so it has no fixed height to measure or to stack.
MEASURABLE_FONTS: tuple[str, ...] = tuple(name for name in bar.FONT_NAMES if name != "global")

# The layout the two-row workflow display uses today, in tiny/tiny. place_rows
# must reproduce these, or the model of the y-to-inked-row offset is wrong.
KNOWN_GOOD_TINY_LAYOUT = (1, 9)


class CaptureError(Exception):
    """Raised when a frame cannot be captured or makes no sense."""


def _url(config: BusyboyConfig, path: str) -> str:
    """Normalize a bare host/IP to a full URL."""
    host = config.host if "://" in config.host else f"http://{config.host}"
    return f"{host}{path}"


def _headers(config: BusyboyConfig) -> dict[str, str]:
    """Build the X-API-Token header, or no headers when no token is set."""
    token = config.token_value
    return {"X-API-Token": token} if token else {}


def _decode_frame(body: bytes) -> bytes:
    """
    Decode the /api/screen body into raw framebuffer bytes.

    The response's `Content-Type: image/bmp` is misleading — there is no BMP
    header, just base64-encoded RGB888. A buffer of the wrong size is fatal
    rather than merely surprising: it would yield plausible-looking but wrong
    row numbers, which is the one failure mode that silently corrupts the
    whole calibration.
    """
    if len(body) == FRAME_SIZE:
        return body
    try:
        # binascii.Error subclasses ValueError, so this covers a malformed body.
        frame = base64.b64decode(body, validate=True)
    except ValueError as error:
        raise CaptureError(f"Response body is neither a raw frame nor valid base64: {error}") from error
    if len(frame) != FRAME_SIZE:
        raise CaptureError(f"Expected a {FRAME_SIZE}-byte front frame, got {len(frame)} bytes")
    return frame


def capture_front_frame(config: BusyboyConfig) -> bytes:
    """Read the front display's framebuffer back off the bar."""
    response = requests.get(
        _url(config, SCREEN_PATH),
        headers=_headers(config),
        params={"display": FRONT_DISPLAY_INDEX},
        timeout=REQUEST_TIMEOUT,
    )
    try:
        if response.status_code >= 400:
            raise CaptureError(f"GET {SCREEN_PATH} failed with HTTP {response.status_code}")
        return _decode_frame(response.content)
    finally:
        response.close()


def _pixel_is_lit(frame: bytes, row: int, column: int) -> bool:
    """Report whether one pixel is anything other than black."""
    start = (row * bar.FRONT_DISPLAY_WIDTH + column) * BYTES_PER_PIXEL
    return any(frame[start : start + BYTES_PER_PIXEL])


def occupied_rows(frame: bytes) -> list[int]:
    """List the display rows carrying at least one lit pixel."""
    return [
        row
        for row in range(bar.FRONT_DISPLAY_HEIGHT)
        if any(_pixel_is_lit(frame, row, column) for column in range(bar.FRONT_DISPLAY_WIDTH))
    ]


def render_occupancy(frame: bytes) -> str:
    """Render the frame as a row-numbered ASCII occupancy map."""
    lines = []
    for row in range(bar.FRONT_DISPLAY_HEIGHT):
        cells = "".join(
            "#" if _pixel_is_lit(frame, row, column) else "." for column in range(bar.FRONT_DISPLAY_WIDTH)
        )
        lines.append(f"{row:2d} {cells}")
    return "\n".join(lines)


def probe_font(config: BusyboyConfig, font: str) -> tuple[int, int]:
    """
    Draw one font at y=0 and measure the rows its glyphs occupy.

    Returns (offset, height): the first inked row, and the count of rows
    spanned. y is not the first inked row — condensed sits 2 rows below the y
    it is given — so both numbers are needed to place a row deliberately.
    """
    bar.clear(config)
    element = bar.TextElement(
        id=PROBE_ELEMENT_ID,
        text=PROBE_TEXT,
        font=cast(bar.DisplayFontName, font),
        display="front",
        x=0,
        y=0,
        width=bar.FRONT_DISPLAY_WIDTH,
        scroll_rate=0,
    )
    bar.draw_text(config, bar.DisplayElements(application_name=bar.APPLICATION_NAME, elements=[element]))
    time.sleep(SETTLE_SECONDS)
    frame = capture_front_frame(config)
    rows = occupied_rows(frame)
    if not rows:
        raise CaptureError(f"{font}: captured an all-black frame — the draw did not land, or 0.5s was too short")
    print(f"\n--- {font} ---")
    print(render_occupancy(frame))
    return rows[0], rows[-1] - rows[0] + 1


def select_pair(heights: dict[str, int]) -> tuple[str, str]:
    """
    Choose the fonts for the two workflow rows.

    normal-over-small is the goal and wins outright if it fits. Otherwise take
    the tallest pair that does fit, preferring the taller font on top — the top
    row carries the repository slug, the line this calibration exists to
    enlarge.
    """
    if heights["normal"] + heights["small"] <= bar.FRONT_DISPLAY_HEIGHT:
        return "normal", "small"
    pairs = [
        (top, bottom)
        for top in heights
        for bottom in heights
        if heights[top] + heights[bottom] <= bar.FRONT_DISPLAY_HEIGHT
    ]
    if not pairs:
        raise CaptureError("No pair of fonts fits within the 16-row display")
    return max(pairs, key=lambda pair: (heights[pair[0]] + heights[pair[1]], heights[pair[0]]))


def place_rows(top: str, bottom: str, offsets: dict[str, int], heights: dict[str, int]) -> tuple[int, int]:
    """
    Compute both rows' y values from the measured geometry.

    Leftover rows are spread top margin, then gap, then bottom margin, so the
    pair sits high rather than centred-and-clipped. Margins are in inked-row
    space; y is that target minus the font's own offset, clamped at 0 because
    the firmware's behaviour for a negative y is unmeasured.
    """
    leftover = bar.FRONT_DISPLAY_HEIGHT - heights[top] - heights[bottom]
    if leftover < 0:
        raise CaptureError(f"{top} over {bottom} needs {-leftover} more rows than the display has")
    margins = [leftover // 3] * 3
    for index in range(leftover % 3):
        margins[index] += 1
    top_margin, gap, _ = margins
    top_row = top_margin
    bottom_row = top_margin + heights[top] + gap
    return max(0, top_row - offsets[top]), max(0, bottom_row - offsets[bottom])


def main() -> None:
    """Probe every measurable font, then report the table and the layout it implies."""
    config = load_config()
    offsets: dict[str, int] = {}
    heights: dict[str, int] = {}
    for font in MEASURABLE_FONTS:
        offsets[font], heights[font] = probe_font(config, font)
    bar.clear(config)

    print("\n=== measured font geometry ===")
    print(f"{'font':<12} {'offset':>7} {'height':>7}")
    for font in MEASURABLE_FONTS:
        print(f"{font:<12} {offsets[font]:>7} {heights[font]:>7}")

    validation = place_rows("tiny", "tiny", offsets, heights)
    print(f"\ntiny/tiny validation gate: {validation}, expected {KNOWN_GOOD_TINY_LAYOUT}")
    if validation != KNOWN_GOOD_TINY_LAYOUT:
        raise CaptureError(
            f"place_rows gives {validation} for tiny/tiny but the known-good layout is "
            f"{KNOWN_GOOD_TINY_LAYOUT}. The offset model is wrong — stop and report, do not ship these values."
        )

    top, bottom = select_pair(heights)
    row_one_y, row_two_y = place_rows(top, bottom, offsets, heights)
    print("\n=== recommended layout ===")
    print(f"ROW_ONE_FONT = {top!r}   ROW_ONE_Y = {row_one_y}")
    print(f"ROW_TWO_FONT = {bottom!r}   ROW_TWO_Y = {row_two_y}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Verify lint and the full suite pass**

Run: `make test`
Expected: ruff check, ruff format --check, ty check all clean; all tests pass.

If `ruff format --check` objects, run `make format` and re-run `make test`.

- [ ] **Step 7: Commit**

```bash
git add src/busyboy/bar.py tests/test_bar.py tools/capture_screen.py
git commit -m "Add a frame-capture tool for measuring front display font geometry"
```

---

### Task 2: Run the calibration against real hardware

**Files:**
- Modify: `docs/superpowers/plans/2026-08-02-workflow-row-font-calibration.md` (record the results in this task's Results block)

**Interfaces:**
- Consumes: `tools/capture_screen.py` from Task 1.
- Produces: the measured `offset` and `height` for each of the seven measurable fonts, plus the chosen `ROW_ONE_FONT`, `ROW_TWO_FONT`, `ROW_ONE_Y`, `ROW_TWO_Y`. Task 3 consumes exactly these.

This task requires a real BUSY Bar on the network. It cannot be completed from tests.

- [ ] **Step 1: Point the environment at the real bar**

```bash
source ~/.zshrc && busybarenv
```

Confirm it took without revealing the token: `echo "${BUSYBOY_HOST:?not set}" && [ -n "$BUSYBOY_TOKEN" ] && echo "token set"`

- [ ] **Step 2: Confirm the bar is reachable**

```bash
uv run --frozen busyboy text "calibrating"
```

Expected: exit 0, silent, and the word appears on the bar. If this fails, stop — every later step depends on it.

- [ ] **Step 3: Run the calibration**

```bash
uv run --frozen python tools/capture_screen.py
```

Expected: an occupancy map per font, then the geometry table, then the tiny/tiny validation gate, then the recommended layout.

- [ ] **Step 4: Check the validation gate**

The tiny/tiny line must read `(1, 9)`, matching the layout in `bar.py` today. If the tool raised `CaptureError` here, **stop and report**. A failing gate means the offset model is wrong, and any constants derived from it would be guesses wearing a measurement's clothes. Do not proceed to Task 3.

- [ ] **Step 5: Record the results in this plan**

Fill in the Results block below with the actual tool output, then commit it. This is the plan's record of a fact that exists nowhere else.

**Results** (filled in by Step 5):

```
font          offset  height
tiny
small
normal
condensed
bold
large
extra_large

tiny/tiny validation gate:
ROW_ONE_FONT =        ROW_ONE_Y =
ROW_TWO_FONT =        ROW_TWO_Y =
```

- [ ] **Step 6: Commit the measurements**

```bash
git add docs/superpowers/plans/2026-08-02-workflow-row-font-calibration.md
git commit -m "Record measured BUSY Bar font geometry"
```

---

### Task 3: Apply the calibration to the workflow layout

**Files:**
- Modify: `src/busyboy/bar.py:45-58` (layout constants), `:156-167` (`_row`), `:188-203` (`build_workflow_payload`)
- Test: `tests/test_bar.py:240-253`

**Interfaces:**
- Consumes: the recorded values from Task 2's Results block.
- Produces: `bar.ROW_ONE_FONT`, `bar.ROW_TWO_FONT`, `bar.FONT_GLYPH_OFFSETS`, `bar.FONT_GLYPH_HEIGHTS`, revised `bar.ROW_ONE_Y` / `ROW_TWO_Y`, and `_row(element_id: str, text: str, y: int, font: DisplayFontName) -> TextElement`.

**Every number below marked `<from Task 2>` must be copied from Task 2's Results block.** The worked example in Step 3 uses the current tiny/tiny values purely to show the shape; do not ship it.

- [ ] **Step 1: Update the existing font assertion to be per-row**

In `tests/test_bar.py`, in `test_the_two_rows_share_a_text_column_beside_the_icon`, replace the loop body's font assertion. The loop becomes:

```python
    for row in (elements["repo"], elements["ref"]):
        assert row["x"] == bar.TEXT_X
        assert row["width"] == bar.TEXT_WIDTH
        assert row["display"] == "front"
    assert elements["repo"]["font"] == bar.ROW_ONE_FONT
    assert elements["ref"]["font"] == bar.ROW_TWO_FONT
    assert elements["repo"]["y"] == bar.ROW_ONE_Y
    assert elements["ref"]["y"] == bar.ROW_TWO_Y
    assert elements["icon"]["x"] == bar.ICON_X
    assert elements["icon"]["y"] == bar.ICON_Y
```

- [ ] **Step 2: Add the vertical-fit invariant test**

In `tests/test_bar.py`, directly after `test_the_icon_and_text_column_fit_the_front_display`:

```python
def test_the_two_rows_fit_the_front_display_without_overlapping():
    top_row = bar.ROW_ONE_Y + bar.FONT_GLYPH_OFFSETS[bar.ROW_ONE_FONT]
    bottom_row = bar.ROW_TWO_Y + bar.FONT_GLYPH_OFFSETS[bar.ROW_TWO_FONT]

    assert top_row >= 0
    assert top_row + bar.FONT_GLYPH_HEIGHTS[bar.ROW_ONE_FONT] <= bottom_row
    assert bottom_row + bar.FONT_GLYPH_HEIGHTS[bar.ROW_TWO_FONT] <= bar.FRONT_DISPLAY_HEIGHT
```

- [ ] **Step 3: Run both tests to verify they fail**

Run: `uv run --frozen pytest tests/test_bar.py -k "two_rows" -v`
Expected: FAIL — `AttributeError: module 'busyboy.bar' has no attribute 'ROW_ONE_FONT'`

- [ ] **Step 4: Replace the layout constants**

In `src/busyboy/bar.py`, replace lines 45-58 (the block from the `# Two-row workflow layout` comment through `ROW_FONT`) with the following. Substitute every `<from Task 2>`:

```python
# Two-row workflow layout on the 72x16 front display: a 12x12 icon on the
# left, and a text column beside it carrying both rows.
#
# Measured against a real bar with tools/capture_screen.py. A font's `y` is not
# its first inked row — each font sits `offset` rows below the y it is given —
# so placing a row deliberately needs both numbers. Heights are the inked rows
# a glyph box spans. "global" is absent: it selects the device's configured
# font rather than naming a size, so it has no fixed geometry.
FONT_GLYPH_OFFSETS: dict[DisplayFontName, int] = {
    "tiny": <from Task 2>,
    "small": <from Task 2>,
    "normal": <from Task 2>,
    "condensed": <from Task 2>,
    "bold": <from Task 2>,
    "large": <from Task 2>,
    "extra_large": <from Task 2>,
}
FONT_GLYPH_HEIGHTS: dict[DisplayFontName, int] = {
    "tiny": <from Task 2>,
    "small": <from Task 2>,
    "normal": <from Task 2>,
    "condensed": <from Task 2>,
    "bold": <from Task 2>,
    "large": <from Task 2>,
    "extra_large": <from Task 2>,
}

ICON_SIZE = 12
ICON_X = 2
ICON_Y = 2
TEXT_X = 18
TEXT_WIDTH = FRONT_DISPLAY_WIDTH - TEXT_X
ROW_ONE_FONT: DisplayFontName = <from Task 2>
ROW_TWO_FONT: DisplayFontName = <from Task 2>
ROW_ONE_Y = <from Task 2>
ROW_TWO_Y = <from Task 2>
```

Worked example of the shape only — these are today's tiny/tiny numbers, not the calibrated ones:

```python
FONT_GLYPH_OFFSETS: dict[DisplayFontName, int] = {"tiny": 0, "small": 1, ...}
ROW_ONE_FONT: DisplayFontName = "tiny"
ROW_ONE_Y = 1
```

- [ ] **Step 5: Give `_row` a font parameter**

In `src/busyboy/bar.py`, change `_row` (lines 156-167):

```python
def _row(element_id: str, text: str, y: int, font: DisplayFontName) -> TextElement:
    """Build one row of the workflow layout, scrolling when it overflows the column."""
    return TextElement(
        id=element_id,
        text=_to_displayable_ascii(text),
        font=font,
        display="front",
        x=TEXT_X,
        y=y,
        width=TEXT_WIDTH,
        scroll_rate=DEFAULT_SCROLL_RATE,
    )
```

- [ ] **Step 6: Pass each row its own font**

In `build_workflow_payload`, change the two `_row` calls:

```python
            _row(REPO_ELEMENT_ID, repo_label, ROW_ONE_Y, ROW_ONE_FONT),
            _row(REF_ELEMENT_ID, ref_label, ROW_TWO_Y, ROW_TWO_FONT),
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_bar.py -v`
Expected: PASS, all tests.

- [ ] **Step 8: Run the full suite**

Run: `make test`
Expected: lint clean, all tests pass. `ROW_FONT` is now gone — if anything still references it, `ty check` or the tests will say so.

- [ ] **Step 9: Verify on the real bar**

With the environment still pointed at the bar (`source ~/.zshrc && busybarenv`), run the watch against this repo for a few seconds and look at the display:

```bash
uv run --frozen busyboy gh workflow main.yaml
```

Expected: two rows, the repository slug in the larger font on top and the branch or PR below, both fully visible — neither clipped at the top or bottom, and not overlapping. Ctrl+C to stop.

Then capture the result for the record:

```bash
uv run --frozen python -c "
from busyboy.config import load_config
from tools.capture_screen import capture_front_frame, render_occupancy
print(render_occupancy(capture_front_frame(load_config())))
"
```

If the rows are clipped or overlapping despite the tests passing, stop and report — it means `FONT_GLYPH_OFFSETS` or `FONT_GLYPH_HEIGHTS` was mis-transcribed from Task 2.

- [ ] **Step 10: Commit**

```bash
git add src/busyboy/bar.py tests/test_bar.py
git commit -m "Calibrate the workflow rows to a larger top font"
```

---

### Task 4: Document the measurements

**Files:**
- Modify: `CLAUDE.md` (Architecture section, Hardware facts section)

**Interfaces:**
- Consumes: Task 2's Results block, Task 3's shipped constants.
- Produces: nothing code-facing.

- [ ] **Step 1: Add the tool to the Architecture section**

In `CLAUDE.md`, after the `src/busyboy/assets/` bullet, add:

```markdown
- `tools/capture_screen.py` — development tool, not part of the package: draws a probe string in each font and
  reads the framebuffer back through `GET /api/screen` to measure glyph offsets and heights. Run with
  `uv run --frozen python tools/capture_screen.py` against a real bar. This is how the font table in the
  hardware-facts section was produced; rerun it rather than guessing when a layout question comes up.
```

- [ ] **Step 2: Replace the tiny-stacking hardware fact**

In the Hardware facts section, replace the bullet beginning **"The `tiny` font stacks twice within the 16-row display"** with the measured table and the shipped layout. Substitute the Task 2 values:

```markdown
- **Font glyph geometry, measured with `tools/capture_screen.py`.** A font's `y` is not its first inked row —
  each font sits `offset` rows below the `y` it is given — so placing a row deliberately needs both numbers.
  `global` is absent: it selects the device's configured font rather than naming a size.

  | font | offset | height |
  |---|---|---|
  | tiny | <from Task 2> | <from Task 2> |
  | small | <from Task 2> | <from Task 2> |
  | normal | <from Task 2> | <from Task 2> |
  | condensed | <from Task 2> | <from Task 2> |
  | bold | <from Task 2> | <from Task 2> |
  | large | <from Task 2> | <from Task 2> |
  | extra_large | <from Task 2> | <from Task 2> |

  The workflow layout stacks `ROW_ONE_FONT` over `ROW_TWO_FONT` at `ROW_ONE_Y` / `ROW_TWO_Y` in `bar.py`,
  with leftover rows spread top margin, then gap, then bottom margin. `condensed` and anything taller cannot
  be stacked twice.
```

- [ ] **Step 3: Replace the lost-frame-capture paragraph**

In the "Testing against a real bar" subsection, replace the paragraph starting **"Frame capture for visual verification"** with:

```markdown
Frame capture for visual verification lives in `tools/capture_screen.py` (`GET /api/screen`). The response's
`Content-Type: image/bmp` header is misleading — there is no BMP header, just base64-encoded raw framebuffer
bytes: RGB888 for the front display (`display=0`), L4-packed (2 pixels/byte) for the back (`display=1`). The
tool decodes the front display only.
```

- [ ] **Step 4: Verify the docs claims are true**

Run: `uv run --frozen python -c "from busyboy import bar; print(bar.ROW_ONE_FONT, bar.ROW_ONE_Y, bar.ROW_TWO_FONT, bar.ROW_TWO_Y)"`
Expected: matches what Step 2's prose claims.

Run: `make test`
Expected: still clean — `docs` is ruff-excluded but `CLAUDE.md` is not a code file, so this only confirms nothing else broke.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the measured front display font geometry"
```

---

## Done when

- `tools/capture_screen.py` runs against a real bar and prints a geometry table.
- The tiny/tiny validation gate passes.
- `busyboy gh workflow` shows a larger top row and a smaller bottom row, both unclipped.
- `make test` is clean.
- `CLAUDE.md` carries the measured table, and no longer says frame capture is unavailable.
