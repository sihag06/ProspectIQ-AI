"""OpenAI adapter: Responses API with native structured output."""
from __future__ import annotations

from openai import OpenAI
from pydantic import BaseModel

from core.models import Provider
from providers.base import LLMProvider, ProviderError, RawResponse


class OpenAIProvider(LLMProvider):
    name = Provider.OPENAI

    def __init__(self) -> None:
        self.client = OpenAI(timeout=90)  # reads OPENAI_API_KEY from the environment

    def call(self, prompt: str, schema: type[BaseModel], model: str,
             system: str, max_tokens: int) -> RawResponse:
        response = self.client.responses.parse(
            model=model,
            instructions=system or None,
            input=prompt,
            text_format=schema,
            max_output_tokens=max_tokens,
        )
        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", "unknown")
            raise ProviderError(
                f"OpenAI (GPT): response was cut off ({reason}). "
                "Try a larger max_tokens or a shorter prompt."
            )
        usage = response.usage
        return RawResponse(
            parsed=response.output_parsed,
            text=response.output_text or "",
            tokens_in=usage.input_tokens if usage else 0,
            tokens_out=usage.output_tokens if usage else 0,
        )
