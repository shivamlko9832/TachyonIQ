"""
Tests for uada/config.py.

Regression coverage for a real bug found while wiring up a live LLM
provider: .env.example itself documents dropping ANTHROPIC_API_KEY or
OPENAI_API_KEY into .env, unprefixed, alongside the UADA_-prefixed
settings -- but Settings() used to crash on exactly that setup with
"Extra inputs are not permitted", since pydantic-settings' env_prefix
only controls which keys map to a field, not which keys from a dotenv
file are tolerated as unrelated.
"""

from __future__ import annotations

import pytest

from uada.config import Settings

pytestmark = pytest.mark.unit


class TestExtraEnvironmentVariablesAreIgnored:
    def test_unprefixed_provider_key_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exactly .env.example's own documented pattern: an unprefixed,
        # third-party-SDK-owned key living alongside UADA_ settings.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        assert settings.db_url.get_secret_value() == "sqlite:///:memory:"

    def test_unprefixed_key_is_not_captured_as_a_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        assert not hasattr(settings, "openai_api_key")

    def test_anthropic_key_variant_also_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        assert settings.db_url.get_secret_value() == "sqlite:///:memory:"

    def test_completely_unrelated_env_var_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not even a provider SDK's own var -- just some other unrelated
        # environment variable that happens to be set in the process.
        monkeypatch.setenv("SOME_OTHER_TOOLS_VARIABLE", "whatever")
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        assert settings.db_url.get_secret_value() == "sqlite:///:memory:"

    def test_uada_prefixed_settings_still_take_effect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # extra="ignore" must only affect *unmapped* keys -- real UADA_
        # settings should still override defaults as before.
        monkeypatch.setenv("UADA_DB_MAX_ROWS", "42")
        settings = Settings(db_url="sqlite:///:memory:")  # type: ignore[call-arg]
        assert settings.db_max_rows == 42
