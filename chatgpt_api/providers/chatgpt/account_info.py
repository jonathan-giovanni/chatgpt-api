"""Account capability hints derived from local ChatGPT captures."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest

AUTH_CLAIMS_KEY = "https://api.openai.com/auth"
PROFILE_CLAIMS_KEY = "https://api.openai.com/profile"
CORE_MODEL = "gpt-5-5"
THINKING_MODEL = "gpt-5-5-thinking"
PRO_MODEL = "gpt-5-5-pro"
MODEL_ALIASES = {"auto"}
PRIMARY_SUPPORTED_MODELS = [CORE_MODEL, THINKING_MODEL, PRO_MODEL]
AUTO_ONLY_PLAN_TYPES = {"free", "go"}
PLAN_TYPES_WITH_EXPLICIT_CORE_MODEL = {"plus", "pro", "team", "enterprise", "edu"}
PLAN_TYPES_WITH_THINKING_MODEL = {"plus", "pro", "team", "enterprise", "edu"}
PLAN_TYPES_WITH_PRO_MODEL = {"pro"}
DEFAULT_PRO_EFFORTS = ["standard", "extended"]
DEFAULT_THINKING_EFFORTS = ["standard", "extended", "max"]
CHAT_MODEL_SLUG = re.compile(r"^gpt-\d")


@dataclass(slots=True)
class ChatGPTAccountInfo:
    plan_type: str | None = None
    plan_bucket: str | None = None
    email: str | None = None
    account_id: str | None = None
    user_id: str | None = None
    token_expires_at: str | None = None
    observed_models: list[str] = field(default_factory=list)
    observed_efforts: list[str] = field(default_factory=list)
    observed_actions: list[str] = field(default_factory=list)
    last_model_config: dict[str, Any] = field(default_factory=dict)
    default_model_config: dict[str, Any] = field(default_factory=dict)
    request_model: str | None = None
    request_thinking_effort: str | None = None
    request_action: str | None = None
    settings_default_model_slug: str | None = None
    settings_default_juices: dict[str, str] = field(default_factory=dict)
    settings_last_used_slugs: dict[str, str] = field(default_factory=dict)
    settings_last_used_juices: dict[str, dict[str, str]] = field(default_factory=dict)
    settings_available_reasoning_efforts: list[str] = field(default_factory=list)
    settings_wingman_thinking_effort: str | None = None

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "plan_type": self.plan_type,
            "plan_bucket": self.plan_bucket,
            "email": mask_email(self.email),
            "account_id": mask_id(self.account_id),
            "user_id": mask_id(self.user_id),
            "token_expires_at": self.token_expires_at,
            "observed_models": self.observed_models,
            "observed_efforts": self.observed_efforts,
            "observed_actions": self.observed_actions,
            "last_model_config": self.last_model_config,
            "default_model_config": self.default_model_config,
            "request_model": self.request_model,
            "request_thinking_effort": self.request_thinking_effort,
            "request_action": self.request_action,
            "settings_default_model_slug": self.settings_default_model_slug,
            "settings_default_juices": self.settings_default_juices,
            "settings_last_used_slugs": self.settings_last_used_slugs,
            "settings_last_used_juices": self.settings_last_used_juices,
            "settings_available_reasoning_efforts": self.settings_available_reasoning_efforts,
            "settings_wingman_thinking_effort": self.settings_wingman_thinking_effort,
            "scope": "capture-derived; not an exhaustive live capability matrix",
        }


def infer_account_capabilities(info: ChatGPTAccountInfo) -> dict[str, Any]:
    """Return conservative, user-facing model choices for this account.

    This intentionally separates the two models this project currently targets
    from extra models merely observed in cookies/settings.
    """

    supported_models: list[str] = []
    plan_type = (info.plan_type or "").lower()
    observed = set(info.observed_models)

    if plan_type in PLAN_TYPES_WITH_EXPLICIT_CORE_MODEL or (not plan_type and CORE_MODEL in observed):
        _append_unique(supported_models, CORE_MODEL)

    if _allows_thinking_model(info):
        _append_unique(supported_models, THINKING_MODEL)

    auto_only_reason = None
    if plan_type in AUTO_ONLY_PLAN_TYPES:
        auto_only_reason = (
            "free/go captures are exposed as ChatGPT Web Auto only; explicit model slugs can be limited, "
            "downgraded, or return empty assistant text in the web flow"
        )

    if CORE_MODEL in observed and plan_type not in AUTO_ONLY_PLAN_TYPES:
        _append_unique(supported_models, CORE_MODEL)
    if THINKING_MODEL in observed and _allows_thinking_model(info):
        _append_unique(supported_models, THINKING_MODEL)
    if _allows_pro_model(info):
        _append_unique(supported_models, PRO_MODEL)

    if plan_type not in AUTO_ONLY_PLAN_TYPES:
        for model in info.observed_models:
            if CHAT_MODEL_SLUG.match(model):
                _append_unique(supported_models, model)

    default_model = _default_supported_model(info, supported_models)
    thinking_efforts = _thinking_efforts(info) if THINKING_MODEL in supported_models else []
    pro_efforts = _pro_efforts(info) if PRO_MODEL in supported_models else []
    model_efforts: dict[str, list[str]] = {}
    for model in supported_models:
        if model == THINKING_MODEL:
            model_efforts[model] = thinking_efforts
        elif model == PRO_MODEL:
            model_efforts[model] = pro_efforts
        elif model.endswith(("-thinking", "-pro")):
            model_efforts[model] = _model_efforts(info, model)

    return {
        "plan_type": info.plan_type,
        "plan_bucket": info.plan_bucket,
        "supported_models": supported_models,
        "default_model": default_model,
        "thinking_model": THINKING_MODEL if THINKING_MODEL in supported_models else None,
        "thinking_efforts": thinking_efforts,
        "pro_model": PRO_MODEL if PRO_MODEL in supported_models else None,
        "pro_efforts": pro_efforts,
        "model_efforts": model_efforts,
        "backend_reasoning_efforts": info.settings_available_reasoning_efforts,
        "auto_model": "auto",
        "auto_only": bool(auto_only_reason),
        "auto_only_reason": auto_only_reason,
        "extra_observed_models": [
            model for model in info.observed_models if model not in supported_models and model not in MODEL_ALIASES
        ],
        "scope": "inferred from capture/settings/plan; confirm with a live chat test when tokens change",
    }


def detect_account_info(capture: CapturedRequest, settings: dict[str, Any] | None = None) -> ChatGPTAccountInfo:
    info = ChatGPTAccountInfo()
    claims = decode_authorization_claims(capture.headers.get("authorization"))
    auth_claims = claims.get(AUTH_CLAIMS_KEY)
    if isinstance(auth_claims, dict):
        info.plan_type = _str_or_none(auth_claims.get("chatgpt_plan_type"))
        info.account_id = _str_or_none(auth_claims.get("chatgpt_account_id"))
        info.user_id = _str_or_none(auth_claims.get("chatgpt_user_id") or auth_claims.get("user_id"))
    profile_claims = claims.get(PROFILE_CLAIMS_KEY)
    if isinstance(profile_claims, dict):
        info.email = _str_or_none(profile_claims.get("email"))
    if isinstance(claims.get("exp"), int):
        info.token_expires_at = datetime.fromtimestamp(claims["exp"], timezone.utc).isoformat()
    info.plan_bucket = plan_bucket(info.plan_type)

    info.last_model_config = parse_json_cookie(capture.cookies.get("oai-last-model-config"))
    info.default_model_config = parse_json_cookie(capture.cookies.get("oai-default-model-config"))

    request_json = capture.request_json or {}
    info.request_model = _str_or_none(request_json.get("model"))
    info.request_thinking_effort = _str_or_none(request_json.get("thinking_effort"))
    info.request_action = _str_or_none(request_json.get("action"))

    models: list[str] = []
    efforts: list[str] = []
    actions: list[str] = []
    _append_unique(models, info.request_model)
    _append_unique(models, _str_or_none(info.last_model_config.get("model")))
    _append_unique(models, _str_or_none(info.default_model_config.get("model")))
    _append_unique(efforts, info.request_thinking_effort)
    _append_unique(efforts, _str_or_none(info.last_model_config.get("effort")))
    juices = info.default_model_config.get("juices")
    if isinstance(juices, dict):
        for value in juices.values():
            _append_unique(efforts, _str_or_none(value))
    _append_unique(actions, info.request_action)

    info.observed_models = models
    info.observed_efforts = efforts
    info.observed_actions = actions
    if settings:
        apply_settings_info(info, settings)
    return info


def apply_settings_info(info: ChatGPTAccountInfo, settings_blob: dict[str, Any]) -> None:
    settings = settings_blob.get("settings")
    if not isinstance(settings, dict):
        return
    default_config = settings.get("default_model_config")
    if isinstance(default_config, dict):
        info.settings_default_model_slug = _str_or_none(default_config.get("default_model_slug"))
        juices = default_config.get("juices")
        if isinstance(juices, dict):
            info.settings_default_juices = {
                str(key): str(value) for key, value in juices.items() if isinstance(value, str)
            }

    last_used = settings.get("last_used_model_config")
    if isinstance(last_used, dict):
        slugs = last_used.get("slugs")
        if isinstance(slugs, dict):
            info.settings_last_used_slugs = {
                str(key): str(value) for key, value in slugs.items() if isinstance(value, str)
            }
        juices = last_used.get("juices")
        if isinstance(juices, dict):
            info.settings_last_used_juices = _nested_string_dict(juices)

    info.settings_wingman_thinking_effort = _str_or_none(settings.get("wingman_thinking_effort"))

    available_options = settings_blob.get("available_options")
    if isinstance(available_options, dict):
        reasoning_efforts = available_options.get("backend_reasoning_effort")
        if isinstance(reasoning_efforts, list):
            info.settings_available_reasoning_efforts = [
                item for item in reasoning_efforts if isinstance(item, str)
            ]

    for model in _models_from_settings(info):
        _append_unique(info.observed_models, model)
    for effort in _efforts_from_settings(info):
        _append_unique(info.observed_efforts, effort)


def decode_authorization_claims(authorization: str | None) -> dict[str, Any]:
    if not authorization:
        return {}
    token = authorization.split(" ", 1)[1] if " " in authorization else authorization
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = _base64url_decode(parts[1])
        parsed = json.loads(payload)
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_json_cookie(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(unquote(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_settings_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as file:
        parsed = json.load(file)
    return parsed if isinstance(parsed, dict) else {}


def plan_bucket(plan_type: str | None) -> str | None:
    if not plan_type:
        return None
    if plan_type == "free":
        return "free"
    return "paid"


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"
    return f"{name[0]}***{name[-1]}@{domain}"


def mask_id(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def _base64url_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _append_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _nested_string_dict(value: dict[Any, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for outer_key, inner_value in value.items():
        if not isinstance(inner_value, dict):
            continue
        result[str(outer_key)] = {
            str(inner_key): str(inner_inner_value)
            for inner_key, inner_inner_value in inner_value.items()
            if isinstance(inner_inner_value, str)
        }
    return result


def _models_from_settings(info: ChatGPTAccountInfo) -> list[str]:
    models: list[str] = []
    _append_unique(models, info.settings_default_model_slug)
    for model in info.settings_last_used_slugs.values():
        _append_unique(models, model)
    for platform in info.settings_last_used_juices.values():
        for model in platform.keys():
            _append_unique(models, model)
    return models


def _efforts_from_settings(info: ChatGPTAccountInfo) -> list[str]:
    efforts: list[str] = []
    for effort in info.settings_default_juices.values():
        _append_unique(efforts, effort)
    for platform in info.settings_last_used_juices.values():
        for effort in platform.values():
            _append_unique(efforts, effort)
    for effort in info.settings_available_reasoning_efforts:
        _append_unique(efforts, effort)
    _append_unique(efforts, info.settings_wingman_thinking_effort)
    return efforts


def _default_supported_model(info: ChatGPTAccountInfo, supported_models: list[str]) -> str | None:
    preferred = [
        info.settings_last_used_slugs.get("web"),
        info.settings_last_used_slugs.get("default"),
        info.request_model,
        _str_or_none(info.last_model_config.get("model")),
    ]
    for model in preferred:
        if model in supported_models:
            return model
    return supported_models[0] if supported_models else None


def _allows_thinking_model(info: ChatGPTAccountInfo) -> bool:
    plan_type = (info.plan_type or "").lower()
    if plan_type in PLAN_TYPES_WITH_THINKING_MODEL:
        return True
    if plan_type in {"free", "go"}:
        return False
    return THINKING_MODEL in info.observed_models


def _allows_pro_model(info: ChatGPTAccountInfo) -> bool:
    plan_type = (info.plan_type or "").lower()
    if plan_type in PLAN_TYPES_WITH_PRO_MODEL:
        return True
    if plan_type in {"free", "go", "plus"}:
        return False
    return PRO_MODEL in info.observed_models


def _model_efforts(info: ChatGPTAccountInfo, model: str) -> list[str]:
    efforts: list[str] = []
    if info.request_model == model:
        _append_unique(efforts, info.request_thinking_effort)
    if info.last_model_config.get("model") == model:
        _append_unique(efforts, _str_or_none(info.last_model_config.get("effort")))
    for platform in info.settings_last_used_juices.values():
        _append_unique(efforts, _str_or_none(platform.get(model)))
    return efforts


def _pro_efforts(info: ChatGPTAccountInfo) -> list[str]:
    efforts: list[str] = []
    for effort in DEFAULT_PRO_EFFORTS:
        _append_unique(efforts, effort)
    for effort in _model_efforts(info, PRO_MODEL):
        _append_unique(efforts, effort)
    return efforts


def _thinking_efforts(info: ChatGPTAccountInfo) -> list[str]:
    efforts: list[str] = []
    for effort in DEFAULT_THINKING_EFFORTS:
        _append_unique(efforts, effort)
    for effort in _model_efforts(info, THINKING_MODEL):
        _append_unique(efforts, effort)
    return efforts
