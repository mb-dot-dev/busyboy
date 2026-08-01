"""Construction and delivery of BUSY Bar display payloads."""

from typing import get_args

from busylib import BusyBar, types
import httpx

from busyboy.config import BusyboyConfig

APPLICATION_NAME = "busyboy"
TEXT_ELEMENT_ID = "text"

# The front display is a 72x16 RGB LED matrix; spanning its full width gives
# scrolling text the whole panel to move across.
FRONT_DISPLAY_WIDTH = 72

DEFAULT_FONT: types.DisplayFontName = "condensed"
FONT_NAMES: tuple[str, ...] = get_args(types.DisplayFontName)

# Units are undocumented in busylib; see Task 5 for calibration against a
# real bar.
DEFAULT_SCROLL_RATE = 20


def open_client(
    config: BusyboyConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> BusyBar:
    """
    Build a client for the configured bar.

    Passing both a host and a token puts busylib in network mode, which sends
    the token as X-API-Token. `transport` exists so tests can substitute an
    httpx.MockTransport.
    """
    return BusyBar(
        config.host,
        token=config.token.get_secret_value(),
        transport=transport,
    )


def build_text_payload(
    text: str,
    *,
    font: types.DisplayFontName = DEFAULT_FONT,
    color: str | None = None,
    timeout: int | None = None,
    scroll_rate: int = DEFAULT_SCROLL_RATE,
) -> types.DisplayElements:
    """
    Build the draw payload for a single centered line on the front display.

    `color` and `timeout` stay None when unset so busylib drops them from the
    request body and the firmware's own defaults apply.
    """
    element = types.TextElement(
        id=TEXT_ELEMENT_ID,
        text=text,
        font=font,
        color=color,
        timeout=timeout,
        display=types.DisplayName.FRONT,
        align="center",
        width=FRONT_DISPLAY_WIDTH,
        scroll_rate=scroll_rate,
    )
    return types.DisplayElements(
        application_name=APPLICATION_NAME,
        elements=[element],
    )


def draw_text(client: BusyBar, payload: types.DisplayElements) -> None:
    """Send a draw payload to the bar."""
    client.display_draw(payload)


def clear(client: BusyBar) -> None:
    """Remove what busyboy drew."""
    client.display_clear()
