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


def api_base_url_from_env() -> str:
    value = os.environ.get("TW_LLM_API_BASE_URL", "").strip()
    if value:
        return value
    # Windows launchers can inherit an environment older than the saved user setting.
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "TW_LLM_API_BASE_URL")
        return value.strip() if isinstance(value, str) else ""
    except (ImportError, OSError):
        return ""


@dataclass(frozen=True)
class LLMConfig:
    api_base_url: str = field(default_factory=api_base_url_from_env)
    text_endpoint: str = "/chat/completions"
    api_key_env_var: str = "TW_LLM_API_KEY"
    model: str = "Inferact/Qwen3.8-27B-NVFP4"
    retries: int = 3
    temperature: float = 0.0
    reasoning_effort: str | None = "medium"
    timeout_seconds: int = 660
    max_request_chars: int = 200000

    def api_key_from_env(self) -> str:
        api_key = os.environ.get(self.api_key_env_var, "").strip()
        if not api_key:
            from .errors import PipelineError

            raise PipelineError(f"missing required environment variable {self.api_key_env_var}")
        return api_key


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
