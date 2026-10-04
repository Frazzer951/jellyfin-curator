from pathlib import Path

import pytest
from pydantic import ValidationError

from jfcurator.config import ContinueWatchingRule, MediaFilterRule, Sort, load_config

EXAMPLE = Path(__file__).parent.parent / "config" / "config.example.yaml"


def test_example_config_loads(monkeypatch):
    monkeypatch.setenv("JELLYFIN_URL", "http://jf:8096")
    monkeypatch.setenv("JELLYFIN_API_KEY", "secret")
    cfg = load_config(EXAMPLE)
    assert cfg.jellyfin.api_key == "secret"
    rules = {r.name: r for r in cfg.iter_rules()}
    subs = rules["Anime - Missing Subtitles"]
    assert isinstance(subs, MediaFilterRule)
    assert subs.match.subtitles.present is False
    cw = rules["Anime - Continue Watching"]
    assert isinstance(cw, ContinueWatchingRule)
    assert cw.sort.key is Sort.LAST_PLAYED and cw.sort.descending


def test_missing_env_var_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("jellyfin: {url: http://x, api_key: '${NOPE}', user: u}\n")
    with pytest.raises(ValueError, match="NOPE"):
        load_config(p)


def test_unknown_keys_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "jellyfin: {url: http://x, api_key: k, user: u}\n"
        "libraries:\n  A:\n    rules:\n      R: {type: new_seasons, sorting: name}\n"
    )
    with pytest.raises(ValidationError, match="sorting"):
        load_config(p)
