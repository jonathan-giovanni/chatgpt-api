import pytest

from chatgpt_api.providers.chatgpt.projects import (
    conversation_mode,
    normalize_project_alias,
    validate_project_mapping,
)


def test_project_aliases_are_case_and_accent_insensitive():
    assert normalize_project_alias("  ATENCIÓN  ") == "atencion"
    assert normalize_project_alias("Customer Support") == "customer-support"


def test_project_mapping_keeps_real_id_out_of_public_payload():
    mapping = validate_project_mapping(
        "support",
        "Support",
        "g-p-0123456789abcdef0123456789abcdef",
        "plus-work",
    )

    public = mapping.public_dict()

    assert public["project_id"] == "g-p-01…cdef"
    assert "0123456789abcdef0123456789abcdef" not in str(public)


def test_conversation_mode_is_explicit_for_project_and_normal_chat():
    assert conversation_mode(None) == {"kind": "primary_assistant"}
    assert conversation_mode("g-p-0123456789abcdef0123456789abcdef") == {
        "kind": "gizmo_interaction",
        "gizmo_id": "g-p-0123456789abcdef0123456789abcdef",
    }


def test_invalid_project_id_is_rejected():
    with pytest.raises(ValueError, match="invalid ChatGPT Project id"):
        validate_project_mapping("bad", "Bad", "conversation-id", "plus-work")
