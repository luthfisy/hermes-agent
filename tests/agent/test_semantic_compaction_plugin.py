from types import SimpleNamespace

from plugins import semantic_compaction as discovery
from semantic_compaction_provider import SemanticCompactor


class FakeCompactor(SemanticCompactor):
    def is_available(self):
        return True

    def propose(self, request):
        return None


class FakeEntryPoint:
    group = "hermes_agent.semantic_compactors"

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def load(self):
        return self.value


def test_find_entry_point_uses_separate_semantic_compactor_group(monkeypatch):
    good = FakeEntryPoint("reliquary", FakeCompactor)
    wrong = SimpleNamespace(
        group="hermes_agent.conversation_indexes",
        name="reliquary",
        load=lambda: FakeCompactor,
    )
    monkeypatch.setattr(discovery.importlib.metadata, "entry_points", lambda: [wrong, good])

    assert discovery.find_semantic_compactor_entry_point("reliquary") is good
    assert discovery.find_semantic_compactor_entry_point("missing") is None


def test_loader_accepts_class_instance_and_factory(monkeypatch):
    for value in (FakeCompactor, FakeCompactor(), lambda: FakeCompactor()):
        ep = FakeEntryPoint("reliquary", value)
        monkeypatch.setattr(
            discovery,
            "find_semantic_compactor_entry_point",
            lambda name, ep=ep: ep,
        )
        loaded = discovery.load_semantic_compactor("reliquary")
        assert isinstance(loaded, FakeCompactor)


def test_loader_rejects_wrong_capability(monkeypatch):
    ep = FakeEntryPoint("reliquary", lambda: object())
    monkeypatch.setattr(discovery, "find_semantic_compactor_entry_point", lambda name: ep)

    assert discovery.load_semantic_compactor("reliquary") is None
