from polydrift.config import DEFAULTS, deep_merge


def test_nested_defaults_survive_a_toplevel_override():
    """Regression: a shallow merge lets a top-level key in config.yaml wipe the
    nested defaults underneath it, silently zeroing out settings."""
    merged = deep_merge(DEFAULTS, {"universe": {"max_markets": 5}})
    assert merged["universe"]["max_markets"] == 5
    assert merged["universe"]["min_liquidity_usd"] == DEFAULTS["universe"]["min_liquidity_usd"]
    assert merged["http"]["user_agent"] == DEFAULTS["http"]["user_agent"]


def test_lists_replace_rather_than_concat():
    merged = deep_merge(DEFAULTS, {"universe": {"exclude_question_patterns": ["(?i)foo"]}})
    assert merged["universe"]["exclude_question_patterns"] == ["(?i)foo"]


def test_merge_does_not_mutate_defaults():
    deep_merge(DEFAULTS, {"universe": {"max_markets": 1}})
    assert DEFAULTS["universe"]["max_markets"] == 60
