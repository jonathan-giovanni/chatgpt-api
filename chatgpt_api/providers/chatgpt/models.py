"""ChatGPT model picker metadata helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


EFFORT_SUFFIX = re.compile(
    r"^(?P<model>gpt-[a-z0-9-]+-(?:thinking|pro))-(?P<effort>standard|extended|max)$"
)
VERSION_PART = re.compile(r"^gpt-(?P<major>\d+)-(?P<minor>\d+)(?P<suffix>.*)$")
LEGACY_MODEL_PREFIXES = ("gpt-5-5",)


@dataclass(slots=True)
class IntelligencePreset:
    title: str
    selected_display_title: str
    model_slug: str
    lane: str
    thinking_effort: str | None = None
    preset_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "selected_display_title": self.selected_display_title,
            "model_slug": self.model_slug,
            "lane": self.lane,
            "thinking_effort": self.thinking_effort,
            "preset_type": self.preset_type,
        }


@dataclass(slots=True)
class ModelVersion:
    id: str
    display_text: str
    enabled: bool
    slugs: list[str] = field(default_factory=list)
    presets: list[IntelligencePreset] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_text": self.display_text,
            "enabled": self.enabled,
            "slugs": self.slugs,
            "presets": [preset.to_dict() for preset in self.presets],
        }


@dataclass(slots=True)
class ModelPicker:
    default_model_slug: str | None
    model_picker_version: int | None
    model_slugs: list[str] = field(default_factory=list)
    versions: list[ModelVersion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_model_slug": self.default_model_slug,
            "model_picker_version": self.model_picker_version,
            "model_slugs": self.model_slugs,
            "versions": [version.to_dict() for version in self.versions],
        }


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """Normalized public model entry backed by capture-derived evidence."""

    id: str
    provider_model: str
    name: str
    mode: str
    source: str
    thinking_effort: str | None = None
    status: str = "active"
    replacement: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_model": self.provider_model,
            "name": self.name,
            "mode": self.mode,
            "source": self.source,
            "thinking_effort": self.thinking_effort,
            "status": self.status,
            "replacement": self.replacement,
        }


def parse_model_picker(payload: dict[str, Any]) -> ModelPicker:
    models = payload.get("models")
    versions = payload.get("versions")
    return ModelPicker(
        default_model_slug=_str_or_none(payload.get("default_model_slug")),
        model_picker_version=_int_or_none(payload.get("model_picker_version")),
        model_slugs=_model_slugs(models if isinstance(models, list) else []),
        versions=_versions_from_payload(versions if isinstance(versions, list) else []),
    )


def presets_for_version(picker: ModelPicker, version_id: str) -> list[IntelligencePreset]:
    for version in picker.versions:
        if version.id == version_id:
            return version.presets
    return []


def build_model_capabilities(
    model_slugs: list[str],
    *,
    model_efforts: dict[str, list[str]] | None = None,
    observed_models: list[str] | None = None,
) -> list[ModelCapability]:
    """Build public aliases from models observed or conservatively supported."""

    efforts = model_efforts or {}
    observed = set(observed_models or [])
    result = [
        ModelCapability(
            id="auto",
            provider_model="auto",
            name="ChatGPT Auto",
            mode="auto",
            source="bridge",
        )
    ]
    seen = {"auto"}
    for slug in model_slugs:
        if not isinstance(slug, str) or not slug or slug == "auto":
            continue
        variants = _model_variants(slug, efforts.get(slug, []))
        for public_id, effort in variants:
            if public_id in seen:
                continue
            seen.add(public_id)
            deprecated = slug.startswith(LEGACY_MODEL_PREFIXES)
            result.append(
                ModelCapability(
                    id=public_id,
                    provider_model=slug,
                    name=_model_display_name(slug, effort),
                    mode=_model_mode(slug),
                    source="observed" if slug in observed else "compatibility",
                    thinking_effort=effort,
                    status="deprecated" if deprecated else "active",
                    replacement="auto" if deprecated else None,
                )
            )
    return result


def resolve_model_alias(model: str, explicit_effort: str | None) -> tuple[str, str | None]:
    """Resolve a public effort alias without pinning the current model family."""

    if model == "auto":
        return "auto", None
    matched = EFFORT_SUFFIX.fullmatch(model)
    if matched:
        return matched.group("model"), matched.group("effort")
    return model, explicit_effort


def _model_variants(slug: str, efforts: list[str]) -> list[tuple[str, str | None]]:
    normalized_efforts = [effort for effort in efforts if effort in {"standard", "extended", "max"}]
    if _model_mode(slug) in {"thinking", "pro"} and normalized_efforts:
        return [(f"{slug}-{effort}", effort) for effort in dict.fromkeys(normalized_efforts)]
    return [(slug, None)]


def _model_mode(slug: str) -> str:
    for mode in ("instant", "thinking", "pro"):
        if slug.endswith(f"-{mode}"):
            return mode
    return "standard"


def _model_display_name(slug: str, effort: str | None) -> str:
    matched = VERSION_PART.match(slug)
    if not matched:
        base = slug
    else:
        base = f"GPT-{matched.group('major')}.{matched.group('minor')}"
    mode = _model_mode(slug)
    labels = {
        "instant": "Instant",
        "thinking": "Thinking",
        "pro": "Pro",
    }
    parts = [base]
    if mode in labels:
        parts.append(labels[mode])
    if effort:
        parts.append({"standard": "Medium", "extended": "High", "max": "Extra High"}[effort])
    return " ".join(parts)


def _versions_from_payload(values: list[Any]) -> list[ModelVersion]:
    versions: list[ModelVersion] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        version_id = _str_or_none(value.get("id"))
        if not version_id:
            continue
        version = ModelVersion(
            id=version_id,
            display_text=_str_or_none(value.get("display_text_for_intelligence"))
            or _str_or_none(value.get("display_text"))
            or version_id,
            enabled=bool(value.get("enabled")),
            slugs=[slug for slug in value.get("slugs", []) if isinstance(slug, str)],
            presets=_presets_from_payload(value.get("intelligence_presets")),
        )
        versions.append(version)
    return versions


def _presets_from_payload(values: Any) -> list[IntelligencePreset]:
    if not isinstance(values, list):
        return []
    presets: list[IntelligencePreset] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        title = _str_or_none(value.get("title"))
        model_slug = _str_or_none(value.get("model_slug"))
        lane = _str_or_none(value.get("lane"))
        if not title or not model_slug or not lane:
            continue
        presets.append(
            IntelligencePreset(
                title=title,
                selected_display_title=_str_or_none(value.get("selected_display_title")) or title,
                model_slug=model_slug,
                lane=lane,
                thinking_effort=_str_or_none(value.get("thinking_effort")),
                preset_type=_str_or_none(value.get("preset_type")),
            )
        )
    return presets


def _model_slugs(values: list[Any]) -> list[str]:
    slugs: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        slug = _str_or_none(value.get("slug"))
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
