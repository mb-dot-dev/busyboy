"""Tests for BUSY Bar payload construction and delivery."""

import json

from busylib import exceptions
import httpx
from pydantic import ValidationError
import pytest

from busyboy import bar
from busyboy.config import load_config


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
    assert element["align"] == "center"
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


def test_scrolling_can_be_switched_off():
    element = element_of(bar.build_text_payload("hi", scroll_rate=0))

    assert element["scroll_rate"] == 0


def test_a_negative_timeout_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", timeout=-1)


def test_an_unrecognised_colour_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", color="definitely-not-a-colour")


def test_draw_text_posts_the_payload(config):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    payload = bar.build_text_payload("BUILD OK")
    client = bar.open_client(config, transport=httpx.MockTransport(handler))
    with client:
        bar.draw_text(client, payload)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/display/draw"
    assert requests[0].headers["X-API-Token"] == "testtoken"
    body = json.loads(requests[0].content)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_clear_deletes_the_drawing(config):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "ok"})

    client = bar.open_client(config, transport=httpx.MockTransport(handler))
    with client:
        bar.clear(client)

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/api/display/draw"


def test_a_rejected_request_raises(config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = bar.open_client(config, transport=httpx.MockTransport(handler))
    with client, pytest.raises(exceptions.BusyBarError):
        bar.clear(client)
