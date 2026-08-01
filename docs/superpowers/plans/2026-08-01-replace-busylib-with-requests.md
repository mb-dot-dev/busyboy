# Replace `busylib` with `requests` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace busyboy's `busylib` dependency with a direct `requests`-based integration against the BUSY
Bar's HTTP API, keeping the CLI's behavior (flags, defaults, exit codes) byte-for-byte identical.

**Architecture:** `bar.py` keeps owning both payload construction and delivery (as it already did with
busylib), now built on hand-written pydantic models plus a `requests.Session`-free retry loop. A new
`exceptions.py` replaces `busylib.exceptions`. `cli.py` changes only its imports and two call sites that no
longer need a client object. Tests move from `httpx.MockTransport` to `responses`.

**Tech Stack:** `requests` (HTTP), `pydantic` + `pydantic-extra-types` (payload validation and color
parsing), `responses` (test mocking), `types-requests` (type stubs for `ty check`).

**Spec:** `docs/superpowers/specs/2026-08-01-replace-busylib-with-requests-design.md` — read it before
starting; this plan implements it task-by-task.

## Global Constraints

- Python >=3.14; all dependency changes go through `uv` (`uv add`, `uv remove`, `uv lock`), never hand-edited
  version pins.
- CLI contract stays identical: `busyboy text`/`busyboy clear` keep the same flags, defaults, exit codes
  (0 success / 1 expected failure / 2 usage error), and silent-success behavior.
- Retries: only `requests.exceptions.RequestException` (connection errors, timeouts) is retried, up to 2
  extra attempts, sleeping `0.25 * (attempt + 1)` seconds between attempts. HTTP error responses (any status
  >= 400) raise immediately — never retried.
- `--color` keeps accepting CSS names and hex via `pydantic_extra_types.color.Color`.
- No response-body model: only the HTTP status code is checked. Response JSON is read solely to extract an
  `error`/`code` for the exception message on failure.
- No `busylib` import may remain anywhere in `src/` or `tests/` when this plan is complete.
- Coverage stays at or above `fail_under = 75` (`[tool.coverage.report]` in `pyproject.toml`).
- Ruff formatting: 120-char lines, double quotes, PEP 257 docstrings, isort with
  `force-sort-within-sections = true`.
- Work happens on the already-checked-out branch `feat/replace-busylib-with-requests`.

---

### Task 1: Exception hierarchy (`busyboy/exceptions.py`)

**Files:**
- Create: `src/busyboy/exceptions.py`
- Test: `tests/test_exceptions.py`

**Interfaces:**
- Consumes: nothing (no dependency on any other busyboy module).
- Produces (used by Tasks 2 and 3):
  - `class BarError(Exception)` — base class.
  - `class BarAPIError(BarError)` — `__init__(self, error: str, *, code: int | None, status_code: int, method: str, path: str)`.
  - `class BarRequestError(BarError)` — `__init__(self, message: str, *, method: str, path: str, attempts: int)`.
  - `def format_delivery_error(error: BarError) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_exceptions.py`:

```python
"""Tests for the BUSY Bar delivery exception hierarchy."""

from busyboy import exceptions


def test_bar_api_error_is_a_bar_error():
    error = exceptions.BarAPIError("bad request", code=400, status_code=400, method="POST", path="/api/display/draw")

    assert isinstance(error, exceptions.BarError)


def test_bar_api_error_message_includes_status_method_and_path():
    error = exceptions.BarAPIError("bad request", code=400, status_code=400, method="POST", path="/api/display/draw")

    message = exceptions.format_delivery_error(error)

    assert "HTTP 400" in message
    assert "POST /api/display/draw" in message
    assert "bad request" in message
    assert "code=400" in message


def test_bar_api_error_without_a_code_omits_it():
    error = exceptions.BarAPIError("bad request", code=None, status_code=400, method="POST", path="/api/display/draw")

    message = exceptions.format_delivery_error(error)

    assert "code=" not in message


def test_bar_request_error_is_a_bar_error():
    error = exceptions.BarRequestError("connection refused", method="POST", path="/api/display/draw", attempts=3)

    assert isinstance(error, exceptions.BarError)


def test_bar_request_error_message_includes_method_path_and_attempts():
    error = exceptions.BarRequestError("connection refused", method="POST", path="/api/display/draw", attempts=3)

    message = exceptions.format_delivery_error(error)

    assert "POST /api/display/draw" in message
    assert "attempts=3" in message
    assert "connection refused" in message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_exceptions.py -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'busyboy.exceptions'` (or `ImportError`).

