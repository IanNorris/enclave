"""Tests for the cross-session memory store."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from enclave.orchestrator.memory import MemoryStore, CATEGORIES


@pytest.fixture
def store(tmp_path):
    """Create a temporary memory store."""
    s = MemoryStore(str(tmp_path), "@test:example.com")
    yield s
    s.close()


class TestStore:
    def test_store_and_count(self, store):
        assert store.count() == 0
        store.store("User prefers Python", category="technical")
        assert store.count() == 1

    def test_store_returns_memory(self, store):
        mem = store.store("User's name is Alice", category="personal", is_key_memory=True)
        assert mem.id
        assert mem.content == "User's name is Alice"
        assert mem.category == "personal"
        assert mem.is_key_memory is True
        assert mem.access_count == 0

    def test_invalid_category_defaults_to_other(self, store):
        mem = store.store("test", category="invalid_cat")
        assert mem.category == "other"

    def test_store_multiple(self, store):
        store.store("fact 1")
        store.store("fact 2")
        store.store("fact 3")
        assert store.count() == 3


class TestQuery:
    def test_query_by_keyword(self, store):
        store.store("User prefers Python for backend")
        store.store("User likes TypeScript for frontend")
        store.store("User's cat is named Whiskers")

        results = store.query(keyword="Python")
        assert len(results) == 1
        assert "Python" in results[0].content

    def test_query_by_category(self, store):
        store.store("Python preference", category="technical")
        store.store("Name is Alice", category="personal")

        results = store.query(category="technical")
        assert len(results) == 1
        assert results[0].category == "technical"

    def test_query_combined(self, store):
        store.store("Python preference", category="technical")
        store.store("Python is fun", category="personal")
        store.store("TypeScript preference", category="technical")

        results = store.query(keyword="Python", category="technical")
        assert len(results) == 1
        assert "Python" in results[0].content
        assert results[0].category == "technical"

    def test_query_empty(self, store):
        results = store.query(keyword="nonexistent")
        assert results == []

    def test_query_updates_access(self, store):
        store.store("fact 1")
        store.query(keyword="fact")  # first access
        results = store.query(keyword="fact")  # second access sees count=1
        assert len(results) == 1
        assert results[0].access_count == 1

    def test_query_limit(self, store):
        for i in range(10):
            store.store(f"fact {i}")
        results = store.query(limit=3)
        assert len(results) == 3

    def test_query_case_insensitive(self, store):
        store.store("User prefers PYTHON")
        results = store.query(keyword="python")
        assert len(results) == 1


class TestKeyMemories:
    def test_list_key_memories(self, store):
        store.store("Regular memory", is_key_memory=False)
        store.store("Key memory 1", is_key_memory=True)
        store.store("Key memory 2", is_key_memory=True)

        keys = store.list_key_memories()
        assert len(keys) == 2
        assert all(m.is_key_memory for m in keys)

    def test_key_memories_as_prompt_empty(self, store):
        prompt = store.key_memories_as_prompt()
        assert prompt == ""

    def test_key_memories_as_prompt(self, store):
        store.store("User's name is Alice", category="personal", is_key_memory=True)
        store.store("Prefers 4-space indentation", category="technical", is_key_memory=True)

        prompt = store.key_memories_as_prompt()
        assert "## Your Memories" in prompt
        assert "Alice" in prompt
        assert "4-space" in prompt

    def test_key_memories_prompt_truncation(self, store):
        for i in range(300):
            store.store(f"Memory entry number {i}", is_key_memory=True)

        prompt = store.key_memories_as_prompt(max_lines=10)
        lines = prompt.strip().split("\n")
        assert len(lines) <= 11  # 10 + possible truncation notice


class TestDelete:
    def test_delete_existing(self, store):
        mem = store.store("to delete")
        assert store.delete(mem.id) is True
        assert store.count() == 0

    def test_delete_nonexistent(self, store):
        assert store.delete("nonexistent") is False


class TestListRecent:
    def test_list_recent(self, store):
        store.store("old")
        store.store("new")
        results = store.list_recent(limit=1)
        assert len(results) == 1
        assert results[0].content == "new"


class TestDreaming:
    def test_store_from_dreaming(self, store):
        extracted = [
            {"content": "User prefers dark themes", "category": "personal", "is_key": True},
            {"content": "Project uses FastAPI", "category": "project"},
            {"content": "", "category": "other"},  # empty — should be skipped
        ]
        stored = store.store_from_dreaming(extracted, source_session="test-123")
        assert stored == 2
        assert store.count() == 2

    def test_dreaming_deduplication(self, store):
        store.store("User prefers dark themes")
        extracted = [
            {"content": "User prefers dark themes"},  # exact duplicate
            {"content": "New fact"},
        ]
        stored = store.store_from_dreaming(extracted)
        assert stored == 1
        assert store.count() == 2

    def test_dreaming_empty_list(self, store):
        stored = store.store_from_dreaming([])
        assert stored == 0


class TestPersistence:
    def test_data_survives_reopen(self, tmp_path):
        s1 = MemoryStore(str(tmp_path), "@persist:test.com")
        s1.store("persistent fact", is_key_memory=True)
        s1.close()

        s2 = MemoryStore(str(tmp_path), "@persist:test.com")
        assert s2.count() == 1
        keys = s2.list_key_memories()
        assert len(keys) == 1
        assert keys[0].content == "persistent fact"
        s2.close()


class TestCategories:
    def test_all_valid_categories(self, store):
        for cat in CATEGORIES:
            mem = store.store(f"test {cat}", category=cat)
            assert mem.category == cat
