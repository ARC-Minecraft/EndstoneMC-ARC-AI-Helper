from endstone_arc_ai_helper.ai_permission import (
    AIPermissionLevel,
    parse_permission_level,
    require_admin_level,
    resolve_ai_capability_ceiling,
    resolve_permission_level,
    resolve_requester_level,
    validate_command_for_level,
)


def test_assistant_tp_allowed():
    ok, err = validate_command_for_level("tp Steve 100 64 200", AIPermissionLevel.ASSISTANT)
    assert ok is True
    assert err == ""


def test_assistant_gamemode_denied():
    ok, err = validate_command_for_level("gamemode creative Steve", AIPermissionLevel.ASSISTANT)
    assert ok is False
    assert "管理员" in err


def test_admin_gamemode_allowed():
    ok, err = validate_command_for_level("gamemode creative Steve", AIPermissionLevel.ADMIN)
    assert ok is True


def test_admin_op_denied():
    ok, err = validate_command_for_level("op Steve", AIPermissionLevel.ADMIN)
    assert ok is False
    assert "代理服主" in err


def test_admin_deop_denied():
    ok, err = validate_command_for_level("deop Steve", AIPermissionLevel.ADMIN)
    assert ok is False
    assert "代理服主" in err


def test_admin_execute_run_op_denied():
    ok, err = validate_command_for_level(
        "execute as @a run op @s", AIPermissionLevel.ADMIN
    )
    assert ok is False
    assert "代理服主" in err
    assert "op" in err


def test_proxy_owner_op_allowed():
    ok, err = validate_command_for_level("op Steve", AIPermissionLevel.PROXY_OWNER)
    assert ok is True
    assert err == ""


def test_admin_ban_denied():
    ok, err = validate_command_for_level("ban Steve", AIPermissionLevel.ADMIN)
    assert ok is False
    assert "代理服主" in err


def test_proxy_owner_ban_allowed():
    ok, err = validate_command_for_level("ban Steve", AIPermissionLevel.PROXY_OWNER)
    assert ok is True


def test_parse_level_aliases():
    assert parse_permission_level("admin") == AIPermissionLevel.ADMIN
    assert parse_permission_level("管理员") == AIPermissionLevel.ADMIN
    assert parse_permission_level("3") == AIPermissionLevel.PROXY_OWNER


class _FakePlayer:
    def __init__(self, *, name="Steve", xuid="123", is_op=False, perms=None):
        self.name = name
        self.xuid = xuid
        self.is_op = is_op
        self._perms = set(perms or [])

    def has_permission(self, node: str) -> bool:
        return node in self._perms


def test_ceiling_from_config_admin():
    assert (
        resolve_ai_capability_ceiling({"default_permission_level": "admin"})
        == AIPermissionLevel.ADMIN
    )


def test_ceiling_alias_ai_capability_level():
    assert (
        resolve_ai_capability_ceiling({"ai_capability_level": "proxy_owner"})
        == AIPermissionLevel.PROXY_OWNER
    )


def test_resolve_normal_player_stays_assistant_when_ceiling_admin():
    """Config admin is AI ceiling, not requester identity."""
    level = resolve_permission_level(
        player=_FakePlayer(is_op=False),
        chat_config={"default_permission_level": "admin"},
    )
    assert level == AIPermissionLevel.ASSISTANT


def test_resolve_op_gets_admin_when_ceiling_admin():
    level = resolve_permission_level(
        player=_FakePlayer(is_op=True),
        chat_config={"default_permission_level": "admin"},
    )
    assert level == AIPermissionLevel.ADMIN


def test_resolve_op_clamped_by_assistant_ceiling():
    level = resolve_permission_level(
        player=_FakePlayer(is_op=True),
        chat_config={"default_permission_level": "assistant"},
    )
    assert level == AIPermissionLevel.ASSISTANT


def test_resolve_is_op_maps_admin():
    level = resolve_permission_level(
        player=_FakePlayer(is_op=True),
        chat_config={"default_permission_level": "admin"},
        op_maps_to_admin=True,
    )
    assert level == AIPermissionLevel.ADMIN


def test_local_player_ignores_inflated_payload_level():
    level = resolve_permission_level(
        player=_FakePlayer(is_op=False),
        chat_config={"default_permission_level": "admin"},
        payload_level="proxy_owner",
        payload_is_op=True,
    )
    assert level == AIPermissionLevel.ASSISTANT


def test_hub_payload_without_player_uses_payload_clamped_by_ceiling():
    level = resolve_permission_level(
        player=None,
        chat_config={"default_permission_level": "admin"},
        payload_level="admin",
        payload_is_op=True,
    )
    assert level == AIPermissionLevel.ADMIN


def test_hub_payload_proxy_owner_clamped_to_admin_ceiling():
    level = resolve_permission_level(
        player=None,
        chat_config={"default_permission_level": "admin"},
        payload_level="proxy_owner",
    )
    assert level == AIPermissionLevel.ADMIN


def test_requester_override_by_name():
    level = resolve_requester_level(
        player=_FakePlayer(name="Steve"),
        chat_config={
            "default_permission_level": "admin",
            "permission_overrides": {"Steve": "proxy_owner"},
        },
    )
    assert level == AIPermissionLevel.PROXY_OWNER


def test_require_admin_level():
    assert require_admin_level(AIPermissionLevel.ADMIN) == ""
    assert require_admin_level(AIPermissionLevel.ASSISTANT) != ""
