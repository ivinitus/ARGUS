from __future__ import annotations

import pytest

from argus import config as argus_config
from argus.pipeline import env_check_backfill


@pytest.mark.parametrize("profile", ["tablet", "tv", "reader", "mobile"])
def test_disabled_profile_skips_environment_backfill(monkeypatch, profile):
    settings = argus_config.load(profile=profile)
    monkeypatch.setattr(
        argus_config,
        "load",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        "argus.runtime.configure",
        lambda configured: None,
    )
    monkeypatch.setattr(
        env_check_backfill,
        "run_backfill",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled profile must not run backfill")
        ),
    )

    assert env_check_backfill.main([]) == 0
