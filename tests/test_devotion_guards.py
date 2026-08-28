from endstone_arc_ai_helper.devotion_guards import (
    clamp_player_blessing,
    is_devotion_blessing_command,
    min_favor_for_effect,
    should_block_devotion_bypass,
    validate_divine_favor_cost,
)
from endstone_arc_ai_helper.ai_permission import AIPermissionLevel


def test_is_devotion_blessing_command():
    assert is_devotion_blessing_command("give Steve diamond 1") is True
    assert is_devotion_blessing_command("effect Steve strength 60 4 true") is True
    assert is_devotion_blessing_command("tp Steve 0 80 0") is True
    assert is_devotion_blessing_command("execute at Steve run summon lightning_bolt ~ ~ ~") is False
    assert is_devotion_blessing_command("execute at Steve run effect Steve speed 30 0 true") is True


def test_clamp_player_blessing_rejects_high_amplifier():
    amp, duration, msg = clamp_player_blessing(amplifier=4, duration_seconds=120)
    assert amp == 4
    assert "过高" in msg


def test_min_favor_for_effect_scales_with_amplifier():
    low = min_favor_for_effect(amplifier=0, duration_seconds=60)
    high = min_favor_for_effect(amplifier=1, duration_seconds=60)
    assert high > low


def test_validate_divine_favor_cost_rejects_cheap_blessing():
    ok, msg, minimum = validate_divine_favor_cost(
        favor_cost=3,
        blessing="strength",
        amplifier=1,
        duration_seconds=180,
    )
    assert ok is False
    assert minimum >= 8
    assert "贪求" in msg


def test_should_block_devotion_bypass_for_assistant():
    blocked, msg = should_block_devotion_bypass(
        "give Steve diamond 1",
        AIPermissionLevel.ASSISTANT,
        devotion_enabled=True,
    )
    assert blocked is True
    assert "mc_divine_intervention" in msg


def test_should_not_block_devotion_bypass_for_admin():
    blocked, msg = should_block_devotion_bypass(
        "give Steve diamond 1",
        AIPermissionLevel.ADMIN,
        devotion_enabled=True,
    )
    assert blocked is False
    assert msg == ""


def test_should_not_block_admin_gamemode_even_with_devotion():
    blocked, msg = should_block_devotion_bypass(
        "gamemode creative Steve",
        AIPermissionLevel.ASSISTANT,
        devotion_enabled=True,
    )
    assert blocked is False
    assert msg == ""
