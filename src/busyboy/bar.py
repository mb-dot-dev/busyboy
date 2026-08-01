"""Construction and delivery of BUSY Bar display payloads."""

import time
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator
from pydantic_extra_types.color import Color
import requests

from busyboy import exceptions
from busyboy.config import BusyboyConfig

APPLICATION_NAME = "busyboy"
TEXT_ELEMENT_ID = "text"

# The front display is a 72x16 RGB LED matrix; spanning its full width gives
# scrolling text the whole panel to move across.
FRONT_DISPLAY_WIDTH = 72

DisplayFontName = Literal["tiny", "small", "normal", "condensed", "bold", "large", "extra_large", "global"]
DEFAULT_FONT: DisplayFontName = "condensed"
FONT_NAMES: tuple[str, ...] = get_args(DisplayFontName)

DisplayName = Literal["front", "back"]

# Measured against a real bar: scroll_rate is pixels per minute (higher is
# faster), and text scrolls leftward. At 1200 the observed speed is ~18 px/s,
# crossing the 72px display in roughly four seconds.
DEFAULT_SCROLL_RATE = 1200

# Measured against a real bar: align="center" clips text off the top of the
# display (glyphs occupy only rows 0-3). The condensed font's glyph box is 9
# rows tall, so an explicit y=2 centers it on the 16-row display (rows 4-12).
DEFAULT_TEXT_Y = 2

DISPLAY_DRAW_PATH = "/api/display/draw"

# Matches busylib's previous defaults: an overall 10s timeout with a 5s cap on
# establishing the connection.
REQUEST_TIMEOUT = (5, 10)
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.25


def _normalize_color(value: str | None) -> str | None:
    """Normalize a CSS name, hex, or rgb() string to #RRGGBBAA."""
    if value is None:
        return None
    hex_value = Color(value).as_hex("long").upper()
    if len(hex_value) == 7:
        return f"{hex_value}FF"
    return hex_value


class TextElement(BaseModel):
    """A single line of text on one of the bar's displays."""

    id: str
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)
    font: DisplayFontName
    color: str | None = Field(default=None, pattern=r"^#[0-9A-F]{8}$")
    timeout: int | None = Field(default=None, ge=0)
    display: DisplayName = "front"
    y: int = 0
    width: int | None = Field(default=None, ge=1)
    scroll_rate: int | None = Field(default=None, ge=0)

    @field_validator("color", mode="before")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        return _normalize_color(value)


class DisplayElements(BaseModel):
    """A draw request: one application's elements for the bar to render."""

    application_name: str
    elements: list[TextElement] = Field(min_length=1)


def build_text_payload(
    text: str,
    *,
    font: DisplayFontName = DEFAULT_FONT,
    color: str | None = None,
    timeout: int | None = None,
    scroll_rate: int = DEFAULT_SCROLL_RATE,
) -> DisplayElements:
    """
    Build the draw payload for a single centered line on the front display.

    `color` and `timeout` stay None when unset so they're excluded from the
    request body and the firmware's own defaults apply.
    """
    element = TextElement(
        id=TEXT_ELEMENT_ID,
        text=text,
        font=font,
        color=color,
        timeout=timeout,
        display="front",
        y=DEFAULT_TEXT_Y,
        width=FRONT_DISPLAY_WIDTH,
        scroll_rate=scroll_rate,
    )
    return DisplayElements(application_name=APPLICATION_NAME, elements=[element])


def _base_url(host: str) -> str:
    """Normalize a bare host/IP to an http:// URL."""
    return host if "://" in host else f"http://{host}"


def _auth_headers(config: BusyboyConfig) -> dict[str, str]:
    """Build the X-API-Token header, or no headers when no token is set."""
    token = config.token_value
    return {"X-API-Token": token} if token else {}


def _raise_for_error_response(response: requests.Response, *, method: str) -> None:
    """Convert an HTTP error response into a BarAPIError."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    raise exceptions.BarAPIError(
        error or response.text or f"HTTP {response.status_code}",
        code=code if isinstance(code, int) else None,
        status_code=response.status_code,
        method=method,
        path=DISPLAY_DRAW_PATH,
    )


def _request(
    config: BusyboyConfig,
    method: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> None:
    """
    Send one request to the bar, retrying transport-level failures.

    Only connection errors and timeouts are retried, up to MAX_RETRIES extra
    attempts with growing backoff. An HTTP error response raises immediately.
    """
    url = f"{_base_url(config.host)}{DISPLAY_DRAW_PATH}"
    headers = _auth_headers(config)
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as error:
            if attempt >= MAX_RETRIES:
                raise exceptions.BarRequestError(
                    str(error),
                    method=method,
                    path=DISPLAY_DRAW_PATH,
                    attempts=attempt + 1,
                ) from error
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        if response.status_code >= 400:
            _raise_for_error_response(response, method=method)
        return


def draw_text(config: BusyboyConfig, payload: DisplayElements) -> None:
    """Send a draw payload to the bar."""
    _request(config, "POST", json_body=payload.model_dump(exclude_none=True))


def clear(config: BusyboyConfig) -> None:
    """Remove what busyboy drew, without touching other applications' elements."""
    _request(config, "DELETE", params={"application_name": APPLICATION_NAME})
