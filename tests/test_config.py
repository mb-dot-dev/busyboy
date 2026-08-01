"""Tests for connection settings resolved from flags and the environment."""

import pytest

from busyboy.config import load_config


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """Keep the developer's own BUSYBOY_* variables out of these tests."""
    monkeypatch.delenv("BUSYBOY_HOST", raising=False)
    monkeypatch.delenv("BUSYBOY_TOKEN", raising=False)


def test_reads_host_and_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config()

    assert config.host == "10.0.4.20"
    assert config.token is not None
    assert config.token.get_secret_value() == "envtoken"


def test_arguments_override_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config(host="192.168.1.5", token="flagtoken")

    assert config.host == "192.168.1.5"
    assert config.token is not None
    assert config.token.get_secret_value() == "flagtoken"


def test_a_single_override_leaves_the_rest_to_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config(host="192.168.1.5")

    assert config.host == "192.168.1.5"
    assert config.token is not None
    assert config.token.get_secret_value() == "envtoken"


def test_arguments_alone_are_enough():
    config = load_config(host="192.168.1.5", token="flagtoken")

    assert config.host == "192.168.1.5"
    assert config.token is not None
    assert config.token.get_secret_value() == "flagtoken"


def test_host_defaults_to_the_usb_address_when_unset():
    config = load_config()

    assert config.host == "10.0.4.20"


def test_token_defaults_to_none_when_unset():
    config = load_config()

    assert config.token is None


def test_the_token_stays_out_of_the_repr():
    config = load_config(host="10.0.4.20", token="supersecret")

    assert "supersecret" not in repr(config)


def test_token_value_unwraps_a_configured_token():
    config = load_config(host="10.0.4.20", token="supersecret")

    assert config.token_value == "supersecret"


def test_token_value_is_none_when_unset():
    config = load_config()

    assert config.token_value is None