- [ ] **Step 3: Write the implementation**

Create `src/busyboy/exceptions.py`:

```python
"""Exception hierarchy for BUSY Bar delivery failures."""


class BarError(Exception):
    """Base class for all busyboy BUSY Bar delivery exceptions."""


class BarAPIError(BarError):
    """Raised when the BUSY Bar API returns an HTTP error response."""

    def __init__(
        self,
        error: str,
        *,
        code: int | None,
        status_code: int,
        method: str,
        path: str,
    ) -> None:
        self.error = error
        self.code = code
        self.status_code = status_code
        self.method = method
        self.path = path
        details = [f"HTTP {status_code}", f"{method} {path}", error]
        if code is not None:
            details.append(f"code={code}")
        super().__init__(" | ".join(details))


class BarRequestError(BarError):
    """Raised when a request fails at the transport level after retries are exhausted."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        path: str,
        attempts: int,
    ) -> None:
        self.message = message
        self.method = method
        self.path = path
        self.attempts = attempts
        super().__init__(f"{message} | {method} {path} | attempts={attempts}")


def format_delivery_error(error: BarError) -> str:
    """Render a BarError as a compact one-line diagnostic for stderr."""
    if isinstance(error, BarAPIError):
        details = [f"HTTP {error.status_code}", f"{error.method} {error.path}", error.error]
        if error.code is not None:
            details.append(f"code={error.code}")
        return " | ".join(details)
    if isinstance(error, BarRequestError):
        return f"request error | {error.method} {error.path} | attempts={error.attempts} | {error.message}"
    return str(error)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_exceptions.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint**

Run: `uv run --frozen ruff check src/busyboy/exceptions.py tests/test_exceptions.py && uv run --frozen ruff format --check src/busyboy/exceptions.py tests/test_exceptions.py`
Expected: no issues. If ruff reformats anything, run `make format` and re-check.

- [ ] **Step 6: Commit**

```bash
git add src/busyboy/exceptions.py tests/test_exceptions.py
git commit -m "feat: add BarError exception hierarchy, replacing busylib.exceptions"
```

---

### Task 2: Payload models and transport (`busyboy/bar.py`)

This task swaps busyboy's dependency (`busylib` → `requests` + `pydantic-extra-types`) and rewrites `bar.py`
and its tests together, since neither half works without the other. `config.py` and its tests are untouched.

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv` commands)
- Modify: `src/busyboy/bar.py` (full rewrite)
- Modify: `tests/test_bar.py` (full rewrite)

**Interfaces:**
- Consumes: `exceptions.BarAPIError`, `exceptions.BarRequestError` (Task 1); `BusyboyConfig.host`,
  `BusyboyConfig.token_value` (existing `config.py`, unchanged).
- Produces (used by Task 3):
  - `bar.DisplayFontName` (`Literal[...]` type), `bar.FONT_NAMES: tuple[str, ...]`, `bar.DEFAULT_FONT`,
    `bar.DEFAULT_SCROLL_RATE`, `bar.DEFAULT_TEXT_Y`, `bar.APPLICATION_NAME`.
  - `bar.build_text_payload(text, *, font=DEFAULT_FONT, color=None, timeout=None, scroll_rate=DEFAULT_SCROLL_RATE) -> DisplayElements`
    (unchanged signature).
  - `bar.draw_text(config: BusyboyConfig, payload: DisplayElements) -> None` (signature changes: takes
    `config` directly, no more client object).
  - `bar.clear(config: BusyboyConfig) -> None` (signature changes: takes `config` directly).
  - `bar.MAX_RETRIES`, `bar.time` (module-level `time` import, exposed so tests can monkeypatch
    `bar.time.sleep`).
  - `bar.open_client` **no longer exists** — Task 3 must not reference it.

