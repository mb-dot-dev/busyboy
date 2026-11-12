"""Tests for BUSY Bar payload construction and delivery."""

import json
import struct
import time

import httpx2
from pydantic import ValidationError
import pytest

from busyboy import bar, exceptions
from busyboy.config import load_config


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


@pytest.fixture
def config():
    return load_config(host="10.0.4.20", token="testtoken")


def element_of(payload):
    """Return the single element from a payload, as the wire dict."""
    return payload.model_dump(exclude_none=True)["elements"][0]


def test_payload_carries_one_text_element():
    payload = bar.build_text_payload("BUILD OK").model_dump(exclude_none=True)

    assert payload["application_name"] == "busyboy"
    assert len(payload["elements"]) == 1
    assert payload["elements"][0]["type"] == "text"
    assert payload["elements"][0]["text"] == "BUILD OK"


def test_element_id_is_stable_so_a_redraw_replaces():
    first = element_of(bar.build_text_payload("one"))
    second = element_of(bar.build_text_payload("two"))

    assert first["id"] == second["id"]


def test_defaults_centre_condensed_text_across_the_front_display():
    element = element_of(bar.build_text_payload("hi"))

    assert element["font"] == "condensed"
    assert element["display"] == "front"
    assert element["y"] == bar.DEFAULT_TEXT_Y
    assert element["width"] == 72
    assert element["scroll_rate"] == bar.DEFAULT_SCROLL_RATE


def test_colour_and_timeout_are_absent_when_unset():
    element = element_of(bar.build_text_payload("hi"))

    assert "color" not in element
    assert "timeout" not in element


def test_colour_and_timeout_are_sent_when_given():
    element = element_of(bar.build_text_payload("hi", color="red", timeout=30))

    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30


def test_a_colour_with_alpha_keeps_its_alpha_channel():
    element = element_of(bar.build_text_payload("hi", color="#FF000080"))
    assert element["color"] == "#FF000080"

    element = element_of(bar.build_text_payload("hi", color="#F008"))
    assert element["color"] == "#FF000088"


def test_scrolling_can_be_switched_off():
    element = element_of(bar.build_text_payload("hi", scroll_rate=0))

    assert element["scroll_rate"] == 0


def test_a_negative_timeout_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", timeout=-1)


def test_an_unrecognised_colour_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", color="definitely-not-a-colour")


def test_non_ascii_text_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("héllo")


def test_draw_text_posts_the_payload(config, bar_transport):
    bar_transport.add("POST", "/api/display/draw")

    payload = bar.build_text_payload("BUILD OK")
    bar.draw_text(config, payload)

    assert len(bar_transport.calls) == 1
    request = bar_transport.calls[0]
    assert request.method == "POST"
    assert request.url.path == "/api/display/draw"
    assert request.headers["X-API-Token"] == "testtoken"
    body = json.loads(request.content)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_clear_deletes_the_drawing(config, bar_transport):
    bar_transport.add("DELETE", "/api/display/draw")

    bar.clear(config)

    assert len(bar_transport.calls) == 1
    request = bar_transport.calls[0]
    assert request.method == "DELETE"
    assert dict(request.url.params) == {"application_name": "busyboy"}


def test_draw_text_omits_the_token_header_when_none_is_configured(bar_transport):
    bar_transport.add("POST", "/api/display/draw")

    config = load_config(host="10.0.4.20")
    payload = bar.build_text_payload("BUILD OK")
    bar.draw_text(config, payload)

    assert "X-API-Token" not in bar_transport.calls[0].headers


def test_a_rejected_request_raises(config, bar_transport):
    bar_transport.add("DELETE", "/api/display/draw", status=401, json={"error": "unauthorized"})

    with pytest.raises(exceptions.BarError):
        bar.clear(config)

    assert len(bar_transport.calls) == 1


def test_a_rejected_request_reports_its_status_and_path(config, bar_transport):
    bar_transport.add("DELETE", "/api/display/draw", status=401, json={"error": "unauthorized", "code": 7})

    with pytest.raises(exceptions.BarAPIError) as caught:
        bar.clear(config)

    assert caught.value.status_code == 401
    assert caught.value.code == 7
    assert caught.value.path == "/api/display/draw"
    assert caught.value.method == "DELETE"


