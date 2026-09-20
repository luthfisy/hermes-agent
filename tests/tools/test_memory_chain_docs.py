"""Chain documents (#116564): MEMORY.md as an anchor for a chain of memory documents.

The anchor (MEMORY.md) stays small and keeps its char cap; documents it references with
``@doc:<name>`` live in ``memories/docs/<name>.md`` as first-class memory the tool can read
and append to, with NO char limit and without entering the system prompt.
"""

import json

import pytest

from tools.memory_tool import MemoryStore, memory_tool

ANCHOR_REF = "Dated memory chain: @doc:conventions-2026-09"
DOC = "conventions-2026-09"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=200, user_char_limit=150)
    s.load_from_disk()
    return s


def _call(store, **kwargs):
    return json.loads(memory_tool(store=store, **kwargs))


def test_referenced_chain_doc_appends_uncapped(store, tmp_path):
    assert _call(store, action="add", target="memory", content=ANCHOR_REF)["success"] is True

    big = "x" * 500  # 2.5x the anchor's cap: chain docs are not budgeted
    result = _call(store, action="add", target="memory", doc=DOC, content=big)

    assert result["success"] is True, result
    assert (tmp_path / "docs" / f"{DOC}.md").read_text(encoding="utf-8") == big
    # The anchor stayed small: the doc's size never lands on the anchor's budget.
    assert store._char_count("memory") < store.memory_char_limit


def test_unreferenced_chain_doc_write_is_refused(store, tmp_path):
    result = _call(store, action="add", target="memory", doc="orphan", content="nope")

    assert result["success"] is False
    assert "@doc:orphan" in result["error"]  # tells the model how to link it
    assert not (tmp_path / "docs" / "orphan.md").exists()


def test_read_returns_doc_entries_and_anchor_chain_list(store):
    _call(store, action="add", target="memory", content=ANCHOR_REF)
    _call(store, action="add", target="memory", doc=DOC, content="First note")
    _call(store, action="add", target="memory", doc=DOC, content="Second note")

    doc = _call(store, action="read", target="memory", doc=DOC)
    assert doc["entries"] == ["First note", "Second note"]
    assert "uncapped" in doc["usage"]
    # A '.md' suffix is accepted for a doc name.
    assert _call(store, action="read", target="memory", doc=f"{DOC}.md")["entries"] == doc["entries"]

    anchor = _call(store, action="read", target="memory")
    assert anchor["entries"] == [ANCHOR_REF]
    assert anchor["chain_docs"] == [{"name": DOC, "entries": 2, "chars": len("First note\n§\nSecond note")}]


def test_anchor_cap_still_applies(store):
    _call(store, action="add", target="memory", content=ANCHOR_REF)

    result = _call(store, action="add", target="memory", content="y" * 400)

    assert result["success"] is False
    assert "would exceed the limit" in result["error"]


@pytest.mark.parametrize("name", ["../escape", "a/b", "..", " "])
def test_unsafe_doc_names_are_rejected(store, tmp_path, name):
    result = _call(store, action="add", target="memory", doc=name, content="x")

    assert result["success"] is False, name
    assert list(tmp_path.rglob("*.md")) == []  # nothing written, anywhere


def test_chain_docs_hang_off_memory_not_user(store):
    result = _call(store, action="add", target="user", doc=DOC, content="x")

    assert result["success"] is False
    assert "MEMORY.md anchor" in result["error"]


def test_anchor_block_names_chain_docs_without_load(store):
    _call(store, action="add", target="memory", content=ANCHOR_REF)
    _call(store, action="add", target="memory", doc=DOC, content="secret detail")
    store.load_from_disk()

    block = store.format_for_system_prompt("memory")

    assert DOC in block and "Chain docs" in block
    assert "secret detail" not in block  # contents stay out of the prompt
