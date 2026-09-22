import base64
import json

from chatgpt_api.providers.chatgpt.account_info import (
    decode_authorization_claims,
    detect_account_info,
    infer_account_capabilities,
    load_settings_file,
    mask_email,
    parse_json_cookie,
)
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest


def test_decode_authorization_claims():
    payload = {
        "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"},
        "https://api.openai.com/profile": {"email": "suphot@example.com"},
        "exp": 1783105215,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    claims = decode_authorization_claims(f"Bearer header.{encoded}.sig")

    assert claims["https://api.openai.com/auth"]["chatgpt_plan_type"] == "pro"


def test_parse_json_cookie_decodes_url_encoding():
    parsed = parse_json_cookie("%7B%22model%22%3A%22gpt-5-5-thinking%22%2C%22effort%22%3A%22extended%22%7D")

    assert parsed == {"model": "gpt-5-5-thinking", "effort": "extended"}


def test_detect_account_info_from_capture():
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": "pro",
            "chatgpt_account_id": "8b36123c-706e-4621-ac6b-69bcaa16998c",
        },
        "https://api.openai.com/profile": {"email": "pro@example.com"},
    }
    token_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    capture = CapturedRequest(
        headers={"authorization": f"Bearer header.{token_payload}.sig"},
        cookies={
            "oai-last-model-config": "%7B%22model%22%3A%22gpt-5-5-thinking%22%2C%22effort%22%3A%22extended%22%7D",
            "oai-default-model-config": "%7B%22juices%22%3A%7B%22pro%22%3A%22extended%22%7D%7D",
        },
        request_json={"action": "next", "model": "gpt-5-5-thinking", "thinking_effort": "extended"},
    )

    info = detect_account_info(capture).to_redacted_dict()

    assert info["plan_type"] == "pro"
    assert info["plan_bucket"] == "paid"
    assert info["email"] == "p***o@example.com"
    assert info["observed_models"] == ["gpt-5-5-thinking"]
    assert info["observed_efforts"] == ["extended"]
    assert info["observed_actions"] == ["next"]


def test_detect_account_info_from_settings():
    capture = CapturedRequest(request_json={"action": "next", "model": "gpt-5-5"})
    settings = {
        "settings": {
            "default_model_config": {"default_model_slug": None, "juices": {"pro": "extended"}},
            "last_used_model_config": {
                "juices": {
                    "web": {"gpt-5-5-pro": "extended", "gpt-5-5-thinking": "max"},
                    "default": {"gpt-5-5-thinking": "max"},
                },
                "slugs": {"web": "gpt-5-5", "default": "gpt-5-5"},
            },
            "wingman_thinking_effort": "instant",
        },
        "available_options": {"backend_reasoning_effort": ["instant", "medium", "high"]},
    }

    info = detect_account_info(capture, settings).to_redacted_dict()

    assert info["settings_last_used_slugs"] == {"web": "gpt-5-5", "default": "gpt-5-5"}
    assert info["settings_last_used_juices"]["web"]["gpt-5-5-thinking"] == "max"
    assert info["settings_available_reasoning_efforts"] == ["instant", "medium", "high"]
    assert info["observed_models"] == ["gpt-5-5", "gpt-5-5-pro", "gpt-5-5-thinking"]
    assert info["observed_efforts"] == ["extended", "max", "instant", "medium", "high"]


def test_infer_account_capabilities_keeps_primary_models_separate():
    capture = CapturedRequest(request_json={"action": "next", "model": "gpt-5-5"})
    settings = {
        "settings": {
            "last_used_model_config": {
                "juices": {"web": {"gpt-5-5-pro": "extended", "gpt-5-5-thinking": "max"}},
                "slugs": {"web": "gpt-5-5"},
            },
        },
        "available_options": {"backend_reasoning_effort": ["instant", "medium", "high"]},
    }
    info = detect_account_info(capture, settings)
    info.plan_type = "pro"
    info.plan_bucket = "paid"

    capabilities = infer_account_capabilities(info)

    assert capabilities["supported_models"] == ["gpt-5-5", "gpt-5-5-thinking", "gpt-5-5-pro"]
    assert capabilities["default_model"] == "gpt-5-5"
    assert capabilities["thinking_model"] == "gpt-5-5-thinking"
    assert capabilities["thinking_efforts"] == ["standard", "extended", "max"]
    assert capabilities["pro_model"] == "gpt-5-5-pro"
    assert capabilities["pro_efforts"] == ["standard", "extended"]
    assert capabilities["backend_reasoning_efforts"] == ["instant", "medium", "high"]
    assert capabilities["extra_observed_models"] == []


def test_infer_account_capabilities_keeps_go_auto_only():
    info = detect_account_info(
        CapturedRequest(
            request_json={"action": "next", "model": "gpt-5-5"},
            cookies={
                "oai-last-model-config": "%7B%22model%22%3A%22gpt-5-5-thinking%22%2C%22effort%22%3A%22extended%22%7D"
            },
        )
    )
    info.plan_type = "go"
    info.plan_bucket = "paid"

    capabilities = infer_account_capabilities(info)

    assert capabilities["supported_models"] == []
    assert capabilities["default_model"] is None
    assert capabilities["thinking_model"] is None
    assert capabilities["pro_model"] is None
    assert capabilities["auto_model"] == "auto"
    assert capabilities["auto_only"] is True
    assert "free/go" in capabilities["auto_only_reason"]
    assert capabilities["extra_observed_models"] == ["gpt-5-5", "gpt-5-5-thinking"]


def test_infer_account_capabilities_hides_auto_alias_for_free():
    info = detect_account_info(CapturedRequest(request_json={"action": "next", "model": "auto"}))
    info.plan_type = "free"
    info.plan_bucket = "free"

    capabilities = infer_account_capabilities(info)

    assert capabilities["supported_models"] == []
    assert capabilities["default_model"] is None
    assert capabilities["auto_model"] == "auto"
    assert capabilities["auto_only"] is True
    assert "free/go" in capabilities["auto_only_reason"]
    assert capabilities["extra_observed_models"] == []


def test_infer_account_capabilities_includes_observed_current_model_family():
    info = detect_account_info(
        CapturedRequest(
            request_json={
                "action": "next",
                "model": "gpt-5-6-thinking",
                "thinking_effort": "extended",
            }
        )
    )
    info.plan_type = "plus"
    info.plan_bucket = "paid"

    capabilities = infer_account_capabilities(info)

    assert "gpt-5-6-thinking" in capabilities["supported_models"]
    assert capabilities["model_efforts"]["gpt-5-6-thinking"] == ["extended"]
    assert "gpt-5-6-thinking" not in capabilities["extra_observed_models"]


def test_load_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"settings":{"wingman_thinking_effort":"instant"}}', encoding="utf-8")

    assert load_settings_file(str(path)) == {"settings": {"wingman_thinking_effort": "instant"}}


def test_mask_email():
    assert mask_email("ab@example.com") == "a***@example.com"
    assert mask_email("abc@example.com") == "a***c@example.com"