- [ ] **Step 1: Add the new dependencies (do not remove busylib yet)**

```bash
uv add requests pydantic-extra-types
uv add --group dev responses types-requests
```

Expected: `pyproject.toml`'s `dependencies` list now includes `requests` and `pydantic-extra-types`; the
`dev` group gains `responses` and `types-requests`. `uv.lock` is updated automatically.

**Do not run `uv remove busylib` in this task.** `src/busyboy/__init__.py` unconditionally does `from
busyboy.cli import main`, and `cli.py` still imports `busylib` until Task 3 rewrites it — since `import
busyboy` always runs `__init__.py`, uninstalling `busylib` now would break collection for every test module,
not just `tests/test_bar.py` (there is no way to import `busyboy.bar` without Python first running
`busyboy/__init__.py`). `busylib` stays listed as an unused dependency through the end of this task; Task 3
removes it once `cli.py` no longer references it.

- [ ] **Step 2: Replace the test file (red first)**

Replace the full contents of `tests/test_bar.py`:

```python
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

    assert len(responses.calls) == 1


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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_bar.py -v`
Expected: FAIL — `busylib` is still installed at this point (Step 1 deliberately didn't remove it), so
collection succeeds, but the old `bar.py` doesn't match the new test file: it has no `MAX_RETRIES` or `time`
attribute, and `open_client`/`draw_text(client, payload)`/`clear(client)` don't match the new tests' calls
(`bar.draw_text(config, payload)`, `bar.clear(config)`). Expect `AttributeError` or `TypeError` failures, not
an import error.

- [ ] **Step 4: Replace the implementation**

Replace the full contents of `src/busyboy/bar.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_bar.py -v`
Expected: PASS (all tests, including the new retry test).

- [ ] **Step 6: Lint**

Run: `uv run --frozen ruff check src/busyboy/bar.py tests/test_bar.py && uv run --frozen ruff format --check src/busyboy/bar.py tests/test_bar.py && uv run --frozen ty check`
Expected: no issues. Fix anything reported (e.g. import ordering, missing type stubs) before proceeding —
this is where a missing `responses` or `requests` stub would surface; `types-requests` was already added in
Step 1, so only add further stub packages if `ty` actually reports a gap.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/busyboy/bar.py tests/test_bar.py
git commit -m "feat: replace busylib with requests in bar.py"
```

---

### Task 3: CLI wiring (`busyboy/cli.py`)

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv remove busylib`)
- Modify: `src/busyboy/cli.py`
- Modify: `tests/test_cli.py` (full rewrite)

**Interfaces:**
- Consumes: `bar.DisplayFontName`, `bar.FONT_NAMES`, `bar.DEFAULT_FONT`, `bar.DEFAULT_SCROLL_RATE`,
  `bar.build_text_payload`, `bar.draw_text(config, payload)`, `bar.clear(config)` (Task 2);
  `exceptions.BarError`, `exceptions.format_delivery_error` (Task 1).
- Produces: no new public interface — `cli.main` is the Click group, unchanged from the outside.

- [ ] **Step 1: Remove the busylib dependency**

Task 2 deliberately left `busylib` installed (see its Step 1) because `src/busyboy/__init__.py`
unconditionally imports `cli.py`, which until now still needed it. Once this task's Step 4 rewrites
`cli.py`'s imports, nothing in the project references `busylib` anymore, so remove it first:

```bash
uv remove busylib
```

Expected: `pyproject.toml`'s `dependencies` list no longer has `busylib`. `uv.lock` updates automatically.
This intentionally makes every test collection fail until Step 4 rewrites `cli.py` — that's the "red" state
Step 3 below expects.

- [ ] **Step 2: Replace the test file (red first)**

Replace the full contents of `tests/test_cli.py`:

