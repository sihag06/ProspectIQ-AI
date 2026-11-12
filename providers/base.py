"""
ProspectIQ LLM provider layer.

Every stage calls ONE function:

    result = generate_structured(prompt, MySchema, provider=..., model=...)
    result.data  -> a validated MySchema instance

It works the same for OpenAI, Anthropic and Gemini. Each provider file
only knows how to talk to its own SDK; validation, retries, error
messages and token tracking all live here, so they behave identically
whichever provider a customer uses.

Rules for schemas passed to generate_structured (the LLM-facing ones):
  - every field required, no default values
  - use `X | None` when a value may be missing (the model sends null)
  - no `dict` fields; use a nested BaseModel instead
  - stick to str, int, float, bool, Literal, list[...] and nested models
These are the rules that all three providers' structured outputs agree on.
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from core.models import Provider

T = TypeVar("T", bound=BaseModel)

# Which environment variable holds each provider's key
PROVIDER_ENV_KEYS: dict[Provider, str] = {
    Provider.OPENAI: "OPENAI_API_KEY",
    Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
    Provider.GEMINI: "GEMINI_API_KEY",
}

PROVIDER_LABELS: dict[Provider, str] = {
    Provider.OPENAI: "OpenAI (GPT)",
    Provider.ANTHROPIC: "Anthropic (Claude)",
    Provider.GEMINI: "Google (Gemini)",
}


# ---------------------------------------------------------------------------
# Results and errors
# ---------------------------------------------------------------------------

@dataclass
class RawResponse:
    """What a provider hands back before validation."""
    parsed: BaseModel | None  # set when the SDK already parsed it
    text: str                 # raw JSON text, used if parsed is None
    tokens_in: int
    tokens_out: int


@dataclass
class LLMResult(Generic[T]):
    data: T
    provider: Provider
    model: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    attempts: int
    cost_usd: float | None   # None when no price is configured for the model


class ProviderError(Exception):
    """A clear, user-facing error. Safe to show in the UI."""


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    name: Provider

    @abstractmethod
    def call(
        self,
        prompt: str,
        schema: type[BaseModel],
        model: str,
        system: str,
        max_tokens: int,
    ) -> RawResponse:
        """Send one request and return the raw structured response."""


def available_providers() -> list[Provider]:
    """Providers whose API key is set. Others are hidden in the UI."""
    return [p for p, key in PROVIDER_ENV_KEYS.items() if os.environ.get(key)]


@lru_cache(maxsize=None)
def get_provider(provider: Provider) -> LLMProvider:
    """Imported lazily, so a missing SDK or key only matters if it's used."""
    if not os.environ.get(PROVIDER_ENV_KEYS[provider]):
        raise ProviderError(
            f"{PROVIDER_LABELS[provider]} is not configured: "
            f"add {PROVIDER_ENV_KEYS[provider]} to your .env file."
        )
    if provider is Provider.OPENAI:
        from providers.openai_provider import OpenAIProvider
        return OpenAIProvider()
    if provider is Provider.ANTHROPIC:
        from providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    if provider is Provider.GEMINI:
        from providers.gemini_provider import GeminiProvider
        return GeminiProvider()
    raise ProviderError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504, 529}


def _status_code(exc: Exception) -> int | None:
    """All three SDKs attach an HTTP status code to their API errors."""
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _friendly(provider: Provider, model: str, exc: Exception) -> str:
    label = PROVIDER_LABELS[provider]
    status = _status_code(exc)
    if status in (401, 403):
        return f"{label}: API key was rejected. Check {PROVIDER_ENV_KEYS[provider]} in .env."
    if status == 404:
        return f"{label}: model '{model}' was not found. Check the model name in core/config.py."
    if status == 429:
        return f"{label}: rate limit or quota reached. Wait a moment or check billing."
    if status and status >= 500:
        return f"{label}: the provider is having problems (HTTP {status}). Try again shortly."
    return f"{label}: {type(exc).__name__}: {exc}"


