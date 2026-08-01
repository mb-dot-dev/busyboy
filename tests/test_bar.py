"""Tests for BUSY Bar payload construction and delivery."""

import json
from urllib.parse import urlparse

from pydantic import ValidationError
import pytest
import requests
import responses

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


def test_scrolling_can_be_switched_off():
    element = element_of(bar.build_text_payload("hi", scroll_rate=0))

    assert element["scroll_rate"] == 0


def test_a_negative_timeout_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", timeout=-1)


def test_an_unrecognised_colour_is_rejected():
    with pytest.raises(ValidationError):
        bar.build_text_payload("hi", color="definitely-not-a-colour")


@responses.activate
def test_draw_text_posts_the_payload(config):
    responses.add(responses.POST, "http://10.0.4.20/api/display/draw", json={"result": "ok"}, status=200)

    payload = bar.build_text_payload("BUILD OK")
    bar.draw_text(config, payload)

    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert request.method == "POST"
    assert urlparse(request.url).path == "/api/display/draw"
    assert request.headers["X-API-Token"] == "testtoken"
    assert request.body is not None
    body = json.loads(request.body)
    assert body["elements"][0]["text"] == "BUILD OK"


@responses.activate
def test_clear_deletes_the_drawing(config):
    responses.add(responses.DELETE, "http://10.0.4.20/api/display/draw", json={"result": "ok"}, status=200)

    bar.clear(config)

    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert request.method == "DELETE"
    assert urlparse(request.url).query == "application_name=busyboy"


@responses.activate
def test_draw_text_omits_the_token_header_when_none_is_configured():
    responses.add(responses.POST, "http://10.0.4.20/api/display/draw", json={"result": "ok"}, status=200)

    config = load_config(host="10.0.4.20")
    payload = bar.build_text_payload("BUILD OK")
    bar.draw_text(config, payload)

    assert "X-API-Token" not in responses.calls[0].request.headers


@responses.activate
def test_a_rejected_request_raises(config):
    responses.add(responses.DELETE, "http://10.0.4.20/api/display/draw", json={"error": "unauthorized"}, status=401)

    with pytest.raises(exceptions.BarError):
        bar.clear(config)


@responses.activate
def test_a_connection_failure_retries_then_raises(config, monkeypatch):
    monkeypatch.setattr(bar.time, "sleep", lambda seconds: None)
    responses.add(
        responses.POST,
        "http://10.0.4.20/api/display/draw",
        body=requests.exceptions.ConnectionError("boom"),
    )

    payload = bar.build_text_payload("BUILD OK")
    with pytest.raises(exceptions.BarRequestError):
        bar.draw_text(config, payload)

    assert len(responses.calls) == bar.MAX_RETRIES + 1
