import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.services.workflow.runtime_profile import (
    runtime_profile,
    require_runtime_capabilities,
)


def configuration(realtime=False):
    return SimpleNamespace(
        is_realtime=realtime,
        realtime=SimpleNamespace(provider="openai", model="speech")
        if realtime
        else None,
        llm=SimpleNamespace(provider="openai", model="text", api_key="never-expose"),
        tts=SimpleNamespace(provider="minimax", model="speech", api_key="never-expose"),
    )


def test_unapplied_cache_is_visible_and_secrets_are_excluded():
    profile = runtime_profile(
        {"tts_cache_enabled": True}, configuration(), channel="browser"
    )
    cache = next(s for s in profile["settings"] if s["key"] == "tts_cache_enabled")
    assert cache["requested"] is True and cache["resolved"] is False
    assert "never-expose" not in json.dumps(profile)


def test_realtime_requirements_fail_before_execution():
    profile = runtime_profile(
        {
            "context_compaction_enabled": True,
            "runtime_requirements": ["local_context_compaction"],
        },
        configuration(True),
        channel="phone",
    )
    assert not profile["ready"]
    with pytest.raises(HTTPException):
        require_runtime_capabilities(profile)


def test_profile_changes_with_effective_model_and_not_with_secret():
    config = configuration()
    before = runtime_profile({}, config, channel="text")
    config.llm.api_key = "rotated"
    assert (
        runtime_profile({}, config, channel="text")["fingerprint"]
        == before["fingerprint"]
    )
    config.llm.model = "other"
    assert (
        runtime_profile({}, config, channel="text")["model_fingerprint"]
        != before["model_fingerprint"]
    )
