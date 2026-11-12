"""Anthropic adapter: Messages API with native structured output."""
from __future__ import annotations

from anthropic import Anthropic
from pydantic import BaseModel

from core.models import Provider
from providers.base import LLMProvider, ProviderError, RawResponse


class AnthropicProvider(LLMProvider):
    name = Provider.ANTHROPIC

    def __init__(self) -> None:
        self.client = Anthropic(timeout=90)  # reads ANTHROPIC_API_KEY from the environment

    def call(self, prompt: str, schema: type[BaseModel], model: str,
             system: str, max_tokens: int) -> RawResponse:
        kwargs = {}
        if system:
            kwargs["system"] = system
        response = self.client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            **kwargs,
        )
        if response.stop_reason == "max_tokens":
            raise ProviderError(
                "Anthropic (Claude): response was cut off at max_tokens. "
                "Try a larger max_tokens or a shorter prompt."
            )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return RawResponse(
            parsed=response.parsed_output,
            text=text,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
        )