```python
"""Tests for exit codes and output of the busyboy command line."""

import json
import re

from click.testing import CliRunner
import pytest
import responses

from busyboy import cli

ENV = {"BUSYBOY_HOST": "10.0.4.20", "BUSYBOY_TOKEN": "testtoken"}

DRAW_URL_PATTERN = re.compile(r"^http://[^/]+/api/display/draw")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


class Recorder:
    """An in-memory stand-in for the bar, capturing what the CLI sends."""

    def __init__(self) -> None:
        self.requests: list = []
        self.status = 200
        self.body: dict[str, str] = {"result": "ok"}

    def callback(self, request):
        self.requests.append(request)
        return (self.status, {}, json.dumps(self.body))


@pytest.fixture
def recorder():
    """Route the CLI's requests through a responses-registered callback."""
    recorder = Recorder()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add_callback(responses.POST, DRAW_URL_PATTERN, callback=recorder.callback, content_type="application/json")
        mock.add_callback(
            responses.DELETE, DRAW_URL_PATTERN, callback=recorder.callback, content_type="application/json"
        )
        yield recorder


def test_a_successful_draw_says_nothing(recorder):
    result = CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert result.exit_code == 0
    assert result.output == ""
    assert result.stderr == ""


def test_text_sends_the_string_to_the_bar(recorder):
    CliRunner().invoke(cli.main, ["text", "BUILD OK"], env=ENV)

    assert len(recorder.requests) == 1
    assert recorder.requests[0].method == "POST"
    body = json.loads(recorder.requests[0].body)
    assert body["elements"][0]["text"] == "BUILD OK"


def test_options_reach_the_payload(recorder):
    CliRunner().invoke(
        cli.main,
        ["text", "hi", "--color", "red", "--timeout", "30", "--font", "small", "--scroll-rate", "600"],
        env=ENV,
    )

    element = json.loads(recorder.requests[0].body)["elements"][0]
    assert element["color"] == "#FF0000FF"
    assert element["timeout"] == 30
    assert element["font"] == "small"
    assert element["scroll_rate"] == 600


def test_token_flag_overrides_the_environment(recorder):
    CliRunner().invoke(cli.main, ["text", "hi", "--token", "flagtoken"], env=ENV)

    assert recorder.requests[0].headers["X-API-Token"] == "flagtoken"


def test_clear_issues_a_delete(recorder):
    result = CliRunner().invoke(cli.main, ["clear"], env=ENV)

    assert result.exit_code == 0
    assert recorder.requests[0].method == "DELETE"


def test_host_flag_overrides_the_environment(recorder):
    CliRunner().invoke(cli.main, ["text", "hi", "--host", "192.168.1.5"], env=ENV)

    assert recorder.requests[0].url.startswith("http://192.168.1.5")


def test_no_configuration_uses_the_usb_default_and_sends_no_token(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi"], env={})

    assert result.exit_code == 0
    assert recorder.requests[0].url.startswith("http://10.0.4.20")
    assert "X-API-Token" not in recorder.requests[0].headers


def test_a_rejected_request_exits_one(recorder):
    recorder.status = 401
    recorder.body = {"error": "unauthorized"}

    result = CliRunner().invoke(cli.main, ["text", "hi"], env=ENV)

    assert result.exit_code == 1
    assert "401" in result.stderr


def test_a_missing_argument_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text"], env=ENV)

    assert result.exit_code == 2


def test_an_unknown_font_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["text", "hi", "--font", "cursive"], env=ENV)

    assert result.exit_code == 2


def test_an_invalid_colour_exits_one(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi", "--color", "chartreuseish"], env=ENV)

    assert result.exit_code == 1
    assert "color" in result.stderr


def test_help_lists_both_subcommands():
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0
    assert "text" in result.output
    assert "clear" in result.output


def test_verbose_on_a_successful_draw_still_exits_zero(recorder):
    result = CliRunner().invoke(cli.main, ["text", "hi", "--verbose"], env=ENV)

    assert result.exit_code == 0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --frozen pytest tests/test_cli.py -v`
Expected: FAIL/ERROR — `cli.py` still does `from busylib import exceptions, types`, and Step 1 just removed
`busylib` from the environment, so collection fails with `ModuleNotFoundError: No module named 'busylib'`.

