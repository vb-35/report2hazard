from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# This is the authoritative taxonomy. Schemas and prompts are generated from it.
TAXONOMY: dict[str, dict[str, Any]] = {
    "generalized_category": {
        "values": (
            "Unstable pre-event conditions",
            "Triggering Event",
            "Material Mobilization",
            "Favourable Topography",
            "Natural dam failure",
            "Sediment Surge",
            "Post-event redistribution",
            "Impact on permanent or temporary infrastructure",
            "Positive Impact on permanent or temporary infrastructure",
            "Changes in geomorphology",
            "Negative Impact on permanent or temporary infrastructure",
            "Alteration of channel dynamics",
            "unknown",
            "not applicable",
        ),
        "aliases": {
            "triggering event.": "Triggering Event",
            "material mobilization.": "Material Mobilization",
            "changes in geomorphology.": "Changes in geomorphology",
            "geomorphic changes": "Changes in geomorphology",
            "role of permanent or temporary infrastructure": "Impact on permanent or temporary infrastructure",
        },
    },
    "interaction_type": {
        "values": ("Process-process", "Process-topography", "Process-structure", "Feedback", "unknown", "not applicable"),
        "aliases": {},
    },
    "sediment_transport_phase": {
        "values": ("Erosion", "Transportation", "Deposition", "Dysconnectivity", "unknown", "not applicable"),
        "aliases": {"disconnectivity": "Dysconnectivity", "transport": "Transportation"},
    },
}


# Shared by classification and independent semantic review; IDs anchor disagreements.
TAXONOMY_DECISION_RULES = """
Taxonomy decision rules:
- T1 Evidence: classify the evidenced step, preserving uncertainty. For each field, use "unknown" when it applies but
  evidence cannot select a label; use "not applicable" when it does not apply. Choose a specific label when supported.
  Explain the choice briefly with evidence.
- T2 Infrastructure: classify the effect on sediment connectivity, not social benefit, harm, or physical damage alone.
  Use "Positive Impact on permanent or temporary infrastructure" for evidenced increased sediment passage, release,
  propagation, or dispersion; "Negative Impact on permanent or temporary infrastructure" for evidenced decreased
  sediment passage through retention, trapping, or obstruction; "Impact on permanent or temporary infrastructure"
  when a structure is affected or involved but connectivity direction is unclear. Failure/overtopping is positive only
  with evidence of increased passage; damage alone proves neither direction. Road closure blocks traffic, not necessarily sediment.
- T3 Causal role: "Unstable pre-event conditions" describes antecedent instability or stored material;
  "Triggering Event" the active initiator; "Material Mobilization" active sediment recruitment;
  "Favourable Topography" terrain/channel form amplifying movement; "Natural dam failure" failure of a natural blockage;
  "Sediment Surge" a sediment-heavy downstream surge; "Post-event redistribution" delayed or secondary redistribution;
  "Changes in geomorphology" explicit channel/landform reshaping; "Alteration of channel dynamics" changed flow behavior.
- T4 Interaction precedence: Feedback for reverse/backwater/upstream response; otherwise Process-structure when a
  structure controls or is affected by the process; otherwise Process-process when a natural process supplies, triggers,
  or alters another; otherwise Process-topography when terrain controls the process. Apply T1 if none is evidenced.
- T5 Transport phase: Dysconnectivity for evidenced sediment retention/blockage/interruption; otherwise Erosion for
  removal/recruitment; otherwise Deposition for settling/accumulation; otherwise Transportation for evidenced sediment
  movement. Traffic interruption or infrastructure damage alone is not Dysconnectivity. Pre-event conditions or triggers
  without sediment movement or retention/blockage use "not applicable"; an applicable but undetermined phase uses "unknown".
  Determine phase separately from infrastructure direction; do not infer one field solely from another label.
""".strip()


def setting_from_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    # Windows launchers can inherit an environment older than the saved user setting.
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return value.strip() if isinstance(value, str) else ""
    except (ImportError, OSError):
        return ""


def api_base_url_from_env() -> str:
    return setting_from_env("TW_LLM_API_BASE_URL")


# "openai": any OpenAI-compatible service (the self-hosted router). "anthropic": the Claude API (Messages).
PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {"label": "Self-hosted (OpenAI-compatible)", "model": "nvidia/Qwen3.8-27B-NVFP4",
               "api_key_env_var": "TW_LLM_API_KEY"},
    "anthropic": {"label": "Anthropic API (Claude)", "model": "claude-opus-5-5", "api_key_env_var": "ANTHROPIC_API_KEY"},
}


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def provider_from_env() -> str:
    return setting_from_env("TW_LLM_PROVIDER").lower() or "openai"


