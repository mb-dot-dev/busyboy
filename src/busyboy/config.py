"""BUSY Bar connection settings, resolved from CLI flags and the environment."""

from typing import Any

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class BusyboyConfig(BaseSettings):
    """Connection settings read from BUSYBOY_HOST and BUSYBOY_TOKEN."""

    model_config = SettingsConfigDict(env_prefix="BUSYBOY_", frozen=True)

    host: str = "10.0.4.20"
    token: SecretStr | None = None


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
    """Render a config ValidationError naming the offending field."""
    lines = [f"Invalid configuration: {detail['loc'][0]}: {detail['msg']}" for detail in error.errors()]
    return "\n".join(lines)