def _is_transient(exc: Exception) -> bool:
    status = _status_code(exc)
    if status in TRANSIENT_STATUS:
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "connection" in name


# ---------------------------------------------------------------------------
# The one function every stage uses
# ---------------------------------------------------------------------------

def generate_structured(
    prompt: str,
    schema: type[T],
    *,
    provider: Provider,
    model: str,
    system: str = "",
    max_tokens: int = 8000,
    retries: int = 1,
) -> LLMResult[T]:
    """
    Ask a model for output matching `schema` and return it validated.

    Retries `retries` extra times when:
      - the output doesn't match the schema (the error is sent back so
        the model can correct itself), or
      - the provider has a temporary problem (timeout, rate limit, 5xx).
    Anything else raises ProviderError with a readable message.
    """
    from core.config import price_for  # local import avoids a circular import

    client = get_provider(provider)
    attempt_prompt = prompt
    tokens_in = tokens_out = 0
    start = time.perf_counter()
    last_error = ""

    for attempt in range(1, retries + 2):
        try:
            raw = client.call(attempt_prompt, schema, model, system, max_tokens)
        except ValidationError as exc:
            # Some SDKs validate during parsing and raise straight away
            last_error = str(exc)
        except ProviderError:
            raise
        except Exception as exc:
            if _is_transient(exc) and attempt <= retries:
                time.sleep(2 * attempt)
                continue
            raise ProviderError(_friendly(provider, model, exc)) from exc
        else:
            tokens_in += raw.tokens_in
            tokens_out += raw.tokens_out
            try:
                data = raw.parsed if isinstance(raw.parsed, schema) \
                    else schema.model_validate_json(raw.text or "")
                latency = time.perf_counter() - start
                return LLMResult(
                    data=data,
                    provider=provider,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_s=round(latency, 2),
                    attempts=attempt,
                    cost_usd=price_for(model, tokens_in, tokens_out),
                )
            except ValidationError as exc:
                last_error = str(exc)

        # Output didn't match the schema: tell the model what was wrong
        attempt_prompt = (
            f"{prompt}\n\nYour previous answer did not match the required "
            f"format. Validation errors:\n{last_error[:1500]}\n"
            "Return only valid output matching the schema."
        )

    raise ProviderError(
        f"{PROVIDER_LABELS[provider]}: output did not match the expected "
        f"format after {retries + 1} attempts. Last error: {last_error[:300]}"
    )


# ---------------------------------------------------------------------------
# Live test: `python -m providers.base`
# Makes one tiny real request to every provider that has a key.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from typing import Literal

    from core.config import MODELS
    from core.models import Stage
    from core.config import model_for

    class CapitalCity(BaseModel):
        city: str
        country: str
        continent: Literal["Asia", "Europe", "Africa", "Americas", "Oceania"]
        fun_fact: str | None

    ready = available_providers()
    missing = [PROVIDER_LABELS[p] for p in Provider if p not in ready]
    print("Providers with keys:", [PROVIDER_LABELS[p] for p in ready] or "none")
    if missing:
        print("Hidden (no key):   ", missing)
    print()

    for p in ready:
        model = model_for(p, Stage.EXTRACT)  # the cheap "fast" model
        try:
            r = generate_structured(
                "What is the capital of Japan? Include one short fun fact.",
                CapitalCity,
                provider=p,
                model=model,
                system="You are a precise assistant.",
            )
            cost = f"${r.cost_usd:.5f}" if r.cost_usd is not None else "no price set"
            print(f"OK  {PROVIDER_LABELS[p]} · {model}")
            print(f"    {r.data.model_dump()}")
            print(f"    tokens {r.tokens_in} in / {r.tokens_out} out · "
                  f"{r.latency_s}s · attempts {r.attempts} · {cost}")
        except ProviderError as e:
            print(f"ERR {e}")
        print()