- [ ] **Step 4: Update the implementation**

In `src/busyboy/cli.py`, change the imports from:

```python
from busylib import exceptions, types
import click
from pydantic import ValidationError

from busyboy import bar
from busyboy.config import ConfigError, load_config
```

to:

```python
import click
from pydantic import ValidationError

from busyboy import bar, exceptions
from busyboy.config import ConfigError, load_config
```

Replace `_configure_logging`:

```python
def _configure_logging(*, verbose: bool) -> None:
    """
    Set up logging for one invocation.

    --verbose raises the level to DEBUG, which is what turns on urllib3's own
    per-connection request logging (the standard way to see requests traffic).
    Non-verbose runs need no configuration: unlike busylib, requests/urllib3
    doesn't log HTTP error responses at error level, so there's no duplicate
    message to silence.
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
```

In `_handle_errors`, change the exception clause from `except exceptions.BusyBarError as error:` to
`except exceptions.BarError as error:` (the body — `if verbose: raise` / `message =
exceptions.format_delivery_error(error)` / `raise click.ClickException(message) from error` — is unchanged).

In the `text` command, change the cast from `cast(types.DisplayFontName, font)` to `cast(bar.DisplayFontName,
font)`, and replace the client/draw call:

```python
    payload = bar.build_text_payload(
        text,
        font=cast(bar.DisplayFontName, font),
        color=color,
        timeout=timeout,
        scroll_rate=scroll_rate,
    )
    bar.draw_text(config, payload)
```

In the `clear` command, replace the client/clear call:

```python
    config = load_config(host=host, token=token)
    bar.clear(config)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest tests/test_cli.py -v`
Expected: PASS (all tests).

- [ ] **Step 6: Full test suite and lint**

Run: `uv run --frozen pytest -v && uv run --frozen ruff check && uv run --frozen ruff format --check && uv run --frozen ty check`
Expected: all pass. Fix anything reported.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/busyboy/cli.py tests/test_cli.py
git commit -m "feat: wire cli.py to the requests-based bar.py and exceptions.py"
```

---

### Task 4: Update CLAUDE.md

Six places in `CLAUDE.md` reference `busylib` directly or describe behavior that changed. Each is listed
below with its exact replacement text.

**Files:**
- Modify: `CLAUDE.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Project section**

Replace:

```
busyboy is a console application (CLI) that displays information on a BUSY Bar. It depends on `busylib`
(`https://github.com/busy-app/busylib-py`), which wraps the bar's HTTP API.
```

with:

```
busyboy is a console application (CLI) that displays information on a BUSY Bar. It talks to the bar's HTTP
API directly over `requests`, following the bar's own OpenAPI spec (vendored at
`docs/superpowers/references/openapi.yaml`) for field names, patterns, and enums.
```

- [ ] **Step 2: Architecture section**

Replace the intro sentence and the module bullet list:

```
Three modules with one job each. Keep the boundaries: `config.py` imports neither Click nor busylib, `bar.py`
knows nothing about argv or the environment, and `cli.py` holds no BUSY Bar payload knowledge.

- `src/busyboy/config.py` — `BusyboyConfig` (pydantic-settings, `env_prefix="BUSYBOY_"`, frozen), defaulting
  `host` to `10.0.4.20` and `token` to `None`, plus `load_config(*, host, token)` and `ConfigError`. Explicit
  arguments beat environment variables because pydantic-settings ranks init arguments above env sources.
  `BusyboyConfig.token_value` encapsulates unwrapping the `SecretStr` (or returning `None`) so callers never
  touch `get_secret_value()` directly.
- `src/busyboy/bar.py` — `open_client`, `build_text_payload` (pure, no I/O), `draw_text`, `clear`, and the
  payload constants. This is where BUSY Bar knowledge lives.
- `src/busyboy/cli.py` — the `main` Click group, the `text` and `clear` subcommands, and the
  error-to-exit-code mapping in `_handle_errors`.
- `src/busyboy/__init__.py` — re-exports `main` so `[project.scripts] busyboy = "busyboy:main"` keeps working.
  Do not put logic here.
