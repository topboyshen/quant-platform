"""RL 模型 key 约定（不加载 zip）。"""


from radar.strategy.feature_extractor import CachedModelPredictor


def test_make_key_variants():
    p = CachedModelPredictor("/tmp/rl_models")
    assert p._make_key("below", True, "", "") == "ppo_universal_below"
    assert p._make_key("below", True, "", "ext") == "ppo_universal_below_ext"
    assert p._make_key("below", False, "300033", "") == "ppo_300033_below"
    assert p._make_key("below", False, "300033", "ext") == "ppo_300033_below_ext"
