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

# Measured on real hardware: normal at y=-2 inks rows 0-8, exactly as its
# offset of 2 predicts. Negative y is legal, and necessary — every font except
# tiny sits 2 rows below the y it is given, so a row inked from row 0 needs -2.
MINIMUM_Y = -8


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
        cells = "".join("#" if _pixel_is_lit(frame, row, column) else "." for column in range(bar.FRONT_DISPLAY_WIDTH))
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
    if len(rows) == bar.FRONT_DISPLAY_HEIGHT:
        # An idle bar runs a full-screen animated app. Drawing a busyboy element
        # normally blanks it, so every row being lit means the capture caught the
        # ambient display instead of the probe — measuring it would report every
        # font as 16 rows tall.
        raise CaptureError(f"{font}: every row is lit — captured the bar's ambient display, not the probe")
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

    Leftover rows are spread top margin, then gap, then bottom margin. Margins
    are in inked-row space; y is that target minus the font's own offset, and
    may legitimately be negative — see MINIMUM_Y.
    """
    leftover = bar.FRONT_DISPLAY_HEIGHT - heights[top] - heights[bottom]
    if leftover < 0:
        raise CaptureError(f"{top} over {bottom} needs {-leftover} more rows than the display has")
    margins = [leftover // 3] * 3
    for index in range(leftover % 3):
        margins[index] += 1
    top_margin, gap, _ = margins
    bottom_row = top_margin + heights[top] + gap
    return top_margin - offsets[top], bottom_row - offsets[bottom]


def check_layout(
    top: str,
    bottom: str,
    row_one_y: int,
    row_two_y: int,
    offsets: dict[str, int],
    heights: dict[str, int],
) -> None:
    """
    Verify a computed layout neither clips nor overlaps.

    This replaces an earlier gate that asserted place_rows reproduces the
    shipped tiny/tiny layout of y=1 and y=9. That layout was hand-tuned rather
    than rule-derived — top margin 2, gap 3, bottom margin 1 — so demanding the
    even-margin rule reproduce it rejected layouts that are perfectly valid.
    What actually matters is the property, not the historical constant: both
    rows fully on the display, and not touching each other.
    """
    top_first = row_one_y + offsets[top]
    bottom_first = row_two_y + offsets[bottom]
    if row_one_y < MINIMUM_Y or row_two_y < MINIMUM_Y:
        raise CaptureError(f"y below {MINIMUM_Y} is beyond what has been measured: {row_one_y}, {row_two_y}")
    if top_first < 0:
        raise CaptureError(f"{top} at y={row_one_y} inks from row {top_first}, above the display")
    if top_first + heights[top] > bottom_first:
        raise CaptureError(f"{top} at y={row_one_y} overlaps {bottom} at y={row_two_y}")
    if bottom_first + heights[bottom] > bar.FRONT_DISPLAY_HEIGHT:
        raise CaptureError(f"{bottom} at y={row_two_y} inks past row {bar.FRONT_DISPLAY_HEIGHT - 1}")


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

    # The shipped tiny/tiny layout is known to render correctly, so whatever
    # else changes, the rules must still accept it.
    check_layout("tiny", "tiny", *place_rows("tiny", "tiny", offsets, heights), offsets, heights)
    print("\ntiny/tiny sanity check: passes")

    top, bottom = select_pair(heights)
    row_one_y, row_two_y = place_rows(top, bottom, offsets, heights)
    check_layout(top, bottom, row_one_y, row_two_y, offsets, heights)
    print("\n=== recommended layout ===")
    print(f"ROW_ONE_FONT = {top!r}   ROW_ONE_Y = {row_one_y}")
    print(f"ROW_TWO_FONT = {bottom!r}   ROW_TWO_Y = {row_two_y}")
    print(f"  {top} inks rows {row_one_y + offsets[top]}-{row_one_y + offsets[top] + heights[top] - 1}")
    print(f"  {bottom} inks rows {row_two_y + offsets[bottom]}-{row_two_y + offsets[bottom] + heights[bottom] - 1}")


if __name__ == "__main__":
    main()
