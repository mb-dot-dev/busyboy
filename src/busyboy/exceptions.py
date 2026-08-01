"""Exception hierarchy for expected busyboy failures."""


class BusyboyError(Exception):
    """Base class for every failure busyboy expects and reports as one line."""


class BarError(BusyboyError):
    """Base class for all busyboy BUSY Bar delivery exceptions."""


class GitError(BusyboyError):
    """Raised when inspecting the local git repository fails."""


class GitHubError(BusyboyError):
    """
    Base class for every GitHub API request failure busyboy raises.

    Not all of these are fatal: GitHubTransientError below IS retryable, so
    catching GitHubError alone to mean "unrecoverable" will also silently
    swallow the retryable subclass. Catch GitHubTransientError specifically
    when that distinction matters.
    """


class GitHubAuthError(GitHubError):
    """Raised when GitHub rejects the token: always on 401, or on 403 when there's no rate-limit evidence."""


class GitHubTransientError(GitHubError):
    """
    Raised for failures worth retrying: 5xx responses, timeouts, dropped connections, and rate limiting.

    `retry_after`, when GitHub's own Retry-After header specified a delay in
    seconds, carries that value so a caller like watch.tick can wait at least
    that long before the next poll instead of retrying on its usual interval.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


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


def format_delivery_error(error: BusyboyError) -> str:
    """Render an expected failure as a compact one-line diagnostic for stderr."""
    if isinstance(error, BarAPIError):
        details = [f"HTTP {error.status_code}", f"{error.method} {error.path}", error.error]
        if error.code is not None:
            details.append(f"code={error.code}")
        return " | ".join(details)
    if isinstance(error, BarRequestError):
        return f"request error | {error.method} {error.path} | attempts={error.attempts} | {error.message}"
    return str(error)
