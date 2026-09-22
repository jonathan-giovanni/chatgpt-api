from scripts.sanitize_chatgpt_capture import sanitize, sanitized_url


def test_sanitize_preserves_protocol_shape_and_redacts_private_values() -> None:
    payload = {
        "action": "next",
        "model": "gpt-observed-model",
        "conversation_mode": {
            "kind": "gizmo_interaction",
            "gizmo_id": "g-p-private-project-id",
        },
        "messages": [
            {
                "id": "private-message-id",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["private prompt"]},
            }
        ],
    }

    result = sanitize(payload)

    assert result["action"] == "next"
    assert result["model"] == "gpt-observed-model"
    assert result["conversation_mode"] == {
        "kind": "gizmo_interaction",
        "gizmo_id": "<redacted-id>",
    }
    assert result["messages"][0]["id"] == "<redacted-id>"
    assert result["messages"][0]["author"]["role"] == "<redacted-string>"
    assert result["messages"][0]["content"]["parts"] == ["<redacted-string>"]


def test_sanitized_url_removes_ids_and_query_values() -> None:
    url = (
        "https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef/project/"
        "12345678-1234-1234-1234-123456789abc?model=secret&temporary-chat=true"
    )

    result = sanitized_url(url)

    assert result == "https://chatgpt.com/g/<id>/project/<id>?<model,temporary-chat>"
    assert "secret" not in result
    assert "0123456789abcdef" not in result
