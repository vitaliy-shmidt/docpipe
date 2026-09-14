from __future__ import annotations

import pytest

from system.ai.prompt_registry import PromptRegistry, is_valid_version_name
from system.errors import DocPipeError


@pytest.fixture
def registry(tmp_path):
    base = tmp_path / "base"
    runtime = tmp_path / "runtime"
    (base / "maintenance_extraction").mkdir(parents=True)
    (base / "maintenance_extraction" / "v1.txt").write_text(
        "BASE v1 {context_block} {text}", encoding="utf-8"
    )
    return PromptRegistry(base, runtime, max_content_length=1000)


def test_version_name_validation():
    assert is_valid_version_name("v1")
    assert is_valid_version_name("v2")
    assert is_valid_version_name("v10")
    assert not is_valid_version_name("v0")  # no leading zero / zero version
    assert not is_valid_version_name("v01")
    assert not is_valid_version_name("V1")  # case-sensitive
    assert not is_valid_version_name("1")
    assert not is_valid_version_name("../../foo")
    assert not is_valid_version_name("test.txt")
    assert not is_valid_version_name("v1/abc")
    assert not is_valid_version_name("")


def test_list_versions_finds_base_only(registry):
    assert registry.list_versions("maintenance_extraction") == ["v1"]


def test_resolve_active_version_falls_back_to_default_with_no_active_json(registry):
    assert registry.resolve_active_version("maintenance_extraction", "v1") == "v1"


def test_load_prompt_reads_base_file(registry):
    content = registry.load_prompt("maintenance_extraction", "v1")
    assert "BASE v1" in content


def test_load_prompt_unknown_version_raises(registry):
    with pytest.raises(DocPipeError) as exc_info:
        registry.load_prompt("maintenance_extraction", "v99")
    assert exc_info.value.code == "unknown_prompt_version"
    assert exc_info.value.status_code == 404


def test_save_version_writes_to_runtime_only(registry, tmp_path):
    registry.save_version("maintenance_extraction", "v2", "DRAFT v2 {context_block} {text}")
    runtime_file = tmp_path / "runtime" / "maintenance_extraction" / "v2.txt"
    base_file = tmp_path / "base" / "maintenance_extraction" / "v2.txt"
    assert runtime_file.is_file()
    assert not base_file.exists()
    assert registry.load_prompt("maintenance_extraction", "v2") == "DRAFT v2 {context_block} {text}"


def test_save_version_appears_in_list_versions_merged_with_base(registry):
    registry.save_version("maintenance_extraction", "v2", "DRAFT v2 {context_block} {text}")
    assert registry.list_versions("maintenance_extraction") == ["v1", "v2"]


def test_save_version_duplicate_against_base_rejected(registry):
    # v1 already exists in base - saving "v1" again (even with different
    # content) must never silently overwrite it.
    with pytest.raises(DocPipeError) as exc_info:
        registry.save_version("maintenance_extraction", "v1", "overwrite attempt")
    assert exc_info.value.code == "prompt_version_exists"
    assert exc_info.value.status_code == 409


def test_save_version_duplicate_against_runtime_rejected(registry):
    registry.save_version("maintenance_extraction", "v2", "first save")
    with pytest.raises(DocPipeError) as exc_info:
        registry.save_version("maintenance_extraction", "v2", "second save attempt")
    assert exc_info.value.code == "prompt_version_exists"
    # The first save must be untouched.
    assert registry.load_prompt("maintenance_extraction", "v2") == "first save"


def test_save_version_invalid_name_rejected(registry):
    with pytest.raises(DocPipeError) as exc_info:
        registry.save_version("maintenance_extraction", "../../evil", "content")
    assert exc_info.value.code == "invalid_prompt_version"


def test_save_version_empty_content_rejected(registry):
    with pytest.raises(DocPipeError) as exc_info:
        registry.save_version("maintenance_extraction", "v2", "   ")
    assert exc_info.value.code == "invalid_prompt"


def test_save_version_too_large_rejected(tmp_path):
    registry = PromptRegistry(tmp_path / "base", tmp_path / "runtime", max_content_length=10)
    (tmp_path / "base" / "m").mkdir(parents=True)
    (tmp_path / "base" / "m" / "v1.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DocPipeError) as exc_info:
        registry.save_version("m", "v2", "this is definitely longer than ten characters")
    assert exc_info.value.code == "prompt_too_large"


def test_activate_version_switches_resolve_active_version(registry):
    registry.save_version("maintenance_extraction", "v2", "DRAFT v2 {context_block} {text}")
    assert registry.resolve_active_version("maintenance_extraction", "v1") == "v1"
    registry.activate_version("maintenance_extraction", "v2")
    assert registry.resolve_active_version("maintenance_extraction", "v1") == "v2"


def test_activate_missing_version_rejected(registry):
    with pytest.raises(DocPipeError) as exc_info:
        registry.activate_version("maintenance_extraction", "v99")
    assert exc_info.value.code == "unknown_prompt_version"
    assert exc_info.value.status_code == 404


def test_activate_does_not_require_a_prior_save_for_a_base_version(registry):
    # Re-activating the already-existing base default is a legitimate
    # (if unusual) operation - e.g. reverting after activating v2.
    registry.activate_version("maintenance_extraction", "v1")
    assert registry.resolve_active_version("maintenance_extraction", "v1") == "v1"


def test_hot_reload_without_restart(registry):
    """The exact scenario from the task's §25: activate must take effect
    on the very next resolve call - no cache, nothing to invalidate."""
    registry.save_version("maintenance_extraction", "v2", "DRAFT v2 {context_block} {text}")
    assert registry.resolve_active_version("maintenance_extraction", "v1") == "v1"
    registry.activate_version("maintenance_extraction", "v2")
    # Same PromptRegistry instance, same process - no restart of any kind.
    assert registry.resolve_active_version("maintenance_extraction", "v1") == "v2"
    assert "DRAFT v2" in registry.load_prompt(
        "maintenance_extraction", registry.resolve_active_version("maintenance_extraction", "v1")
    )


def test_version_exists_checks_both_layers(registry):
    assert registry.version_exists("maintenance_extraction", "v1") is True
    assert registry.version_exists("maintenance_extraction", "v2") is False
    registry.save_version("maintenance_extraction", "v2", "content {context_block} {text}")
    assert registry.version_exists("maintenance_extraction", "v2") is True


def test_default_exists_true_for_real_base_file(registry):
    assert registry.default_exists("maintenance_extraction", "v1") is True


def test_default_exists_false_for_missing_mode(registry):
    assert registry.default_exists("does_not_exist", "v1") is False


def test_subdir_traversal_rejected(registry):
    with pytest.raises(ValueError):
        registry.list_versions("../../etc")


def test_assistant_style_nested_subdir_supported(tmp_path):
    base = tmp_path / "base"
    runtime = tmp_path / "runtime"
    (base / "assistant" / "hotel_health_summary").mkdir(parents=True)
    (base / "assistant" / "hotel_health_summary" / "v1.txt").write_text(
        "assistant base {context_block} {question}", encoding="utf-8"
    )
    registry = PromptRegistry(base, runtime, max_content_length=1000)
    assert registry.resolve_active_version("assistant/hotel_health_summary", "v1") == "v1"
    assert "assistant base" in registry.load_prompt("assistant/hotel_health_summary", "v1")
