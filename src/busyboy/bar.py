"""Construction and delivery of BUSY Bar display payloads."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import dataclasses
from importlib import resources
import logging
import threading
from typing import Annotated, Literal, NoReturn, Protocol, Self, cast, get_args

import busylib
from busylib import exceptions as busylib_exceptions
from busylib.features.input_events import ButtonEvent, input_events
from pydantic import BaseModel, Field, field_validator
from pydantic_extra_types.color import Color

from busyboy import exceptions
from busyboy.config import BusyboyConfig

LOGGER = logging.getLogger(__name__)

APPLICATION_NAME = "busyboy"
TEXT_ELEMENT_ID = "text"

# The front display is a 72x16 RGB LED matrix; spanning its full width gives
# scrolling text the whole panel to move across.
FRONT_DISPLAY_WIDTH = 72
FRONT_DISPLAY_HEIGHT = 16

DisplayFontName = Literal["tiny", "small", "normal", "condensed", "bold", "large", "extra_large", "global"]
# Every font except "global", which selects whatever font the device is configured for rather than naming a
# size. It therefore has no fixed glyph geometry, which is why FONT_GLYPH_OFFSETS and FONT_GLYPH_HEIGHTS are
# keyed by this narrower type: annotating them with DisplayFontName would promise a "global" entry that
# cannot exist, turning a lookup that type-checks into a KeyError at runtime.
MeasuredFontName = Literal["tiny", "small", "normal", "condensed", "bold", "large", "extra_large"]
DEFAULT_FONT: DisplayFontName = "condensed"

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
# Typed tuple[IconName, ...] rather than tuple[str, ...] so the literal type survives iteration and no use
# site needs a cast. get_args() itself is untyped (tuple[Any, ...]), so one cast here is unavoidable.
ICON_NAMES: tuple[IconName, ...] = cast(tuple[IconName, ...], get_args(IconName))
ASSETS_PACKAGE = "busyboy.assets"

# Two-row workflow layout on the 72x16 front display: a 12x12 icon on the
# left, and a text column beside it carrying both rows.
#
# Measured against a real bar with tools/capture_screen.py. A font's `y` is not
# its first inked row — each font sits `offset` rows below the y it is given —
# so placing a row deliberately needs both numbers. Heights are the inked rows
# a glyph box spans. Keyed by MeasuredFontName, so "global" is absent by
# construction rather than by convention.
FONT_GLYPH_OFFSETS: dict[MeasuredFontName, int] = {
    "tiny": 1,
    "small": 2,
    "normal": 2,
    "condensed": 2,
    "bold": 2,
    "large": 2,
    "extra_large": 2,
}
FONT_GLYPH_HEIGHTS: dict[MeasuredFontName, int] = {
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
ROW_ONE_FONT: MeasuredFontName = "normal"
ROW_TWO_FONT: MeasuredFontName = "small"
ROW_ONE_Y = -2
ROW_TWO_Y = 7

REPO_ELEMENT_ID = "repo"
REF_ELEMENT_ID = "ref"
ICON_ELEMENT_ID = "icon"

# One-limit layout on the 72x16 front display: a short `small` text label in rows 0-6 and a 7-row bar in rows
# 9-15 (row 7 and 8 left dark). `small`'s glyph box sits two rows below the `y` it is given
# (FONT_GLYPH_OFFSETS), so `y=-2` inks from row 0. Filled rectangles need device API >= 24.3.0.
LIMITS_TRACK_X = 1
LIMITS_TRACK_WIDTH = 70
LIMITS_TRACK_HEIGHT = 7
LIMITS_BAR_Y = 9
LIMITS_TRACK_COLOR = "#1F1F1F"
LIMITS_FILL_COLOR = "#CD6E58"
LIMITS_HIGH_FILL_COLOR = "#B3261E"
LIMITS_HIGH_THRESHOLD = 85
LIMITS_LABEL_X = 1
LIMITS_LABEL_Y = -2
LIMITS_LABEL_FONT: MeasuredFontName = "small"
LIMITS_LABEL_COLOR = "#E6E6E6"


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


RgbaColor = Annotated[str, Field(pattern=r"^#[0-9A-F]{8}$")]


class RectangleElement(BaseModel):
    """A filled rectangle, mirroring the vendored OpenAPI schema's RectangleElement."""

    id: str
    type: Literal["rectangle"] = "rectangle"
    display: DisplayName = "front"
    x: int = 0
    y: int = 0
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    radius: int = Field(default=0, ge=0)
    fill: Literal["none", "solid", "gradient_h", "gradient_v"] = "solid"
    fill_colors: list[RgbaColor] = Field(min_length=1, max_length=2)
    # Deliberately 0, unlike the vendor default of 1: a bar's track and fill must not grow a white outline.
    border_width: int = Field(default=0, ge=0)

    @field_validator("fill_colors", mode="before")
    @classmethod
    def _validate_fill_colors(cls, value: list[str]) -> list[str | None]:
        return [_normalize_color(color) for color in value]


