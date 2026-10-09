"""Configuration objects for the local HTTP API facades."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chatgpt_api.providers.chatgpt.models import DEFAULT_CHAT_MODEL


@dataclass(frozen=True, slots=True)
class OpenAICompatConfig:
    account: str
    accounts: tuple[str, ...] = ()
    accounts_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str | None = None
    impersonate: str = "safari184"
    agent_prompt_mode: str = "optimized"
    account_strategy: str = "auto"
    model_fallback: str | None = "auto"
    temporary_chat: bool = True
    image_output_dir: Path = Path("outputs/chatgpt-images")
    research_output_dir: Path = Path("outputs/chatgpt-research")
    admin_db_path: Path | None = None
    public_base_url: str | None = None
    web_timeout: float = 5400.0
    chat_concurrency: str | None = None
    upload_concurrency: str | None = None
    image_concurrency: str | None = None
    research_concurrency: str | None = None
    default_model: str = DEFAULT_CHAT_MODEL
