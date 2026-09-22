from chatgpt_api.providers.chatgpt.models import (
    build_model_capabilities,
    parse_model_picker,
    presets_for_version,
    resolve_model_alias,
)


def test_parse_model_picker_versions_and_presets():
    picker = parse_model_picker(
        {
            "default_model_slug": "gpt-5-5",
            "model_picker_version": 2,
            "models": [
                {"slug": "gpt-5-5"},
                {"slug": "gpt-5-5-thinking"},
                {"slug": "gpt-5-5-pro"},
            ],
            "versions": [
                {
                    "id": "5.5",
                    "display_text_for_intelligence": "GPT-5.5",
                    "enabled": True,
                    "slugs": ["gpt-5-5", "gpt-5-5-thinking", "gpt-5-5-pro"],
                    "intelligence_presets": [
                        {
                            "title": "Instant",
                            "selected_display_title": "5.5 Instant",
                            "model_slug": "gpt-5-5-instant",
                            "lane": "instant",
                        },
                        {
                            "title": "Medium",
                            "selected_display_title": "5.5 Medium",
                            "model_slug": "gpt-5-5-thinking",
                            "lane": "thinking",
                            "thinking_effort": "standard",
                        },
                        {
                            "title": "Pro",
                            "selected_display_title": "5.5 Pro",
                            "model_slug": "gpt-5-5-pro",
                            "lane": "pro",
                        },
                    ],
                }
            ],
        }
    )

    assert picker.default_model_slug == "gpt-5-5"
    assert picker.model_picker_version == 2
    assert picker.model_slugs == ["gpt-5-5", "gpt-5-5-thinking", "gpt-5-5-pro"]
    presets = presets_for_version(picker, "5.5")
    assert [preset.title for preset in presets] == ["Instant", "Medium", "Pro"]
    assert presets[1].thinking_effort == "standard"
    assert presets[2].model_slug == "gpt-5-5-pro"


def test_build_model_capabilities_uses_observed_current_family() -> None:
    capabilities = build_model_capabilities(
        ["gpt-5-6-thinking", "gpt-5-5-thinking"],
        model_efforts={
            "gpt-5-6-thinking": ["extended"],
            "gpt-5-5-thinking": ["standard"],
        },
        observed_models=["gpt-5-6-thinking"],
    )

    assert [item.id for item in capabilities] == [
        "auto",
        "gpt-5-6-thinking-extended",
        "gpt-5-5-thinking-standard",
    ]
    assert capabilities[1].provider_model == "gpt-5-6-thinking"
    assert capabilities[1].source == "observed"
    assert capabilities[1].status == "active"
    assert capabilities[2].status == "deprecated"
    assert capabilities[2].replacement == "auto"


def test_resolve_model_alias_supports_current_and_legacy_effort_aliases() -> None:
    assert resolve_model_alias("gpt-5-6-thinking-extended", None) == (
        "gpt-5-6-thinking",
        "extended",
    )
    assert resolve_model_alias("gpt-5-5-pro-standard", None) == (
        "gpt-5-5-pro",
        "standard",
    )
    assert resolve_model_alias("custom-model", "high") == ("custom-model", "high")
