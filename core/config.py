"""
ProspectIQ configuration: which model each stage uses, scoring weights,
and other tunable numbers. Change behaviour here, not inside stages.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

from core.models import Provider, Stage

load_dotenv()  # reads .env so API keys are available everywhere

# ---------------------------------------------------------------------------
# Models
# Two tiers per provider: "fast" (cheap, for high-volume steps like
# extracting signals from every source) and "smart" (for the reasoning
# that decides the output: fit analysis, writing, verification).
# Model IDs checked against the SDKs and docs in November 2026.
# ---------------------------------------------------------------------------

MODELS: dict[Provider, dict[str, str]] = {
    Provider.OPENAI: {"fast": "gpt-5.4-mini", "smart": "gpt-5.5"},
    Provider.ANTHROPIC: {"fast": "claude-haiku-5-5", "smart": "claude-sonnet-5-5"},
    Provider.GEMINI: {"fast": "gemini-3.5-flash-lite", "smart": "gemini-3.5-flash"},
}

STAGE_TIER: dict[Stage, str] = {
    Stage.IDENTITY: "fast",
    Stage.EXTRACT: "fast",
    Stage.FIT: "smart",      # the core reasoning step
    Stage.DRAFT: "smart",
    Stage.VERIFY: "smart",
}


def model_for(provider: Provider, stage: Stage) -> str:
    tier = STAGE_TIER.get(stage, "fast")
    return MODELS[provider][tier]


def verifier_for(writer: Provider, available: list[Provider]) -> tuple[Provider, str]:
    """
    Prefer a different provider to check the email than the one that
    wrote it, so the model isn't grading its own work. Falls back to the
    same provider's smart model when only one key is configured.
    """
    others = [p for p in available if p != writer]
    provider = others[0] if others else writer
    return provider, MODELS[provider]["smart"]


# ---------------------------------------------------------------------------
# Prices, USD per 1 million tokens: (input, output)
# Left empty on purpose: copy current numbers from each provider's
# pricing page rather than trusting hard-coded guesses. Until a model
# is listed here, the UI shows token counts and "no price set".
# ---------------------------------------------------------------------------

PRICES: dict[str, tuple[float, float]] = {
    # "gpt-5.4-mini": (0.00, 0.00),
}


def price_for(model: str, tokens_in: int, tokens_out: int) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    return round((tokens_in * p_in + tokens_out * p_out) / 1_000_000, 6)


# ---------------------------------------------------------------------------
# Product–company fit scoring (Stage 5)
# S = 0.35*Fit + 0.30*Business impact + 0.20*Freshness + 0.15*Evidence quality
# ---------------------------------------------------------------------------

FIT_WEIGHTS: dict[str, float] = {
    "fit": 0.35,
    "business_impact": 0.30,
    "freshness": 0.20,
    "evidence_quality": 0.15,
}

FRESHNESS_HALF_LIFE_DAYS = 120   # a signal this old scores 50 on freshness
WEAK_FIT_THRESHOLD = 55          # top score below this -> weak-fit path
STRONG_FIT_THRESHOLD = 75        # at or above this -> strong fit

# ---------------------------------------------------------------------------
# Research and email limits
# ---------------------------------------------------------------------------

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
MAX_RESULTS_PER_QUERY = 5
EMAIL_MAX_WORDS = 120

assert abs(sum(FIT_WEIGHTS.values()) - 1.0) < 1e-9, "FIT_WEIGHTS must add up to 1"
