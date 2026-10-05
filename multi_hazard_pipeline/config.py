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
    model: str = "google/gemma-4-31B-it"
    retries: int = 3
    temperature: float = 0.0
    timeout_seconds: int = 400
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
    schema_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "schema" / "fai_row.schema.json")

    def controlled_labels(self) -> dict[str, list[str]]:
        return {
            "generalized_category": list(self.generalized_categories),
            "interaction_type": list(self.interaction_types),
            "sediment_transport_phase": list(self.sediment_transport_phases),
        }


DEFAULT_CONFIG = PipelineConfig()