class DisplayElements(BaseModel):
    """A draw request: one application's elements for the bar to render."""

    application_name: str
    elements: list[TextElement | ImageElement | RectangleElement] = Field(min_length=1)


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


def strip_undisplayable(text: str) -> str:
    """
    Drop every character the display's bitmap fonts cannot render, collapsing the leftover whitespace.

    Unlike `_to_displayable_ascii`, this may return "". It is for text that is
    one *component* of a row rather than a whole row — text where some other
    component already guarantees `TextElement.text`'s `min_length=1`, so
    dropping characters cannot produce an invalid payload. That is what buys
    the nicer rendering: a workflow named "🚀 Deploy" reads as "Deploy" here,
    where one-for-one replacement would leave a stray "?" in front of it.
    Callers must handle the empty result.
    """
    displayable = "".join(character for character in text if "\x20" <= character <= "\x7e")
    return " ".join(displayable.split())


def build_workflow_payload(*, repo_label: str, ref_label: str, icon: IconName) -> DisplayElements:
    """
    Build the two-row workflow layout: repository on top; pull request or branch and the workflow name below;
    a status icon to their left.

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


@dataclasses.dataclass(frozen=True)
class LimitView:
    """One limit as the bar shows it: a short text `label` over a bar filled to a 0-100 `percent`."""

    label: str
    percent: float


def build_limits_payload(view: LimitView) -> DisplayElements:
    """
    Build the one-limit layout: a text label above a progress bar.

    Ids are stable, so a redraw replaces rather than stacks, including when the view switches between limits.
    The fill is always drawn: a draw never removes omitted ids, so a bar that has reset to ~0 must overwrite
    its old fill rather than leave it out. An empty bar gets a 1px fill in the track colour (a rectangle
    cannot be 0 wide), which is invisible against the track.
    """
    high = view.percent > LIMITS_HIGH_THRESHOLD
    fill_width = min(max(round(view.percent / 100 * LIMITS_TRACK_WIDTH), 0), LIMITS_TRACK_WIDTH)
    empty = fill_width == 0
    return DisplayElements(
        application_name=APPLICATION_NAME,
        elements=[
            RectangleElement(
                id="limit-track",
                x=LIMITS_TRACK_X,
                y=LIMITS_BAR_Y,
                width=LIMITS_TRACK_WIDTH,
                height=LIMITS_TRACK_HEIGHT,
                fill_colors=[LIMITS_TRACK_COLOR],
            ),
            RectangleElement(
                id="limit-fill",
                x=LIMITS_TRACK_X,
                y=LIMITS_BAR_Y,
                width=max(fill_width, 1),
                height=LIMITS_TRACK_HEIGHT,
                fill_colors=[LIMITS_TRACK_COLOR if empty else LIMITS_HIGH_FILL_COLOR if high else LIMITS_FILL_COLOR],
            ),
            TextElement(
                id="limit-label",
                text=_to_displayable_ascii(view.label),
                font=LIMITS_LABEL_FONT,
                color=LIMITS_LABEL_COLOR,
                display="front",
                x=LIMITS_LABEL_X,
                y=LIMITS_LABEL_Y,
            ),
        ],
    )


def _client(config: BusyboyConfig) -> busylib.BusyBar:
    """
    Build a busylib client carrying busyboy's own host/token configuration for one call.

    Callers close it with a plain `client.close()` in a `finally` block rather
    than opening it as `with _client(config) as client:`. busylib's
    `SyncClientBase.__enter__` is annotated to return `SyncClientBase` rather
    than `Self`, which would erase the mixin methods (`display_draw`,
    `assets_upload`, ...) from the type checker's view of `client` inside a
    `with` block.
    """
    return busylib.BusyBar(config.host, token=config.token_value)


def _reraise_as_bar_error(error: busylib_exceptions.BusyBarError) -> NoReturn:
    """
    Map a busylib delivery failure onto busyboy's own two-branch exception hierarchy.

    `BusyBarAPIError` carries the HTTP status/response detail `BarAPIError`
    needs. Every other `BusyBarError` — transport failures, and the rarer
    protocol or response-shape errors busylib defines for its own broader
    feature set — becomes `BarRequestError`, so `watch.tick` and the CLI can
    keep treating "the bar delivery failed" as one thing regardless of which
    busylib exception produced it.
    """
    if isinstance(error, busylib_exceptions.BusyBarAPIError):
        raise exceptions.BarAPIError(
            error.error,
            code=error.code,
            status_code=error.status_code or 0,
            method=error.method or "",
            path=error.path or "",
        ) from error
    if isinstance(error, busylib_exceptions.BusyBarRequestError):
        raise exceptions.BarRequestError(
            error.message,
            method=error.method or "",
            path=error.path or "",
            attempts=error.attempts or 1,
        ) from error
    raise exceptions.BarRequestError(str(error), method="", path="", attempts=1) from error


def draw_text(config: BusyboyConfig, payload: DisplayElements) -> None:
    """Send a draw payload to the bar."""
    client = _client(config)
    try:
        client.display_draw(payload.model_dump(exclude_none=True))
    except busylib_exceptions.BusyBarError as error:
        _reraise_as_bar_error(error)
    finally:
        client.close()


def clear(config: BusyboyConfig) -> None:
    """Remove what busyboy drew, without touching other applications' elements."""
    client = _client(config)
    try:
        client.display_clear(application_name=APPLICATION_NAME)
    except busylib_exceptions.BusyBarError as error:
        _reraise_as_bar_error(error)
    finally:
        client.close()


