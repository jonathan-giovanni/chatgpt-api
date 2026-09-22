from chatgpt_api.api.admin_store import BridgeAdminStore
from chatgpt_api.providers.chatgpt.projects import ProjectMapping


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
