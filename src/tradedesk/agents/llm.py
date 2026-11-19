"""LLM adapter with a configurable provider.

Providers
- "none" (default): no LLM calls; agents run on deterministic logic only.
- "anthropic": official Anthropic Python SDK (`pip install anthropic`). Key from LLM_API_KEY
  (or the SDK's own credential resolution). Uses structured outputs so the reply is schema-valid JSON,
  and server-side refusal fallbacks.

The LLM only returns a small *review* object. It can never produce orders, prices or sizes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class LLMReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment: str = Field(pattern="^(AGREE|DISAGREE|UNCERTAIN)$")
    concerns: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(max_length=600)


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string", "enum": ["AGREE", "DISAGREE", "UNCERTAIN"]},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["assessment", "concerns", "summary"],
    "additionalProperties": False,
}


@dataclass
class LLMResult:
    review: LLMReview
    tokens_in: int | None
    tokens_out: int | None
    model: str


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    provider: str
    model: str

    def review(self, system_prompt: str, report_json: dict, max_tokens: int, timeout_s: float) -> LLMResult: ...


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", api_key: str | None = None):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:  # pragma: no cover - depends on environment
            raise LLMError("anthropic SDK not installed: pip install anthropic") from e
        import anthropic
        self._anthropic = anthropic
        self.model = model or "claude-opus-5-5"
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def review(self, system_prompt: str, report_json: dict, max_tokens: int, timeout_s: float) -> LLMResult:
        a = self._anthropic
        try:
            resp = self._client.with_options(timeout=timeout_s, max_retries=0).beta.messages.create(
                model=self.model,
                max_tokens=max(max_tokens, 1024),
                system=system_prompt,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
                messages=[{"role": "user", "content": "Review this report JSON:\n" + json.dumps(report_json, default=str)}],
            )
        except a.APIStatusError as e:
            raise LLMError(f"LLM API error {e.status_code}") from e
        except a.APIConnectionError as e:
            raise LLMError("LLM connection error") from e
        if resp.stop_reason == "refusal":
            raise LLMError("LLM refused")
        if resp.stop_reason == "max_tokens":
            raise LLMError("LLM output truncated (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError("LLM returned no text")
        try:
            review = LLMReview.model_validate_json(text)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"invalid LLM JSON: {e}") from e
        return LLMResult(review, getattr(resp.usage, "input_tokens", None), getattr(resp.usage, "output_tokens", None),
                         resp.model)


def make_llm(provider: str, model: str = "", api_key: str = "") -> LLMClient | None:
    p = (provider or "none").lower()
    if p == "none":
        return None
    if p == "anthropic":
        return AnthropicClient(model=model or "claude-opus-5-5", api_key=api_key or None)
    raise LLMError(f"unknown LLM provider {provider!r}")
