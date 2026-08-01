"""Tests for connection settings resolved from flags and the environment."""

import pytest

from busyboy.config import ConfigError, load_config


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
    assert config.token.get_secret_value() == "envtoken"


def test_arguments_override_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config(host="192.168.1.5", token="flagtoken")

    assert config.host == "192.168.1.5"
    assert config.token.get_secret_value() == "flagtoken"


def test_a_single_override_leaves_the_rest_to_the_environment(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    config = load_config(host="192.168.1.5")

    assert config.host == "192.168.1.5"
    assert config.token.get_secret_value() == "envtoken"


def test_arguments_alone_are_enough():
    config = load_config(host="192.168.1.5", token="flagtoken")

    assert config.host == "192.168.1.5"
    assert config.token.get_secret_value() == "flagtoken"


def test_missing_host_names_both_the_variable_and_the_flag(monkeypatch):
    monkeypatch.setenv("BUSYBOY_TOKEN", "envtoken")

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "BUSYBOY_HOST" in message
    assert "--host" in message


def test_missing_token_names_both_the_variable_and_the_flag(monkeypatch):
    monkeypatch.setenv("BUSYBOY_HOST", "10.0.4.20")

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "BUSYBOY_TOKEN" in message
    assert "--token" in message


def test_both_missing_reports_both():
    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "BUSYBOY_HOST" in message
    assert "BUSYBOY_TOKEN" in message


def test_the_token_stays_out_of_the_repr():
    config = load_config(host="10.0.4.20", token="supersecret")

    assert "supersecret" not in repr(config)
