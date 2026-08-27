
def test_assess_offering_stingy_rich_player():
    from endstone_arc_ai_helper.player_inventory import assess_offering_sincerity

    class FakeStack:
        def __init__(self, type_id: str, amount: int = 1):
            self.type = type(type("T", (), {"id": type_id})())
            self.amount = amount

    class FakeInv:
        size = 36
        helmet = FakeStack("minecraft:netherite_helmet")
        chestplate = FakeStack("minecraft:netherite_chestplate")
        leggings = FakeStack("minecraft:netherite_leggings")
        boots = FakeStack("minecraft:netherite_boots")

        def get_item(self, idx: int):
            if idx == 0:
                return FakeStack("minecraft:diamond_block", 32)
            if idx == 1:
                return FakeStack("minecraft:diamond", 48)
            return None

    class FakePlayer:
        inventory = FakeInv()

    result = assess_offering_sincerity(FakePlayer(), "minecraft:diamond", 3)
    assert result["is_stingy"] is True
    assert result["wealth_tier"] in ("殷实", "豪富")


def test_assess_offering_poor_player_sincere():
    from endstone_arc_ai_helper.player_inventory import assess_offering_sincerity

    class FakeInv:
        size = 36

        def get_item(self, idx: int):
            if idx == 0:
                return type(
                    "S",
                    (),
                    {
                        "type": type("T", (), {"id": "minecraft:bread"})(),
                        "amount": 5,
                    },
                )()
            return None

    class FakePlayer:
        inventory = FakeInv()

    result = assess_offering_sincerity(FakePlayer(), "minecraft:bread", 2)
    assert result["is_stingy"] is False