def test_a_connection_failure_retries_then_raises(config, bar_transport, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    bar_transport.add("POST", "/api/display/draw", error=httpx2.ConnectError("boom"))

    payload = bar.build_text_payload("BUILD OK")
    with pytest.raises(exceptions.BarRequestError) as caught:
        bar.draw_text(config, payload)

    assert len(bar_transport.calls) == 3
    assert caught.value.attempts == 3


def test_a_non_api_delivery_failure_still_maps_to_a_bar_error(config, monkeypatch):
    """A successful-status response busylib can't parse still surfaces as a one-line BarError, not a raw crash."""
    real_busy_bar = bar.busylib.BusyBar

    def responder(_request):
        return httpx2.Response(200, content=b"not json", headers={"content-type": "text/plain"})

    def fake_busy_bar(addr, *, token=None, **_kwargs):
        return real_busy_bar(addr, token=token, transport=httpx2.MockTransport(responder))

    monkeypatch.setattr(bar.busylib, "BusyBar", fake_busy_bar)

    with pytest.raises(exceptions.BarRequestError):
        bar.clear(config)


def test_every_icon_ships_as_a_12x12_rgba_png():
    for icon in bar.ICON_NAMES:
        data = bar.icon_bytes(icon)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height, depth, colour_type = struct.unpack(">IIBB", data[16:26])
        assert (width, height, depth, colour_type) == (12, 12, 8, 6)


def elements_by_id(payload):
    """Index a payload's elements by their stable element id."""
    return {element["id"]: element for element in payload.model_dump(exclude_none=True)["elements"]}


def test_image_element_accepts_a_legitimate_icon_filename():
    element = bar.ImageElement(id="icon", path="success.png")
    assert element.path == "success.png"


def test_image_element_rejects_a_path_traversal_style_path():
    with pytest.raises(ValidationError):
        bar.ImageElement(id="icon", path="../../etc/passwd")


def test_the_workflow_payload_carries_two_rows_and_an_icon():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="#12", icon="success")
    )

    assert set(elements) == {"repo", "ref", "icon"}
    assert elements["repo"]["text"] == "mb-dot-dev/busyboy"
    assert elements["ref"]["text"] == "#12"
    assert elements["icon"]["type"] == "image"
    assert elements["icon"]["path"] == "success.png"


def test_the_two_rows_share_a_text_column_beside_the_icon():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="main", icon="pending")
    )

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


def test_the_icon_and_text_column_fit_the_front_display():
    assert bar.ICON_X + bar.ICON_SIZE <= bar.TEXT_X
    assert bar.TEXT_X + bar.TEXT_WIDTH == bar.FRONT_DISPLAY_WIDTH


def test_the_two_rows_fit_the_front_display_without_overlapping():
    top_row = bar.ROW_ONE_Y + bar.FONT_GLYPH_OFFSETS[bar.ROW_ONE_FONT]
    bottom_row = bar.ROW_TWO_Y + bar.FONT_GLYPH_OFFSETS[bar.ROW_TWO_FONT]

    assert top_row >= 0
    assert top_row + bar.FONT_GLYPH_HEIGHTS[bar.ROW_ONE_FONT] <= bottom_row
    assert bottom_row + bar.FONT_GLYPH_HEIGHTS[bar.ROW_TWO_FONT] <= bar.FRONT_DISPLAY_HEIGHT


def test_the_front_display_is_seventy_two_by_sixteen():
    assert bar.FRONT_DISPLAY_WIDTH == 72
    assert bar.FRONT_DISPLAY_HEIGHT == 16


def test_workflow_element_ids_are_stable_so_a_redraw_replaces():
    first = elements_by_id(bar.build_workflow_payload(repo_label="a/b", ref_label="#1", icon="success"))
    second = elements_by_id(bar.build_workflow_payload(repo_label="c/d", ref_label="#2", icon="failure"))

    assert set(first) == set(second)


def test_a_non_ascii_repo_label_is_sanitized_rather_than_rejected():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/büsyboy", ref_label="#12", icon="success")
    )

    assert elements["repo"]["text"] == "mb-dot-dev/b?syboy"


def test_a_non_ascii_ref_label_is_sanitized_rather_than_rejected():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="feature/café", icon="success")
    )

    assert elements["ref"]["text"] == "feature/caf?"


def test_a_label_that_is_entirely_non_ascii_is_still_a_valid_non_empty_payload():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="日本語", icon="success")
    )

    assert elements["ref"]["text"] == "???"


def test_a_pure_ascii_label_passes_through_unchanged():
    elements = elements_by_id(
        bar.build_workflow_payload(repo_label="mb-dot-dev/busyboy", ref_label="feature/x", icon="success")
    )

    assert elements["repo"]["text"] == "mb-dot-dev/busyboy"
    assert elements["ref"]["text"] == "feature/x"


def test_uploading_icons_posts_every_asset_scoped_to_the_application(config, bar_transport):
    bar_transport.add("POST", "/api/assets/upload")

    bar.upload_icons(config)

    assert len(bar_transport.calls) == len(bar.ICON_NAMES)
    uploaded = set()
    for request in bar_transport.calls:
        params = dict(request.url.params)
        assert params["application_name"] == "busyboy"
        assert request.content[:8] == b"\x89PNG\r\n\x1a\n"
        uploaded.add(params["file"])
    assert uploaded == {f"{icon}.png" for icon in bar.ICON_NAMES}


def test_a_failed_icon_upload_raises(config, bar_transport):
    bar_transport.add("POST", "/api/assets/upload", status=401, json={"error": "nope"})

    with pytest.raises(exceptions.BarError):
        bar.upload_icons(config)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CI", "CI"),
        ("🚀 Deploy", "Deploy"),
        ("CI 🚀 Build", "CI Build"),
        ("🚀", ""),
        ("", ""),
        ("  Release  ", "Release"),
    ],
)
def test_undisplayable_characters_are_dropped_rather_than_replaced(text, expected):
    assert bar.strip_undisplayable(text) == expected


