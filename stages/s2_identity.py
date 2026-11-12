"""
Stage 2: identity resolution.

Confirms we are about to research the right person at the right company,
before spending money on full research. Four outcomes:

  confirmed      a result links this person to this company -> continue
  unverified     company is real but the person has no public footprint
                 -> continue with company research, flagged
  ambiguous      several different people match, none clearly at this
                 company -> pause, the rep picks
  role_mismatch  recent evidence says they moved to another company
                 -> pause, the rep decides which company to research

The LLM proposes; code checks. Evidence URLs must be real search
results, and an "ambiguous" or "moved" verdict needs real backing,
or it is downgraded.
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from core.config import model_for
from core.db import get_run, save_run
from core.events import emit
from core.models import (
    EventType,
    IdentityCandidate,
    IdentityResult,
    IdentityStatus,
    Run,
    RunStatus,
    Stage,
)
from core.pipeline import Outcome, StageResult, track
from providers.base import generate_structured
from search.tavily_search import SearchHit, SearchQuery, search_many, unique_hits


# ---------------------------------------------------------------------------
# What the LLM fills in (provider-safe: all required, no defaults)
# ---------------------------------------------------------------------------

class CandidateOut(BaseModel):
    name: str
    title: str
    company: str
    evidence_url: str | None
    note: str


class IdentityOut(BaseModel):
    status: Literal["confirmed", "ambiguous", "role_mismatch", "unverified"]
    current_company: str | None
    company_domain: str | None
    candidates: list[CandidateOut]
    found_name: str | None
    found_title: str | None
    explanation: str


SYSTEM = (
    "You verify the identity of B2B sales prospects using only the web search "
    "results provided. You never use outside knowledge and never guess. When "
    "evidence is thin, you say so."
)

PROMPT = """Target prospect
- Name: {name}
- Job title (as given by the sales rep): {title}
- Company (as given by the sales rep): {company}
- Company website (as given): {website}

Search results
{results}

Decide one status:
- "confirmed": at least one result clearly connects this person to this company.
- "role_mismatch": a recent result clearly shows this person now works at a DIFFERENT company. Put that company in current_company.
- "ambiguous": the results show two or more DIFFERENT people with this name, and none of them is clearly at {company}. List each distinct person as a candidate (max 4).
- "unverified": the company is real but no result says anything about this person.

Rules:
- evidence_url must be copied exactly from the URLs above, or null.
- company_domain is the company's own website domain if a result shows it (e.g. "acme.com"), else null.
- For "confirmed", give one candidate: the matched person.
- found_name: the person's full name exactly as the results state it (e.g. a
  LinkedIn display name like "Priya Raman Acme" is really "Priya Raman"), or null.
- found_title: the person's current job title only, without the company name
  (e.g. "Chief Executive Officer"), or null if no result states it.
