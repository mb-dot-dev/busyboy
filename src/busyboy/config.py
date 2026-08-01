"""BUSY Bar connection settings, resolved from CLI flags and the environment."""

from typing import Any

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class BusyboyConfig(BaseSettings):
    """Connection settings read from BUSYBOY_HOST and BUSYBOY_TOKEN."""

    model_config = SettingsConfigDict(env_prefix="BUSYBOY_", frozen=True)

    host: str
    token: SecretStr


class ConfigError(Exception):
    """Raised when required connection settings are missing or malformed."""


def load_config(
    *,
    host: str | None = None,
    token: str | None = None,
) -> BusyboyConfig:
    """
    Build the config, letting explicit values win over the environment.

    Only arguments that were actually supplied are passed through, because
    pydantic-settings ranks init arguments above environment variables.
    """
    # Typed as Any because BaseSettings.__init__ also accepts private
    # keyword arguments; a narrower dict trips the type checker on unpack.
    overrides: dict[str, Any] = {key: value for key, value in (("host", host), ("token", token)) if value is not None}
    try:
        return BusyboyConfig(**overrides)
    except ValidationError as error:
        # `from None` breaks the exception chain deliberately: pydantic's
        # ValidationError carries the raw pre-coercion input in __cause__,
        # which includes the token. Chaining it would leak the token to
        # stderr under --verbose, where cli.py re-raises and prints the
        # traceback.
        raise ConfigError(_format_config_error(error)) from None


def _format_config_error(error: ValidationError) -> str:
    """
    Render a config ValidationError naming both the env var and the flag.

    A user hitting this may know about neither, so every missing field is
    reported on its own line.
    """
    lines: list[str] = []
    for detail in error.errors():
        field = str(detail["loc"][0])
        if detail["type"] == "missing":
            lines.append(f"Missing configuration: BUSYBOY_{field.upper()} is not set (or pass --{field})")
        else:
            lines.append(f"Invalid configuration: {field}: {detail['msg']}")
    return "\n".join(lines)
