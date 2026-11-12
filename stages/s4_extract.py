"""
Stage 4: signal extraction + company snapshot.

Each source goes to a fast model, in parallel, which returns business
signals with a verbatim evidence quote. Code then checks every quote
really appears in that source's text. Signals whose quote can't be
found are discarded, openly. This is how ProspectIQ stays grounded
without a vector database: every fact downstream points to text a
human can find on the source page.
"""
from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from pydantic import BaseModel

from core.config import model_for
from core.db import get_seller_profile
from core.events import emit
from core.models import (
    About,
    CompanySnapshot,
    EventType,
    Run,
    Signal,
    Source,
    SourceCategory,
    SourceType,
    Stage,
)
from core.pipeline import Outcome, StageResult, track
from providers.base import ProviderError, generate_structured

EXTRACT_CHARS = 8_000
MAX_SIGNALS_PER_SOURCE = 4


# ---------------------------------------------------------------------------
# LLM schemas
# ---------------------------------------------------------------------------

class SignalOut(BaseModel):
    category: Literal["overview", "news", "funding", "hiring", "technology", "challenges", "leadership"]
    description: str
    evidence_quote: str
    about_person: bool


class ExtractionOut(BaseModel):
    page_is_about_company: bool
    signals: list[SignalOut]


class SnapshotOut(BaseModel):
    what_they_do: str
    products: list[str]
    customers: list[str]
    size_estimate: str
    business_model: str


EXTRACT_SYSTEM = (
    "You extract business facts from a single web page for B2B sales research. "
    "You only report what the page says. Every evidence_quote is copied character "
    "for character from the page."
)

EXTRACT_PROMPT = """Company: {company}{domain_note}
Prospect: {person}
We sell: {seller} — problems we solve: {problems}

Page title: {title}
Page text:
\"\"\"
{text}
\"\"\"

First decide page_is_about_company: is this page about THIS {company}? Pages
often mention a different organisation, product or everyday phrase with a
similar name. If it isn't about this company, set false and return no signals.

Then extract up to {max_n} concrete business signals about {company} (or {person}) from this page:
events, hiring, funding, expansion, product launches, technology adoption,
stated challenges, or what leaders have said. Prefer signals that relate to
the problems we solve, but report any important development.

Rules:
- evidence_quote: ONE continuous passage copied exactly from the page text,
  10 to 40 words. No "...", no paraphrasing, no merging sentences.
- description: the signal in one plain sentence (max 20 words).
- about_person: true only if the signal is specifically about {person}.
- Skip navigation text, cookie banners, ads and generic marketing claims.
- If the page has nothing concrete about {company}, return an empty list.
"""

SNAPSHOT_PROMPT = """Using only the page excerpts below, describe {company}.

{excerpts}

Fill in:
- what_they_do: one or two plain sentences
- products: main products or services (max 5)
- customers: who they sell to (max 5)
- size_estimate: e.g. employees, revenue or locations, only if stated; else "Not stated"
- business_model: how they make money, one sentence; "Not stated" if unclear
"""


# ---------------------------------------------------------------------------
# The verbatim check
# ---------------------------------------------------------------------------

_TRANSLATE = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                            "\u2013": "-", "\u2014": "-", "\u00a0": " "})


def normalise(text: str) -> str:
    """Make formatting differences irrelevant: quotes, dashes, spacing, case."""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    return re.sub(r"\s+", " ", text).strip()


def quote_in_source(quote: str, source_text: str) -> bool:
    q = normalise(quote).strip(" .\"'")
    if len(q.split()) < 5 or "..." in q or "\u2026" in quote:
        return False
    return q in normalise(source_text)


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def _extract_one(run: Run, source: Source, seller_name: str, problems: str):
    p = run.request.prospect
    return generate_structured(
        EXTRACT_PROMPT.format(
            company=p.company, person=p.name, seller=seller_name, problems=problems,
            domain_note=f" (website: {run.identity.company_domain})"
            if run.identity and run.identity.company_domain else "",
            title=source.title, text=source.text[:EXTRACT_CHARS], max_n=MAX_SIGNALS_PER_SOURCE,
        ),
        ExtractionOut, provider=run.request.provider,
        model=model_for(run.request.provider, Stage.EXTRACT), system=EXTRACT_SYSTEM,
    )