def limits_elements(session, weekly):
    payload = bar.build_limits_payload(session=session, weekly=weekly)
    return payload, {element.id: element for element in payload.elements}


def test_rectangle_element_defaults_to_no_border():
    rectangle = bar.RectangleElement(id="r", width=5, height=5, fill_colors=["#1F1F1F"])

    assert rectangle.border_width == 0
    assert rectangle.fill == "solid"
    assert rectangle.fill_colors == ["#1F1F1FFF"]


def test_rectangle_element_rejects_zero_width():
    with pytest.raises(ValidationError):
        bar.RectangleElement(id="r", width=0, height=5, fill_colors=["#1F1F1F"])


def test_rectangle_serialises_for_the_wire():
    rectangle = bar.RectangleElement(id="r", x=1, y=2, width=5, height=6, fill_colors=["#CD6E58"])

    body = rectangle.model_dump(exclude_none=True)

    assert body["type"] == "rectangle"
    assert body["fill"] == "solid"
    assert body["border_width"] == 0
    assert body["fill_colors"] == ["#CD6E58FF"]
    assert (body["x"], body["y"], body["width"], body["height"]) == (1, 2, 5, 6)


@pytest.mark.parametrize(
    ("percent", "width"),
    [(3.0, 2), (50, 35), (100, 70), (150, 70)],
)
def test_fill_width_is_proportional_and_clamped(percent, width):
    _, elements = limits_elements(bar.LimitRow(percent, None), bar.LimitRow(percent, None))

    assert elements["session-fill"].width == width
    assert elements["weekly-fill"].width == width


@pytest.mark.parametrize("percent", [0, 0.3, -5])
def test_an_empty_bar_still_draws_its_fill_so_a_previous_fill_is_overwritten(percent):
    """A draw never removes omitted ids, so a fill that reset to ~0 must be redrawn, invisibly, not left out."""
    _, elements = limits_elements(bar.LimitRow(percent, None), bar.LimitRow(percent, None))

    for prefix in ("session", "weekly"):
        fill = elements[f"{prefix}-fill"]
        assert fill.width == 1
        assert fill.fill_colors == [bar.LIMITS_TRACK_COLOR + "FF"]


@pytest.mark.parametrize(("percent", "color"), [(85, "#CD6E58FF"), (85.1, "#B3261EFF")])
def test_fill_turns_red_above_85_percent(percent, color):
    _, elements = limits_elements(bar.LimitRow(percent, None), bar.LimitRow(10, None))

    assert elements["session-fill"].fill_colors == [color]
    assert elements["weekly-fill"].fill_colors == ["#CD6E58FF"]


@pytest.mark.parametrize(
    ("percent", "countdown", "shown"),
    [(75, "3h59m", " "), (75.1, "3h59m", "3h59m"), (99, None, " "), (10, "3h59m", " ")],
)
def test_countdown_only_shows_above_75_percent(percent, countdown, shown):
    _, elements = limits_elements(bar.LimitRow(percent, countdown), bar.LimitRow(0, None))

    assert elements["session-reset"].text == shown
    assert elements["session-reset"].font == "tiny"


def test_a_hidden_countdown_is_still_drawn_so_it_overwrites_the_old_one():
    payload, _ = limits_elements(bar.LimitRow(0, None), bar.LimitRow(0, None))

    ids = [element.id for element in payload.elements]

    assert "session-reset" in ids
    assert "weekly-reset" in ids


def test_elements_have_stable_ids_and_draw_order():
    payload, _ = limits_elements(bar.LimitRow(90, "1h00m"), bar.LimitRow(80, "2d"))

    assert [element.id for element in payload.elements] == [
        "session-track",
        "session-fill",
        "session-reset",
        "weekly-track",
        "weekly-fill",
        "weekly-reset",
    ]


def test_limits_payload_is_scoped_to_the_application():
    payload, _ = limits_elements(bar.LimitRow(1, None), bar.LimitRow(1, None))

    assert payload.application_name == bar.APPLICATION_NAME


def test_countdown_text_color_matches_the_fill():
    _, elements = limits_elements(bar.LimitRow(80, "1h00m"), bar.LimitRow(90, "2d"))

    assert elements["session-reset"].color == "#1A1A1AFF"
    assert elements["weekly-reset"].color == "#FFFFFFFF"


def test_rows_are_stacked_on_the_front_display():
    _, elements = limits_elements(bar.LimitRow(50, None), bar.LimitRow(50, None))

    assert elements["session-track"].y == bar.LIMITS_SESSION_Y
    assert elements["weekly-track"].y == bar.LIMITS_WEEKLY_Y
    assert elements["session-track"].width == bar.LIMITS_TRACK_WIDTH
    assert bar.LIMITS_WEEKLY_Y + bar.LIMITS_TRACK_HEIGHT <= bar.FRONT_DISPLAY_HEIGHT