```

with:

```
Four modules with one job each. Keep the boundaries: `config.py` imports neither Click nor `bar.py`, `bar.py`
knows nothing about argv or the environment, and `cli.py` holds no BUSY Bar payload knowledge.

- `src/busyboy/config.py` — `BusyboyConfig` (pydantic-settings, `env_prefix="BUSYBOY_"`, frozen), defaulting
  `host` to `10.0.4.20` and `token` to `None`, plus `load_config(*, host, token)` and `ConfigError`. Explicit
  arguments beat environment variables because pydantic-settings ranks init arguments above env sources.
  `BusyboyConfig.token_value` encapsulates unwrapping the `SecretStr` (or returning `None`) so callers never
  touch `get_secret_value()` directly.
- `src/busyboy/bar.py` — `build_text_payload` (pure, no I/O), `draw_text`, `clear`, the payload constants, and
  the `requests`-based transport itself: a `requests.request(...)` call per delivery, retried up to
  `MAX_RETRIES` times with backoff on transport-level failures, raising `busyboy.exceptions.BarAPIError` or
  `BarRequestError` on failure. This is where BUSY Bar knowledge lives — payload shape and delivery both.
- `src/busyboy/exceptions.py` — `BarError` (base), `BarAPIError`, `BarRequestError`, and
  `format_delivery_error(error) -> str`, the one-line renderer `cli.py` prints to stderr.
- `src/busyboy/cli.py` — the `main` Click group, the `text` and `clear` subcommands, and the
  error-to-exit-code mapping in `_handle_errors`.
- `src/busyboy/__init__.py` — re-exports `main` so `[project.scripts] busyboy = "busyboy:main"` keeps working.
  Do not put logic here.
```

- [ ] **Step 3: CLI contract section**

Replace:

```
Success is silent with exit 0. Expected failures print one line to stderr and exit 1; Click usage errors exit 2.
`--verbose` enables busylib request logging and lets the original exception propagate for a traceback.

