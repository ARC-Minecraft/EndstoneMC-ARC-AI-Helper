from endstone_arc_ai_helper.player_inventory import count_item, remove_item_count


class FakeStack:
    def __init__(self, type_id: str, amount: int = 1):
        self.type = type(type("T", (), {"id": type_id})())
        self.amount = amount
        self.data = 0


class FakeInv:
    def __init__(self, slots):
        self._slots = dict(slots)
        self.size = 36

    def get_item(self, idx: int):
        return self._slots.get(idx)

    def set_item(self, idx: int, stack):
        if stack is None:
            self._slots.pop(idx, None)
        else:
            self._slots[idx] = stack


class FakePlayer:
    def __init__(self, inv):
        self.inventory = inv


def test_remove_item_count_partial_and_full():
    inv = FakeInv({0: FakeStack("minecraft:diamond", 10)})
    player = FakePlayer(inv)
    assert count_item(player, "diamond") == 10
    removed = remove_item_count(player, "minecraft:diamond", 3)
    assert removed == 3
    assert count_item(player, "diamond") == 7
    removed = remove_item_count(player, "diamond", 10)
    assert removed == 7
    assert count_item(player, "diamond") == 0
