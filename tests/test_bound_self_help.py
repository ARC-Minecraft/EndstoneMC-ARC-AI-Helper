from endstone_arc_ai_helper.bound_self_help import validate_bound_self_help_command


def test_tp_to_coordinates_allowed():
    ok, err = validate_bound_self_help_command("tp Steve 100 64 200", "Steve")
    assert ok is True
    assert err == ""


def test_tp_other_player_denied():
    ok, err = validate_bound_self_help_command("tp Alex 100 64 200", "Steve")
    assert ok is False
    assert "绑定角色" in err


def test_execute_tp_allowed():
    ok, err = validate_bound_self_help_command(
        "execute in overworld run tp Steve 100 64 200",
        "Steve",
    )
    assert ok is True
    assert err == ""


def test_give_denied():
    ok, err = validate_bound_self_help_command("give Steve diamond 64", "Steve")
    assert ok is False
    assert "不允许" in err


def test_harmful_effect_denied():
    ok, err = validate_bound_self_help_command("effect Steve poison 10 0", "Steve")
    assert ok is False
    assert "负面效果" in err
