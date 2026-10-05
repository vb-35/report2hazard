from dataclasses import replace

import pytest

from multi_hazard_pipeline.config import DEFAULT_CONFIG
from multi_hazard_pipeline.errors import PipelineError
from multi_hazard_pipeline.llm import ChatClient


def test_complete_request_limit_includes_prompt_payload_and_schema():
    client = ChatClient("test-only", replace(DEFAULT_CONFIG.llm, max_request_chars=100))
    # The suite blocks all network calls; this must fail before opening a URL.
    with pytest.raises(PipelineError, match="max_request_chars"):
        client._post(system_prompt="x" * 100, user_payload={}, response_schema={"schema": {}})
