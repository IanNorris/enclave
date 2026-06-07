"""Tests for the data-driven consult_panel configuration."""

from __future__ import annotations

from pathlib import Path

from enclave.common import panel


class TestDefaults:
    def test_default_panel_has_five_members(self) -> None:
        p = panel.default_panel()
        ids = [m["id"] for m in p["members"]]
        assert ids == [
            "architect", "pragmatist", "skeptic", "contrarian", "operator",
        ]
        assert all(m["enabled"] for m in p["members"])

    def test_defaults_only_reference_public_models(self) -> None:
        # Guard against private/preview model ids leaking into the repo.
        p = panel.default_panel()
        for m in p["members"]:
            assert m["models"], f"{m['id']} has no models"
            assert all(isinstance(x, str) and x for x in m["models"])


class TestNormalize:
    def test_models_string_coerced_to_list(self) -> None:
        p = panel.normalize_panel(
            {"members": [{"name": "X", "models": "a, b\nc, a"}]}
        )
        # De-duped, order preserved, empties dropped.
        assert p["members"][0]["models"] == ["a", "b", "c"]

    def test_ids_are_unique_and_slugified(self) -> None:
        p = panel.normalize_panel(
            {"members": [{"name": "The Sage"}, {"name": "The Sage"}]}
        )
        assert [m["id"] for m in p["members"]] == ["the-sage", "the-sage-2"]

    def test_enabled_defaults_true(self) -> None:
        p = panel.normalize_panel({"members": [{"name": "X"}]})
        assert p["members"][0]["enabled"] is True


class TestPersistence:
    def test_load_seeds_defaults(self, tmp_path: Path) -> None:
        loaded = panel.load_panel(tmp_path)
        assert panel.panel_path(tmp_path).exists()
        assert len(loaded["members"]) == 5

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        saved = panel.save_panel(
            tmp_path,
            {"members": [
                {"name": "Custom", "voice": "v", "focus": "f",
                 "models": ["preview-model-x"], "enabled": True},
            ]},
        )
        again = panel.load_panel(tmp_path)
        assert again == saved
        assert again["members"][0]["models"] == ["preview-model-x"]

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        panel.panel_path(tmp_path).write_text("{ not json")
        loaded = panel.load_panel(tmp_path)
        assert len(loaded["members"]) == 5


class TestWorkspace:
    def test_enabled_members_excludes_disabled(self, tmp_path: Path) -> None:
        panel.write_workspace_panel(
            tmp_path,
            {"members": [
                {"name": "On", "voice": "v", "focus": "f", "enabled": True},
                {"name": "Off", "voice": "v", "focus": "f", "enabled": False},
            ]},
        )
        wp = panel.load_workspace_panel(tmp_path)
        names = [m["name"] for m in panel.enabled_members(wp)]
        assert names == ["On"]

    def test_missing_workspace_file_uses_defaults(self, tmp_path: Path) -> None:
        wp = panel.load_workspace_panel(tmp_path)
        assert len(wp["members"]) == 5

    def test_build_prompt_includes_role_and_problem(self) -> None:
        member = {"name": "The Skeptic", "voice": "v", "focus": "f"}
        prompt = panel.build_panelist_prompt(member, "MY PROBLEM")
        assert "The Skeptic" in prompt
        assert "MY PROBLEM" in prompt