def _snapshot(run: Run) -> CompanySnapshot:
    p = run.request.prospect
    preferred = [s for s in run.sources
                 if s.category is SourceCategory.OVERVIEW or s.source_type is SourceType.COMPANY_SITE]
    picked = (preferred or run.sources)[:4]
    excerpts = "\n\n".join(f"[{s.title}]\n{s.text[:3000]}" for s in picked)
    result = generate_structured(
        SNAPSHOT_PROMPT.format(company=p.company, excerpts=excerpts),
        SnapshotOut, provider=run.request.provider,
        model=model_for(run.request.provider, Stage.EXTRACT),
    )
    track(run, result)
    return CompanySnapshot(**result.data.model_dump())


def run(run: Run) -> StageResult:
    if not run.sources:
        run.signals, run.snapshot = [], CompanySnapshot()
        return StageResult(Outcome.CONTINUE, "No sources to extract from", {"low_signal": True})

    seller = get_seller_profile(run.request.seller_profile_id)
    problems = "; ".join(seller.problems_solved)
    by_id = {s.id: s for s in run.sources}

    kept: list[Signal] = []
    discarded: list[dict] = []
    seen_quotes: set[str] = set()  # the same story syndicated across sites
    duplicates = 0
    failed_sources = 0
    off_topic: set[str] = set()

    # Calls run in parallel threads; events are emitted here, on the main thread
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_extract_one, run, s, seller.name, problems): s.id
                   for s in run.sources}
        done = 0
        for fut in as_completed(futures):
            source = by_id[futures[fut]]
            done += 1
            try:
                result = fut.result()
            except ProviderError:
                failed_sources += 1
                continue
            track(run, result)
            if not result.data.page_is_about_company:
                off_topic.add(source.id)
                continue
            for out in result.data.signals[:MAX_SIGNALS_PER_SOURCE]:
                key = normalise(out.evidence_quote)
                if key in seen_quotes:
                    duplicates += 1
                elif quote_in_source(out.evidence_quote, source.text):
                    seen_quotes.add(key)
                    kept.append(Signal(
                        category=SourceCategory(out.category),
                        description=out.description,
                        evidence_quote=out.evidence_quote.strip(),
                        source_id=source.id,
                        about=About.PERSON if out.about_person else About.COMPANY,
                        published_at=source.published_at,
                        quote_verified=True,
                    ))
                else:
                    discarded.append({"source": source.title, "quote": out.evidence_quote[:120]})
            if done % 5 == 0 or done == len(futures):
                emit(run.id, Stage.EXTRACT, EventType.PROGRESS,
                     f"Read {done} of {len(futures)} sources, {len(kept)} signals so far")

    if off_topic:
        run.sources = [s for s in run.sources if s.id not in off_topic]
        emit(run.id, Stage.EXTRACT, EventType.PROGRESS,
             f"Removed {len(off_topic)} page{'s' if len(off_topic) > 1 else ''} about a different "
             f"organisation or phrase with a similar name")

    if failed_sources == len(by_id):
        raise ProviderError("Signal extraction failed for every source. Check the AI provider in Settings.")

    if discarded:
        emit(run.id, Stage.EXTRACT, EventType.PROGRESS,
             f"Discarded {len(discarded)} signals: evidence quote not found in the source",
             {"discarded": discarded})
    if duplicates:
        emit(run.id, Stage.EXTRACT, EventType.PROGRESS,
             f"Merged {duplicates} duplicate signals repeated across sources")
    if failed_sources:
        emit(run.id, Stage.EXTRACT, EventType.PROGRESS,
             f"Skipped {failed_sources} sources the AI couldn't read")

    run.signals = kept
    run.snapshot = _snapshot(run)
    emit(run.id, Stage.EXTRACT, EventType.PROGRESS,
         f"Company snapshot: {run.snapshot.what_they_do[:160]}")

    if not kept:
        return StageResult(Outcome.CONTINUE, "No verifiable signals found", {"low_signal": True})
    return StageResult(
        Outcome.CONTINUE,
        f"{len(kept)} verified signals from {len(run.sources)} sources"
        + (f", {len(discarded)} discarded" if discarded else ""),
    )
