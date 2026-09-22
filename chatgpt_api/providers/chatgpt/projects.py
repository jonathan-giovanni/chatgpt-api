"""Local ChatGPT Project aliases and observed conversation-mode payloads."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


PROJECT_ID = re.compile(r"^g-p-[A-Za-z0-9_-]{16,128}$")
_ACTIVE_PROJECT_ID: ContextVar[str | None] = ContextVar("chatgpt_project_id", default=None)


@dataclass(frozen=True, slots=True)
class ProjectMapping:
    alias: str
    name: str
    project_id: str
    account: str
    state: str = "configured"
    last_verified_at: str | None = None

    def conversation_mode(self) -> dict[str, str]:
        return {"kind": "gizmo_interaction", "gizmo_id": self.project_id}

    def public_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "name": self.name,
            "account": self.account,
            "state": self.state,
            "last_verified_at": self.last_verified_at,
            "project_id": mask_project_id(self.project_id),
        }


def normalize_project_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip())
    ascii_value = "".join(character for character in normalized if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")


def validate_project_mapping(alias: str, name: str, project_id: str, account: str) -> ProjectMapping:
    normalized_alias = normalize_project_alias(alias or name)
    if not normalized_alias:
        raise ValueError("project alias or name is required")
    display_name = name.strip()
    if not display_name:
        raise ValueError("project name is required")
    if not PROJECT_ID.fullmatch(project_id.strip()):
        raise ValueError("invalid ChatGPT Project id")
    normalized_account = account.strip()
    if not normalized_account:
        raise ValueError("project account is required")
    return ProjectMapping(
        alias=normalized_alias,
        name=display_name,
        project_id=project_id.strip(),
        account=normalized_account,
    )


def mask_project_id(project_id: str) -> str:
    if len(project_id) <= 12:
        return "<redacted>"
    return f"{project_id[:6]}…{project_id[-4:]}"


def conversation_mode(project_id: str | None) -> dict[str, str]:
    if project_id:
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("invalid ChatGPT Project id")
        return {"kind": "gizmo_interaction", "gizmo_id": project_id}
    return {"kind": "primary_assistant"}


def current_project_id() -> str | None:
    return _ACTIVE_PROJECT_ID.get()


@contextmanager
def project_context(project_id: str | None) -> Iterator[None]:
    token = _ACTIVE_PROJECT_ID.set(project_id)
    try:
        yield
    finally:
        _ACTIVE_PROJECT_ID.reset(token)