- explanation: one or two plain sentences a sales rep would understand, naming the evidence.
"""


def _queries(run: Run) -> list[SearchQuery]:
    p = run.request.prospect
    qs = [SearchQuery(query=f'"{p.name}" {p.company}', max_results=6)]
    qs.append(SearchQuery(query=f'"{p.name}" {p.title}' if p.title else f'"{p.name}"',
                          max_results=6))
    if not p.website:
        qs.append(SearchQuery(query=f"{p.company} official website", max_results=3))
    return qs


def _format_results(hits: list[SearchHit]) -> str:
    lines = []
    for i, h in enumerate(hits, start=1):
        lines.append(f"[{i}] {h.title}\nURL: {h.url}\n{h.snippet[:500]}")
    return "\n\n".join(lines) or "(no results)"


def _domain(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    text = url_or_domain if "://" in url_or_domain else "https://" + url_or_domain
    host = urlparse(text).hostname or ""
    return host.removeprefix("www.") or None


def _norm_text(s: str | None) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _same_company(a: str | None, b: str | None) -> bool:
    norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())
    return bool(a and b) and (norm(a) in norm(b) or norm(b) in norm(a))


def run(run: Run) -> StageResult:
    p = run.request.prospect
    results = search_many(_queries(run), provider=run.request.search_provider)
    hits = unique_hits(results)
    cached = all(r.from_cache for r in results)
    emit(run.id, Stage.IDENTITY, EventType.PROGRESS,
         f"Searched {len(results)} queries, {len(hits)} results"
         + (" (from cache)" if cached else ""),
         {"queries": [r.query.query for r in results]})

    model = model_for(run.request.provider, Stage.IDENTITY)
    llm = generate_structured(
        PROMPT.format(name=p.name, title=p.title or "(not given)", company=p.company,
                      website=str(p.website) if p.website else "(not given)",
                      results=_format_results(hits)),
        IdentityOut, provider=run.request.provider, model=model, system=SYSTEM,
    )
    track(run, llm)
    out = llm.data

    # --- Code checks: never trust a URL or verdict the evidence doesn't back
    real_urls = {h.url for h in hits}
    candidates = [
        IdentityCandidate(
            name=c.name, title=c.title, company=c.company,
            evidence_url=c.evidence_url if c.evidence_url in real_urls else None,
            note=c.note,
        )
        for c in out.candidates
    ]
    status = IdentityStatus(out.status)
    notes = []

    if status is IdentityStatus.AMBIGUOUS and len(candidates) < 2:
        status, notes = IdentityStatus.UNVERIFIED, ["fewer than two distinct people found"]
    if status is IdentityStatus.ROLE_MISMATCH and (
        not out.current_company or _same_company(out.current_company, p.company)
    ):
        status, notes = IdentityStatus.CONFIRMED, ["'moved company' not supported by evidence"]
    if status is IdentityStatus.ROLE_MISMATCH and not any(c.evidence_url for c in candidates):
        status, notes = IdentityStatus.UNVERIFIED, ["job change claimed without a source"]

    run.identity = IdentityResult(
        status=status,
        company_domain=_domain(str(p.website)) if p.website else _domain(out.company_domain),
        current_company=out.current_company if status is IdentityStatus.ROLE_MISMATCH else p.company,
        candidates=candidates,
        explanation=out.explanation,
    )
    if notes:
        emit(run.id, Stage.IDENTITY, EventType.PROGRESS,
             f"Adjusted the AI's verdict: {notes[0]}")

    has_source = any(c.evidence_url for c in candidates)

    # Name check: reps often paste LinkedIn display names ("Bhanu Kiran Madscientist").
    # Use the sourced name when the first name matches and the rest differs.
    if status is IdentityStatus.CONFIRMED and out.found_name and has_source:
        typed, found = p.name.strip(), " ".join(out.found_name.split())
        same_first = typed.split()[0].lower() == found.split()[0].lower()
        if same_first and _norm_text(typed) != _norm_text(found):
            p.name = found
            emit(run.id, Stage.IDENTITY, EventType.PROGRESS,
                 f"Updated name: you entered \u201c{typed}\u201d, sources say \u201c{found}\u201d")

    # Title check: the rep's title decides who the email targets, so fix it
    # when a source clearly states a different one
    # "Founder at MadScientist, Inc." -> "Founder", but keep "Head of Data at Scale"
    found_title = (out.found_title or "").strip()
    tail = re.search(r"\s+(at|@)\s+(.+)$", found_title)
    if tail and _same_company(tail.group(2), p.company):
        found_title = found_title[:tail.start()].strip(" ,-|")
    if status is IdentityStatus.CONFIRMED and found_title and has_source and \
            _norm_text(found_title) != _norm_text(p.title):
        old = p.title or "no title"
        p.title = found_title
        emit(run.id, Stage.IDENTITY, EventType.PROGRESS,
             f"Updated job title: you entered \u201c{old}\u201d, sources say \u201c{p.title}\u201d")

    # Use the name as sources spell it ("jensen huang" -> "Jensen Huang")
    if status is IdentityStatus.CONFIRMED and candidates and \
            candidates[0].name.lower() == p.name.lower() and candidates[0].name != p.name:
        p.name = candidates[0].name

    who = f"{p.name}, {p.title}" if p.title else p.name
    if status is IdentityStatus.CONFIRMED:
        return StageResult(Outcome.CONTINUE, f"Confirmed {who} at {p.company}")
    if status is IdentityStatus.UNVERIFIED:
        return StageResult(Outcome.CONTINUE,
                           f"Couldn't verify {p.name} publicly; researching {p.company} as a company")
    if status is IdentityStatus.AMBIGUOUS:
        return StageResult(Outcome.PAUSE,
                           f"Found {len(candidates)} different people named {p.name}. Pick the right one")
    return StageResult(Outcome.PAUSE,
                       f"{p.name} may have moved to {out.current_company}. Choose which company to research")


# ---------------------------------------------------------------------------
# The rep's decisions on a paused run
# ---------------------------------------------------------------------------

def _resume(run: Run, message: str) -> None:
    run.identity.status = IdentityStatus.CONFIRMED
    run.current_stage = Stage.RESEARCH
    run.status = RunStatus.CREATED
    save_run(run)
    emit(run.id, Stage.IDENTITY, EventType.COMPLETED, message)


def choose_candidate(run_id: str, index: int) -> None:
    run = get_run(run_id)
    c = run.identity.candidates[index]
    p = run.request.prospect
    if c.company and not _same_company(c.company, p.company):
        p.company, p.website = c.company, None
        run.identity.company_domain = None
    if c.title:
        p.title = c.title
    _resume(run, f"You confirmed {c.name}" + (f", {c.title}" if c.title else "")
            + f" at {p.company}")


def resolve_job_change(run_id: str, use_new_company: bool) -> None:
    run = get_run(run_id)
    p = run.request.prospect
    if use_new_company and run.identity.current_company:
        old = p.company
        p.company, p.website = run.identity.current_company, None
        run.identity.company_domain = None
        _resume(run, f"Researching {p.company} (moved from {old})")
    else:
        run.identity.current_company = p.company
        _resume(run, f"Kept {p.company} as you entered it")


def reject_identity(run_id: str) -> None:
    run = get_run(run_id)
    run.status = RunStatus.REJECTED
    run.rejection_reason = "Prospect identity could not be confirmed"
    save_run(run)
    emit(run.id, Stage.IDENTITY, EventType.FAILED,
         "Stopped: none of the matches was the right person")
