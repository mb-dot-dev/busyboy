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
