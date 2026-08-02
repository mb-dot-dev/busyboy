"""Construction and delivery of BUSY Bar display payloads."""

from importlib import resources
import time
from typing import Literal, cast, get_args

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
FRONT_DISPLAY_HEIGHT = 16

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

IconName = Literal["success", "failure", "pending", "in_progress", "cancelled", "skipped"]
# Unlike FONT_NAMES (kept as tuple[str, ...] because click.Choice wants Sequence[str]), ICON_NAMES never
# touches Click — it's only iterated internally, so typing it tuple[IconName, ...] preserves the literal
# type through iteration and avoids a cast at every use site. get_args() itself is untyped (tuple[Any, ...]),
# so one cast here is unavoidable.
ICON_NAMES: tuple[IconName, ...] = cast(tuple[IconName, ...], get_args(IconName))
ASSETS_PACKAGE = "busyboy.assets"

# Two-row workflow layout on the 72x16 front display: a 12x12 icon on the
# left, and a text column beside it carrying both rows.
#
# Measured against a real bar with tools/capture_screen.py. A font's `y` is not
# its first inked row — each font sits `offset` rows below the y it is given —
# so placing a row deliberately needs both numbers. Heights are the inked rows
# a glyph box spans. "global" is absent: it selects the device's configured
# font rather than naming a size, so it has no fixed geometry.
FONT_GLYPH_OFFSETS: dict[DisplayFontName, int] = {
    "tiny": 1,
    "small": 2,
    "normal": 2,
    "condensed": 2,
    "bold": 2,
    "large": 2,
    "extra_large": 2,
}
FONT_GLYPH_HEIGHTS: dict[DisplayFontName, int] = {
    "tiny": 5,
    "small": 7,
    "normal": 9,
    "condensed": 9,
    "bold": 9,
    "large": 11,
    "extra_large": 10,
}

ICON_SIZE = 12
ICON_X = 2
ICON_Y = 2
TEXT_X = 18
TEXT_WIDTH = FRONT_DISPLAY_WIDTH - TEXT_X
ROW_ONE_FONT: DisplayFontName = "normal"
ROW_TWO_FONT: DisplayFontName = "small"
ROW_ONE_Y = -2
ROW_TWO_Y = 7

REPO_ELEMENT_ID = "repo"
REF_ELEMENT_ID = "ref"
ICON_ELEMENT_ID = "icon"

DISPLAY_DRAW_PATH = "/api/display/draw"
ASSET_UPLOAD_PATH = "/api/assets/upload"

# A 5s cap on establishing the connection, and a 10s cap on each individual
# socket read (not an overall request budget — retries can each take up to
# 15s).
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
    text: str = Field(min_length=1, pattern=r"^[\x20-\x7E]+$")
    font: DisplayFontName
    color: str | None = Field(default=None, pattern=r"^#[0-9A-F]{8}$")
    timeout: int | None = Field(default=None, ge=0)
    display: DisplayName = "front"
    x: int = 0
    y: int = 0
    width: int | None = Field(default=None, ge=1)
    scroll_rate: int | None = Field(default=None, ge=0)

    @field_validator("color", mode="before")
    @classmethod
    def _validate_color(cls, value: str | None) -> str | None:
        return _normalize_color(value)


class ImageElement(BaseModel):
    """An image drawn from a file previously uploaded to the app's assets."""

    id: str
    type: Literal["image"] = "image"
    # The bar's own OpenAPI spec allows a broader pattern here (letters, digits, `.`, `_`, `/`, `-`), which
    # would let a traversal-style value like "../../etc/passwd" through. busyboy only ever constructs
    # `path` as f"{icon}.png" from the closed IconName Literal, so this is deliberately narrower than the
    # vendor contract — do not widen it back to match the spec.
    path: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.png$")
    display: DisplayName = "front"
    x: int = 0
    y: int = 0


class DisplayElements(BaseModel):
    """A draw request: one application's elements for the bar to render."""

    application_name: str
    elements: list[TextElement | ImageElement] = Field(min_length=1)


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


