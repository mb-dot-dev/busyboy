"""Shared pytest fixtures: an in-memory stand-in for the bar's HTTP API.

bar.py talks to the bar through busylib, which uses httpx2 rather than
`requests`, so `responses` (which only patches `requests`) can no longer
intercept those calls. `BarTransport` mirrors `responses.add`/`responses.calls`
closely enough that call sites mostly needed a straight swap; GitHub calls
still go through `requests` and are still mocked with `responses` directly.
"""

from __future__ import annotations

import httpx2
import pytest

from busyboy import bar


class BarTransport:
    """A `responses`-style router for the bar's HTTP API, backed by an httpx2 mock transport."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], list[tuple[int, dict[str, object], Exception | None]]] = {}
        self.calls: list[httpx2.Request] = []

    def add(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        """
        Register a response for one method+path.

        Registering more than once for the same method+path queues them in
        order; once only one is left it repeats for every further call,
        matching `responses`' own behaviour.
        """
        queue = self._routes.setdefault((method, path), [])
        queue.append((status, json if json is not None else {"result": "ok"}, error))

    def responder(self, request: httpx2.Request) -> httpx2.Response:
        """Serve the next queued response for this request's method+path, recording the call."""
        self.calls.append(request)
        queue = self._routes.get((request.method, request.url.path))
        if not queue:
            raise AssertionError(f"no route registered for {request.method} {request.url.path}")
        status, json_body, error = queue.pop(0) if len(queue) > 1 else queue[0]
        if error is not None:
            raise error
        return httpx2.Response(status, json=json_body)


@pytest.fixture
def bar_transport(monkeypatch: pytest.MonkeyPatch) -> BarTransport:
    """Route every `busylib.BusyBar` the code under test constructs through one recording mock transport."""
    transport = BarTransport()
    real_busy_bar = bar.busylib.BusyBar

    def fake_busy_bar(addr: str, *, token: str | None = None, **_kwargs: object) -> bar.busylib.BusyBar:
        return real_busy_bar(addr, token=token, transport=httpx2.MockTransport(transport.responder))

    monkeypatch.setattr(bar.busylib, "BusyBar", fake_busy_bar)
    return transport