Configuration is `BUSYBOY_HOST` and `BUSYBOY_TOKEN`, both optional and overridable per invocation with
`--host` / `--token`. `host` defaults to `10.0.4.20`, busylib's own USB-subnet address, so a USB-connected bar
needs no configuration at all. `token` defaults to unset. busyboy always passes `host` through to `open_client`,
so busylib is always in "network" mode; a configured token is sent as `X-API-Token`, and an unset one is simply
omitted from the request. Over WiFi (a non-default host) the token remains optional — supply one if the bar
requires it.
```

with:

```
Success is silent with exit 0. Expected failures print one line to stderr and exit 1; Click usage errors exit 2.
`--verbose` sets the root logger to `DEBUG` (surfacing `urllib3`'s own per-connection request logging) and lets
the original exception propagate for a traceback.

Configuration is `BUSYBOY_HOST` and `BUSYBOY_TOKEN`, both optional and overridable per invocation with
`--host` / `--token`. `host` defaults to `10.0.4.20`, the bar's own USB-subnet address, so a USB-connected bar
needs no configuration at all. `token` defaults to unset. A configured token is always sent as `X-API-Token`;
an unset one is simply omitted from the request — there is no separate client "mode" to be in. Over WiFi (a
non-default host) the token remains optional — supply one if the bar requires it.
```

- [ ] **Step 4: Gotchas section**

Replace the entire five-item Gotchas section:

```
**busylib's context manager loses the subtype.** `SyncClientBase.__enter__` is annotated to return
`SyncClientBase`, not `Self`, and `display_draw`/`display_clear` live on `BusyBar`. So
`with open_client(...) as client:` types `client` as `SyncClientBase` and fails `ty check`. Bind first, then use
the binding as the context manager:

```python
client = bar.open_client(config)
with client:
    bar.draw_text(client, payload)
```

Do not reach for `cast`, `Any`, or `# noqa` here — they only hide it.

**Unpacking into `BaseSettings` needs `dict[str, Any]`.** `BaseSettings.__init__` also accepts private keyword
arguments (`_case_sensitive`, `_env_prefix`, ...), so unpacking a `dict[str, str]` makes `ty` report
`Expected 'bool | None', found 'str'`.

**Ruff's `force-sort-within-sections` sorts `import x` and `from x import y` together** by module name. So
`from busylib import ...` comes before `import click`, and `from collections.abc import Callable` before
`import functools`. Write imports that way or `make format` will rewrite them.

**busylib logs API failures itself**, at error level. With no logging configured those reach stderr through
Python's last-resort handler and duplicate the CLI's own message. `cli.py` sets the `busylib` logger to
`CRITICAL` unless `--verbose`.

**Never let pydantic's `ValidationError` into a traceback that a user sees.** Its `missing` errors carry the
entire pre-coercion input dict as `input_value` — including the API token. `SecretStr` does not help, because
the leak is of the raw input rather than the coerced field. `load_config` raises `ConfigError(...) from None`
specifically to break that chain; keep it that way even though `host` and `token` both have defaults now and no
`missing` error can currently be produced — a future required field would reintroduce the same risk.

**`click.Choice` returns `str`**, so passing it to a busylib `Literal` field needs a `cast`. That cast is
legitimate — the parser has already restricted the value.
```

with:

```
**Unpacking into `BaseSettings` needs `dict[str, Any]`.** `BaseSettings.__init__` also accepts private keyword
arguments (`_case_sensitive`, `_env_prefix`, ...), so unpacking a `dict[str, str]` makes `ty` report
`Expected 'bool | None', found 'str'`.

**Ruff's `force-sort-within-sections` sorts `import x` and `from x import y` together** by module name. So
`from pydantic import ...` comes before `from pydantic_extra_types.color import Color`, which comes before
`import requests`; and `from collections.abc import Callable` before `import functools`. Write imports that
way or `make format` will rewrite them.

**Never let pydantic's `ValidationError` into a traceback that a user sees.** Its `missing` errors carry the
entire pre-coercion input dict as `input_value` — including the API token. `SecretStr` does not help, because
the leak is of the raw input rather than the coerced field. `load_config` raises `ConfigError(...) from None`
specifically to break that chain; keep it that way even though `host` and `token` both have defaults now and no
`missing` error can currently be produced — a future required field would reintroduce the same risk.

**`click.Choice` returns `str`**, so passing it to `bar.DisplayFontName` (a `Literal`) needs a `cast`. That
cast is legitimate — the parser has already restricted the value.
```

(If anything unexpected turns up while implementing Tasks 2–3 — a surprising `ty`/ruff error, an actual need
to silence a logger after all — add it here as a new gotcha with the same "cost real time to discover"
framing. Don't invent one speculatively; only add what was actually hit.)

- [ ] **Step 5: Testing section**

Replace:

```
`busylib`'s client accepts `transport=`, so tests drive a real `BusyBar` against an `httpx.MockTransport` rather
than mocking busylib internals. `bar.open_client` exposes that parameter for exactly this reason.

- Mock responses must match the real shape: `SuccessResponse` requires a `result` field, so return
  `{"result": "ok"}`, not `{"success": True}`.
- Use **401** for failure-path tests, not 500. busylib retries `408, 429, 500, 502, 503, 504` up to
  `max_retries=2` with a 0.25 s backoff, so a 500 just makes the test sleep.
- `tests/test_cli.py` monkeypatches `cli.bar.open_client` to inject the mock transport and record requests.
- Click 8.2+ removed `CliRunner(mix_stderr=...)`. `result.output` is stdout only; `result.stderr` is separate.
- Tests that touch config must clear `BUSYBOY_HOST` / `BUSYBOY_TOKEN` (there is an autouse fixture for this) or
  they will pick up a real environment.
```

with:

```
Tests drive the real `bar.py` functions against `responses`-registered endpoints rather than mocking anything
internal to `bar.py` or `requests` itself.

- Response bodies don't need to match any particular shape — busyboy only checks the HTTP status code, and
  discards the response body entirely on success.
- Use **401** for failure-path tests, not 500 or a registered connection error, unless the test specifically
  targets retry behavior — `bar.py` retries transport-level failures (connection errors, timeouts) up to
  `MAX_RETRIES` times with a `RETRY_BACKOFF_SECONDS * (attempt + 1)` sleep between attempts, so provoking that
  path sleeps the test unless `time.sleep` is monkeypatched (see `test_a_connection_failure_retries_then_raises`
  in `tests/test_bar.py`). HTTP error responses are never retried, regardless of status code.
- `tests/test_cli.py` registers a `responses` callback matching any host (`re.compile(r"^http://[^/]+/api/display/draw")`)
  so the `--host` override test doesn't need special-casing.
- Click 8.2+ removed `CliRunner(mix_stderr=...)`. `result.output` is stdout only; `result.stderr` is separate.
- Tests that touch config must clear `BUSYBOY_HOST` / `BUSYBOY_TOKEN` (there is an autouse fixture for this) or
  they will pick up a real environment.
```

- [ ] **Step 6: Hardware facts section**

Replace the framing sentence:

```
Measured against a real bar by capturing frames from `/api/screen` and analysing pixel shift. busylib's schema
documents none of this.
```

with:

```
Measured against a real bar by capturing frames from `/api/screen` and analysing pixel shift. The OpenAPI spec
(`docs/superpowers/references/openapi.yaml`) documents none of this.
```

- [ ] **Step 7: "Testing against a real bar" section**

Replace:

```
`source ~/.zshrc && busybarenv` exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN` for a real device. Frames can be read
back with `BusyBar.screen(0)`, which returns decoded RGB888 bytes — useful for asserting what actually rendered.
Never print or commit the token value.
```

with:

```
`source ~/.zshrc && busybarenv` exports `BUSYBOY_HOST` and `BUSYBOY_TOKEN` for a real device. Never print or
commit the token value.

Frame capture for visual verification (`GET /api/screen`, base64-encoded RGB888/L4 framebuffer bytes) previously
used busylib's `BusyBar.screen(0)` helper during hardware calibration. That helper is no longer available in
this project's environment now that busylib isn't a dependency — busyboy's own CLI never called it, so it
wasn't reimplemented. If frame capture is needed again, either `pip install busylib` in a scratch virtualenv
outside this project's lockfile, or decode `/api/screen` directly: the response body's `Content-Type:
image/bmp` header is misleading (there's no real BMP header), it's base64-encoded raw framebuffer bytes,
RGB888 for the front display (`display=0`) and L4-packed (2 pixels/byte) for the back display (`display=1`).
```

- [ ] **Step 8: Verify no stray references remain**

Run: `grep -n "busylib" CLAUDE.md`
Expected: exactly one match, inside the new "Testing against a real bar" paragraph from Step 7 (the
historical/workaround mention). Every other occurrence should be gone.

- [ ] **Step 9: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for the requests-based architecture"
```

---

### Task 5: Full verification

**Files:** none (verification only).

**Interfaces:** none.

- [ ] **Step 1: Full test suite**

Run: `uv run --frozen pytest -v`
Expected: all tests pass, across `test_config.py`, `test_exceptions.py`, `test_bar.py`, `test_cli.py`.

- [ ] **Step 2: Lint**

Run: `uv run --frozen ruff check && uv run --frozen ruff format --check && uv run --frozen ty check`
Expected: no issues.

- [ ] **Step 3: Coverage**

Run: `uv run --frozen pytest --cov --cov-report=xml`
Expected: coverage report shows `Total` at or above 75% (the `fail_under` gate in `pyproject.toml`).

- [ ] **Step 4: Confirm no busylib references remain in source or dependencies**

Run: `grep -rn "busylib" src/ tests/ pyproject.toml`
Expected: no output (zero matches).

- [ ] **Step 5: Confirm README.md needs no change**

Run: `git diff README.md`
Expected: no output — the design's premise (CLI behavior is identical) holds. If this shows a diff, something
unintended changed user-facing behavior; investigate before proceeding.

- [ ] **Step 6: If anything needed fixing in Steps 1–5, commit the fix**

```bash
git add -A
git commit -m "fix: address issues found during full verification"
```

If nothing needed fixing, skip this step — there's nothing to commit.
