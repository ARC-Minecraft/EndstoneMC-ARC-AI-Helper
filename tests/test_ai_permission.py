from endstone_arc_ai_helper.ai_permission import (
    AIPermissionLevel,
    parse_permission_level,
    require_admin_level,
    resolve_permission_level,
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


def test_resolve_default_assistant():
    level = resolve_permission_level(
        player=_FakePlayer(),
        chat_config={"default_permission_level": "assistant"},
    )
    assert level == AIPermissionLevel.ASSISTANT


def test_resolve_config_default_admin():
    level = resolve_permission_level(
        player=_FakePlayer(),
        chat_config={"default_permission_level": "admin"},
    )
    assert level == AIPermissionLevel.ADMIN


def test_resolve_is_op_maps_admin():
    level = resolve_permission_level(
        player=_FakePlayer(is_op=True),
        chat_config={"default_permission_level": "assistant"},
    )
    assert level == AIPermissionLevel.ADMIN


def test_require_admin_level():
    assert require_admin_level(AIPermissionLevel.ADMIN) == ""
    assert require_admin_level(AIPermissionLevel.ASSISTANT) != ""
