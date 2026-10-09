from chatgpt_api.api.admin_store import BridgeAdminStore
from chatgpt_api.providers.chatgpt.projects import ProjectMapping
from contextlib import contextmanager


def test_schema_is_migrated_once_and_reopens_current_data(tmp_path, monkeypatch):
    statements = []
    original = BridgeAdminStore._connect

    @contextmanager
    def connect(store):
        with original(store) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(BridgeAdminStore, "_connect", connect)
    path = tmp_path / "admin.sqlite"
    first = BridgeAdminStore(path)
    first.set_setting("example", "current")
    assert any("CREATE TABLE" in sql for sql in statements)
    statements.clear()
    second = BridgeAdminStore(path)
    assert not any("CREATE TABLE" in sql for sql in statements)
    assert second.get_setting("example") == "current"
    # Replacing a database starts with its own schema version, not process state.
    replacement = BridgeAdminStore(tmp_path / "other.sqlite")
    replacement.set_setting("example", "replacement")
    (tmp_path / "other.sqlite").replace(path)
    assert BridgeAdminStore(path).get_setting("example") == "replacement"


def test_artifacts_hide_missing_files(tmp_path):
    store = BridgeAdminStore(tmp_path / "admin.sqlite")
    live = tmp_path / "live.png"
    live.write_bytes(b"png")
    missing = tmp_path / "missing.png"

    store.record_artifact(
        {
            "id": "live",
            "filename": "live.png",
            "path": str(live),
            "download_url": "http://local/live.png",
            "content_type": "image/png",
            "bytes": live.stat().st_size,
        },
        kind="image",
    )
    store.record_artifact(
        {
            "id": "missing",
            "filename": "missing.png",
            "path": str(missing),
            "download_url": "http://local/missing.png",
            "content_type": "image/png",
            "bytes": None,
        },
        kind="image",
    )

    artifacts = store.list_artifacts()

    assert [artifact["file_id"] for artifact in artifacts] == ["live"]
    assert store.artifact_count() == 1
    assert store.delete_artifact("missing") is None


def test_project_mappings_resolve_name_alias_and_id(tmp_path):
    store = BridgeAdminStore(tmp_path / "admin.sqlite")
    mapping = ProjectMapping(
        alias="support",
        name="Support",
        project_id="g-p-0123456789abcdef0123456789abcdef",
        account="plus-work",
    )

    store.upsert_project(mapping)

    assert store.resolve_project("Support") == mapping
    assert store.resolve_project("support") == mapping
    assert store.resolve_project(mapping.project_id) == mapping
    assert store.list_projects() == [mapping]
    assert store.delete_project("Support") is True
    assert store.resolve_project("support") is None


def test_conversation_sessions_keep_latest_parent_and_original_project(tmp_path):
    store = BridgeAdminStore(tmp_path / "admin.sqlite")
    conversation_id = "11111111-1111-4111-8111-111111111111"

    store.upsert_conversation_session(
        conversation_id=conversation_id,
        parent_message_id="message-1",
        account="plus-work",
        project_id="g-p-0123456789abcdef0123456789abcdef",
    )
    store.upsert_conversation_session(
        conversation_id=conversation_id,
        parent_message_id="message-2",
        account="plus-work",
    )

    session = store.get_conversation_session(conversation_id)
    assert session is not None
    assert session["parent_message_id"] == "message-2"
    assert session["account"] == "plus-work"
    assert session["project_id"] == "g-p-0123456789abcdef0123456789abcdef"