def icon_bytes(icon: IconName) -> bytes:
    """Read one packaged icon PNG."""
    return (resources.files(ASSETS_PACKAGE) / f"{icon}.png").read_bytes()


def upload_icons(config: BusyboyConfig) -> None:
    """
    Upload every status icon to the bar's asset store for this application.

    The upload is unconditional: the bar's API has no endpoint that lists an
    app's existing assets, and six ~200-byte requests cost less than the
    machinery to avoid them. One client is reused across all six calls.
    """
    client = _client(config)
    try:
        for icon in ICON_NAMES:
            client.assets_upload(APPLICATION_NAME, f"{icon}.png", icon_bytes(icon))
    except busylib_exceptions.BusyBarError as error:
        _reraise_as_bar_error(error)
    finally:
        client.close()


LISTENER_RETRY_INITIAL_SECONDS = 1.0
LISTENER_RETRY_MAX_SECONDS = 30.0
LISTENER_STOP_TIMEOUT_SECONDS = 2.0

type StatusStream = Callable[[], AsyncIterator[object]]


async def _status_stream(config: BusyboyConfig) -> AsyncIterator[object]:
    """Yield the bar's status messages over busylib's WebSocket, closing the client when the stream ends."""
    client = busylib.AsyncBusyBar(config.host, token=config.token_value)
    try:
        async for message in client.stream_status_ws():
            yield message
    finally:
        await client.aclose()


class ButtonListener(Protocol):
    """What the poll loop needs from a button listener; `StartButtonListener` is the real one."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wait_for_press(self, timeout: float) -> bool: ...


class StartButtonListener:
    """
    Tell a synchronous caller when the bar's Start/Pause button is pressed.

    busylib reports physical input only on its async status WebSocket, and busyboy's poll loops are synchronous,
    so the stream is consumed on a daemon thread with its own event loop. A press sets a `threading.Event` that
    `wait_for_press` waits on. The thread never raises into the caller: a dropped or failing stream is retried
    with capped exponential backoff, and the button simply does nothing until it reconnects. `stream` is the
    seam tests use in place of the real WebSocket.
    """

    def __init__(self, config: BusyboyConfig, *, stream: StatusStream | None = None) -> None:
        self._stream: StatusStream = stream if stream is not None else lambda: _status_stream(config)
        self._pressed = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin listening on a background thread; returns once the thread's loop is running."""
        self._thread = threading.Thread(target=self._run, name="busyboy-start-button", daemon=True)
        self._thread.start()
        self._ready.wait()

    def stop(self) -> None:
        """Stop listening and wait briefly for the thread to end. Safe to call when never started."""
        if self._thread is None:
            return
        if self._loop is not None and self._task is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                LOGGER.debug("Button listener loop was already closed")
        self._thread.join(timeout=LISTENER_STOP_TIMEOUT_SECONDS)

    def wait_for_press(self, timeout: float) -> bool:
        """Block up to `timeout` seconds; return True, consuming the press, if Start was pressed meanwhile."""
        if self._pressed.wait(timeout):
            self._pressed.clear()
            return True
        return False

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._task = loop.create_task(self._listen())
        self._ready.set()
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            LOGGER.debug("Button listener stopped")
        finally:
            loop.close()

    async def _listen(self) -> None:
        delay = LISTENER_RETRY_INITIAL_SECONDS
        while True:
            try:
                async for message in self._stream():
                    delay = LISTENER_RETRY_INITIAL_SECONDS
                    if isinstance(message, Mapping) and any(
                        isinstance(event, ButtonEvent) and event.button == "start" and event.is_press
                        for event in input_events(message)
                    ):
                        self._pressed.set()
            except Exception as error:  # noqa: BLE001 - nothing the stream does may reach the poll loop
                LOGGER.debug("Button stream failed, retrying: %s", error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, LISTENER_RETRY_MAX_SECONDS)
