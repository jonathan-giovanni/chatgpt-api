import pytest
from curl_cffi import Curl
from curl_cffi.requests.impersonate import BrowserType

from chatgpt_api.api.config import OpenAICompatConfig
from chatgpt_api.api.openai_compat import _impersonate_for_capture
from chatgpt_api.cli import build_parser
from chatgpt_api.providers.chatgpt.auth import ChatGPTAuthConfig
from chatgpt_api.providers.chatgpt.request_capture import CapturedRequest
from chatgpt_api.providers.chatgpt.transport import ChatGPTWebTransport, normalize_impersonate_profile


@pytest.mark.parametrize("profile,expected", [
    ("safari18_4", "safari184"),
    ("safari18_4_ios", "safari184_ios"),
    ("safari18_0", "safari180"),
    ("safari18_0_ios", "safari180_ios"),
    ("chrome", "chrome"),
    ("chrome136", "chrome136"),
])
def test_saved_profile_normalization(profile, expected):
    assert normalize_impersonate_profile(profile) == expected
    transport = ChatGPTWebTransport(ChatGPTAuthConfig(), impersonate=profile)
    assert transport.impersonate == expected


def test_default_safari_profile_is_canonical_and_supported_without_network():
    transport = ChatGPTWebTransport(ChatGPTAuthConfig())
    assert transport.impersonate == OpenAICompatConfig(account="example").impersonate == "safari184"
    assert BrowserType(transport.impersonate).value == "safari184"
    handle = Curl()
    try:
        assert handle.impersonate(transport.impersonate) == 0
    finally:
        handle.close()


@pytest.mark.parametrize("configured", [None, "auto", "default", "safari184", "safari18_4"])
def test_default_profile_still_detects_chrome_captures(configured):
    capture = CapturedRequest(headers={"user-agent": "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"})
    assert _impersonate_for_capture(configured, capture) == "chrome"


@pytest.mark.parametrize("configured", [None, "auto", "default", "safari18_4"])
def test_missing_user_agent_falls_back_to_supported_safari(configured):
    assert _impersonate_for_capture(configured, CapturedRequest()) == "safari184"


def test_explicit_profiles_and_firefox_capture_are_preserved():
    capture = CapturedRequest(headers={"user-agent": "Firefox/135.0"})
    assert _impersonate_for_capture("safari184", capture) == "firefox135"
    assert _impersonate_for_capture("chrome136", capture) == "chrome136"


@pytest.mark.parametrize("command", [
    ["account-models"], ["account-check"], ["account-limits"],
    ["chat", "--message", "hello"], ["image", "--prompt", "image"],
    ["vision", "--input-image", "example.png"],
])
def test_cli_default_and_legacy_flag_use_canonical_profile(command):
    parser = build_parser()
    assert parser.parse_args(command).impersonate == "safari184"
    assert parser.parse_args(command + ["--impersonate", "safari18_4"]).impersonate == "safari184"


def test_server_cli_normalizes_legacy_environment_setting(monkeypatch):
    monkeypatch.setenv("CHATGPT_IMPERSONATE", "safari18_4")
    args = build_parser().parse_args(["server", "start", "--account", "example"])
    assert args.impersonate == "safari184"
