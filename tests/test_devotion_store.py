import os
import tempfile

from endstone_arc_ai_helper.devotion_store import DevotionStore, merge_devotion_config


def test_merge_devotion_config_defaults():
    cfg = merge_devotion_config(None)
    assert cfg["enabled"] is False
    assert cfg["max_long_term"] == 100
    assert cfg["default_long_term"] == 1


def test_dual_favor_prayer_and_consume():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "devotion.json")
        store = DevotionStore(path, {"enabled": True, "default_long_term": 1})
        record = store.get_record(name="Steve")
        assert record["long_term"] == 1
        assert record["short_term"] == 0

        ok, msg, state = store.adjust_faith(
            name="Steve", short_delta=3, long_delta=2, kind="prayer"
        )
        assert ok is True
        assert state["short_term"] == 1
        assert state["long_term"] == 3

        ok, msg, state = store.adjust_faith(
            name="Steve", short_delta=5, long_delta=0, kind="offering"
        )
        assert ok is True
        assert state["short_term"] == 3
        assert state["long_term"] == 3

        ok, msg, state = store.consume_short_favor(name="Steve", cost=2, reason="test")
        assert ok is True
        assert state["short_term"] == 1

        ok, msg, _ = store.consume_short_favor(name="Steve", cost=5, reason="fail")
        assert ok is False
        assert "不足" in msg


def test_auto_split_gain():
    to_short, to_long = DevotionStore.auto_split_gain(12, long_term=10, short_term=4, long_cap=5)
    assert to_short == 6
    assert to_long == 5

    to_short, to_long = DevotionStore.auto_split_gain(3, long_term=5, short_term=5, long_cap=5)
    assert to_short == 0
    assert to_long == 3


def test_long_growth_cap_enforced():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "devotion.json")
        store = DevotionStore(path, {"enabled": True, "long_growth_cap": 5})
        ok, _msg, state = store.adjust_faith(
            name="Alex", short_delta=0, long_delta=20, kind="prayer"
        )
        assert ok is True
        assert state["long_term"] == 6