@dataclass(frozen=True)
class LLMConfig:
    # Select with TW_LLM_PROVIDER or LLMConfig.with_provider(); see PROVIDERS.
    provider: str = field(default_factory=provider_from_env)
    # OpenAI-compatible provider only; the Anthropic client uses its own endpoint (or ANTHROPIC_BASE_URL).
    api_base_url: str = field(default_factory=api_base_url_from_env)
    text_endpoint: str = "/chat/completions"
    # Empty model / key variable: the provider's default from PROVIDERS.
    api_key_env_var: str = ""
    model: str = ""
    retries: int = 3
    # Ignored by the Anthropic provider: current Claude models reject sampling parameters.
    temperature: float = 0.0
    # Anthropic: sent as output_config.effort (low, medium, high, xhigh, max).
    reasoning_effort: str | None = "medium"
    # The GPU router no longer stops requests at 600 s (nginx fixed 2026-11-04). Placement calls under parallel load
    # reached 575 s, so wait up to 20 minutes before retrying.
    timeout_seconds: int = 1200
    max_request_chars: int = 200000
    # Optional cap on generated tokens (reasoning included). None keeps the server default (Anthropic requires a
    # value, so that provider uses 64000). A cap ends runaway generations that a server keeps running after the
    # client has disconnected.
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown LLM provider {self.provider!r}; choose one of {', '.join(PROVIDERS)}")
        defaults = PROVIDERS[self.provider]
        for name in ("model", "api_key_env_var"):
            if not getattr(self, name):
                object.__setattr__(self, name, defaults[name])

    def with_provider(self, provider: str | None, model: str | None = None) -> "LLMConfig":
        """Switch provider; the model and key variable reset to that provider's defaults unless a model is given."""
        from dataclasses import replace

        provider = (provider or self.provider).strip().lower()
        if provider == self.provider:
            return replace(self, model=model or self.model)
        return replace(self, provider=provider, model=model or "", api_key_env_var="")

    def api_key_from_env(self) -> str:
        api_key = setting_from_env(self.api_key_env_var)
        if not api_key and self.provider != "anthropic":
            from .errors import PipelineError

            raise PipelineError(f"missing required environment variable {self.api_key_env_var}")
        # The Anthropic SDK also finds ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile by itself.
        return api_key


def llm_from_choices(base: LLMConfig, provider: str | None = None, model: str | None = None,
                     effort: str | None = None) -> LLMConfig:
    """Apply interface or command-line choices; empty values keep the base settings."""
    from dataclasses import replace

    from .errors import PipelineError

    provider = (provider or "").strip().lower() or None
    if provider and provider not in PROVIDERS:
        raise PipelineError(f"Unknown model provider {provider!r}; choose one of {', '.join(PROVIDERS)}.")
    llm = base.with_provider(provider, (model or "").strip() or None)
    effort = (effort or "").strip().lower()
    if effort:
        if effort not in EFFORT_LEVELS:
            raise PipelineError(f"Unknown reasoning effort {effort!r}; choose one of {', '.join(EFFORT_LEVELS)}.")
        llm = replace(llm, reasoning_effort=effort)
    return llm


@dataclass(frozen=True)
class PipelineConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    generalized_categories: tuple[str, ...] = TAXONOMY["generalized_category"]["values"]
    interaction_types: tuple[str, ...] = TAXONOMY["interaction_type"]["values"]
    sediment_transport_phases: tuple[str, ...] = TAXONOMY["sediment_transport_phase"]["values"]
    export_columns: tuple[str, ...] = (
        "segment",
        "event",
        "process",
        "generalized_category",
        "interaction_type",
        "sediment_transport_phase",
    )
    batch_max_chars: int = 12000
    min_segment_count_before_pdf_retry: int = 10  # retained for config compatibility
    max_correction_rounds: int = 2
    mainly_english_threshold: float = 0.75
    language_min_confidence: float = 0.80
    language_min_margin: float = 0.20
    schema_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "schema" / "fai_row.schema.json")

    def __post_init__(self) -> None:
        for name in ("mainly_english_threshold", "language_min_confidence", "language_min_margin"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be between 0 and 1")

    def controlled_labels(self) -> dict[str, list[str]]:
        return {
            "generalized_category": list(self.generalized_categories),
            "interaction_type": list(self.interaction_types),
            "sediment_transport_phase": list(self.sediment_transport_phases),
        }


DEFAULT_CONFIG = PipelineConfig()
