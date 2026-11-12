"""Gemini adapter: google-genai SDK with JSON-schema constrained output."""
from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

from core.models import Provider
from providers.base import LLMProvider, ProviderError, RawResponse


class GeminiProvider(LLMProvider):
    name = Provider.GEMINI

    def __init__(self) -> None:
        self.client = genai.Client()  # reads GEMINI_API_KEY from the environment

    def call(self, prompt: str, schema: type[BaseModel], model: str,
             system: str, max_tokens: int) -> RawResponse:
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            max_output_tokens=max_tokens,
        )
        response = self.client.models.generate_content(
            model=model, contents=prompt, config=config
        )
        candidate = response.candidates[0] if response.candidates else None
        finish = str(getattr(candidate, "finish_reason", "") or "")
        if "MAX_TOKENS" in finish:
            raise ProviderError(
                "Google (Gemini): response was cut off at max_tokens. "
                "Try a larger max_tokens or a shorter prompt."
            )
        usage = response.usage_metadata
        return RawResponse(
            parsed=None,  # validated in base.py, same as the other providers' fallback
            text=response.text or "",
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_out=(usage.candidates_token_count or 0) if usage else 0,
        )