def _to_displayable_ascii(text: str) -> str:
    """
    Replace every character outside printable ASCII (\\x20-\\x7E) with '?'.

    The front display's fonts are bitmap ASCII, so the device itself cannot
    render anything else — a repo or branch name containing unicode (both are
    permitted by GitHub) would otherwise fail `TextElement.text`'s pattern and
    raise a `pydantic.ValidationError` out of payload construction. Characters
    are replaced one-for-one rather than dropped, so a non-empty input always
    produces a non-empty, valid result (`TextElement.text` requires
    `min_length=1`) — even when every character is non-ASCII. An empty input
    returns "?" for the same reason.
    """
    if not text:
        return "?"
    return "".join(character if "\x20" <= character <= "\x7e" else "?" for character in text)


def build_workflow_payload(*, repo_label: str, ref_label: str, icon: IconName) -> DisplayElements:
    """
    Build the two-row workflow layout: repository, pull request or branch, and a status icon.

    Element ids are stable, so redrawing replaces the previous elements rather
    than stacking new ones on top of them. Labels are sanitized to printable
    ASCII before reaching `TextElement`; see `_to_displayable_ascii`.
    """
    return DisplayElements(
        application_name=APPLICATION_NAME,
        elements=[
            ImageElement(id=ICON_ELEMENT_ID, path=f"{icon}.png", display="front", x=ICON_X, y=ICON_Y),
            _row(REPO_ELEMENT_ID, repo_label, ROW_ONE_Y, ROW_ONE_FONT),
            _row(REF_ELEMENT_ID, ref_label, ROW_TWO_Y, ROW_TWO_FONT),
        ],
    )


def _base_url(host: str) -> str:
    """Normalize a bare host/IP to an http:// URL."""
    return host if "://" in host else f"http://{host}"


def _auth_headers(config: BusyboyConfig) -> dict[str, str]:
    """Build the X-API-Token header, or no headers when no token is set."""
    token = config.token_value
    return {"X-API-Token": token} if token else {}


def _raise_for_error_response(response: requests.Response, *, method: str, path: str) -> None:
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
        path=path,
    )


def _request(
    config: BusyboyConfig,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> None:
    """
    Send one request to the bar, retrying transport-level failures.

    Only connection errors and timeouts are retried, up to MAX_RETRIES extra
    attempts with growing backoff. An HTTP error response raises immediately,
    and so does any other request failure (e.g. a malformed URL) — those are
    deterministic, so retrying them would just add pointless backoff delay.
    """
    url = f"{_base_url(config.host)}{path}"
    headers = _auth_headers(config)
    if content_type is not None:
        headers["Content-Type"] = content_type
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if attempt >= MAX_RETRIES:
                raise exceptions.BarRequestError(
                    str(error),
                    method=method,
                    path=path,
                    attempts=attempt + 1,
                ) from error
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        except requests.exceptions.RequestException as error:
            raise exceptions.BarRequestError(
                str(error),
                method=method,
                path=path,
                attempts=attempt + 1,
            ) from error
        try:
            if response.status_code >= 400:
                _raise_for_error_response(response, method=method, path=path)
        finally:
            response.close()
        return


def draw_text(config: BusyboyConfig, payload: DisplayElements) -> None:
    """Send a draw payload to the bar."""
    _request(config, "POST", DISPLAY_DRAW_PATH, json_body=payload.model_dump(exclude_none=True))


def clear(config: BusyboyConfig) -> None:
    """Remove what busyboy drew, without touching other applications' elements."""
    _request(config, "DELETE", DISPLAY_DRAW_PATH, params={"application_name": APPLICATION_NAME})


def icon_bytes(icon: IconName) -> bytes:
    """Read one packaged icon PNG."""
    return (resources.files(ASSETS_PACKAGE) / f"{icon}.png").read_bytes()


def upload_icons(config: BusyboyConfig) -> None:
    """
    Upload every status icon to the bar's asset store for this application.

    The upload is unconditional: the bar's API has no endpoint that lists an
    app's existing assets, and six ~200-byte requests cost less than the
    machinery to avoid them.
    """
    for icon in ICON_NAMES:
        _request(
            config,
            "POST",
            ASSET_UPLOAD_PATH,
            params={"application_name": APPLICATION_NAME, "file": f"{icon}.png"},
            data=icon_bytes(icon),
            content_type="application/octet-stream",
        )
