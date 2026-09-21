from types import SimpleNamespace

from conversation_index import ConversationIndex
from plugins import conversation_index as discovery


class FakeIndex(ConversationIndex):
    def is_available(self):
        return True

    def consume_changes(self, changes, *, after_cursor):
        return changes[-1].sequence if changes else after_cursor

    def search(self, query, *, conversation_ids, limit):
        return ()

    def rebuild_from_snapshot(self, snapshot):
        return snapshot.watermark


class FakeEntryPoint:
    group = "hermes_agent.conversation_indexes"

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def load(self):
        return self.value


def test_find_entry_point_uses_dedicated_group(monkeypatch):
    good = FakeEntryPoint("reliquary", FakeIndex)
    wrong = SimpleNamespace(group="other", name="reliquary", load=lambda: FakeIndex)
    monkeypatch.setattr(discovery.importlib.metadata, "entry_points", lambda: [wrong, good])

    assert discovery.find_conversation_index_entry_point("reliquary") is good
    assert discovery.find_conversation_index_entry_point("missing") is None


def test_loader_accepts_class_instance_and_factory(monkeypatch):
    for value in (FakeIndex, FakeIndex(), lambda: FakeIndex()):
        ep = FakeEntryPoint("reliquary", value)
        monkeypatch.setattr(discovery, "find_conversation_index_entry_point", lambda name, ep=ep: ep)
        loaded = discovery.load_conversation_index("reliquary")
        assert isinstance(loaded, FakeIndex)


def test_loader_rejects_wrong_contract(monkeypatch):
    ep = FakeEntryPoint("reliquary", lambda: object())
    monkeypatch.setattr(discovery, "find_conversation_index_entry_point", lambda name: ep)

    assert discovery.load_conversation_index("reliquary") is None
