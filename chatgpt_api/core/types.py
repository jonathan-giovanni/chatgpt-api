"""Provider-neutral request and response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]
ContentKind = Literal["text", "image_url", "image_bytes", "file_bytes"]
ChatAction = Literal["next", "continue", "variant"]


@dataclass(slots=True)
class ContentPart:
    """One piece of message content."""

    kind: ContentKind
    text: str | None = None
    url: str | None = None
    data: bytes | None = None
    mime_type: str | None = None
    name: str | None = None

    @classmethod
    def text_part(cls, text: str) -> "ContentPart":
        return cls(kind="text", text=text)

    @classmethod
    def image_url(cls, url: str, mime_type: str | None = None) -> "ContentPart":
        return cls(kind="image_url", url=url, mime_type=mime_type)

    @classmethod
    def image_bytes(
        cls,
        data: bytes,
        mime_type: str = "image/png",
        name: str | None = None,
    ) -> "ContentPart":
        return cls(kind="image_bytes", data=data, mime_type=mime_type, name=name)

    @classmethod
    def file_bytes(cls, data: bytes, mime_type: str, name: str) -> "ContentPart":
        return cls(kind="file_bytes", data=data, mime_type=mime_type, name=name)


@dataclass(slots=True)
class Message:
    role: Role
    content: list[ContentPart]

    @classmethod
    def text(cls, role: Role, text: str) -> "Message":
        return cls(role=role, content=[ContentPart.text_part(text)])


@dataclass(slots=True)
class ChatRequest:
    messages: list[Message]
    model: str | None = None
    conversation_id: str | None = None
    parent_message_id: str | None = None
    action: ChatAction = "next"
    variant_purpose: str | None = None
    thinking_effort: str | None = None
    stream: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatDelta:
    text: str = ""
    role: Role | None = None
    conversation_id: str | None = None
    raw: Any = None
    done: bool = False


@dataclass(slots=True)
class ChatResponse:
    text: str
    conversation_id: str | None = None
    raw: Any = None


@dataclass(slots=True)
class ImageRequest:
    prompt: str
    image: bytes | None = None
    image_mime_type: str = "image/png"
    input_images: list["ImageInput"] = field(default_factory=list)
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageInput:
    data: bytes
    mime_type: str = "image/png"
    name: str | None = None


@dataclass(slots=True)
class ImageAsset:
    data: bytes | None = None
    url: str | None = None
    mime_type: str | None = None
    raw: Any = None


@dataclass(slots=True)
class ImageResponse:
    images: list[ImageAsset]
    prompt: str
    raw: Any = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    chat: bool = False
    streaming: bool = False
    image_generation: bool = False
    image_edit: bool = False
    vision: bool = False
