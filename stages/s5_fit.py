"""
Stage 5: Product–Company Fit Analysis. The core of ProspectIQ.

Question it answers: of everything we know about this company, which
development gives the seller a genuine, explainable reason to reach out?

  1. Safety gate (code): signals about layoffs, lawsuits, deaths and
     similar are never used as a sales hook.
  2. Shortlist (code): rank the rest by freshness and evidence quality,
     keep a varied shortlist so one story can't crowd out the others.
  3. Fit mapping (smart model): for each promising signal,
     signal -> likely pain -> seller capability -> expected outcome,
     rated on anchored 1-5 rubrics for fit and business impact.
  4. Code checks: the matched capability must exist in the seller's
     profile, ratings are clamped, unknown signal ids are dropped.
  5. Scoring (code): S = 0.35 Fit + 0.30 Impact + 0.20 Freshness + 0.15 Evidence.
  6. Verdict and buyer check: strong / moderate / weak, and whether this
     prospect's role actually owns the pain.
"""
from __future__ import annotations

import re
from datetime import date

from typing import Literal

from pydantic import BaseModel

from core.config import (
    FIT_WEIGHTS,
    FRESHNESS_HALF_LIFE_DAYS,
    STRONG_FIT_THRESHOLD,
    WEAK_FIT_THRESHOLD,
    model_for,
)
from core.db import get_seller_profile
from core.events import emit
from core.models import (
    About,
    EventType,
    ExcludedSignal,
    FitAnalysis,
    FitCandidate,
    FitScore,
    FitVerdict,
    Run,
    SellerProfile,
    Signal,
    Source,
    SourceType,
    Stage,
)
from core.pipeline import Outcome, StageResult, track
from providers.base import generate_structured

SHORTLIST_SIZE = 30
MAX_PER_CATEGORY = 7

# ---------------------------------------------------------------------------
# 1. Safety gate
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS = {
    "layoffs or job cuts": r"\blay ?offs?\b|\blaid off\b|job cuts|redundanc|retrench|downsiz",
    "legal trouble": r"\blawsuits?\b|\bsued\b|\bsuing\b|litigation|class action|indict|charged with",
    "investigation or fraud": r"\bfraud\b|investigation|probe into|\bsec charges\b|money laundering",
    "death or illness": r"\bdied\b|\bdeath\b|passed away|\bfuneral\b|terminal illness",
    "financial distress": r"bankrupt|insolven|default(ed)? on|going concern",
    "security incident": r"data breach|cyber ?attack|ransomware|\bhacked\b",
    "scandal or misconduct": r"scandal|misconduct|harassment|resigned amid",
}


def sensitive_reason(signal: Signal) -> str | None:
    text = f"{signal.description} {signal.evidence_quote}".lower()
    for reason, pattern in SENSITIVE_PATTERNS.items():
        if re.search(pattern, text):
            return reason
    return None


# ---------------------------------------------------------------------------
# 2. Scores computed in code
# ---------------------------------------------------------------------------

SOURCE_QUALITY = {
    SourceType.COMPANY_SITE: 90,
    SourceType.PRESS_RELEASE: 85,
    SourceType.INTERVIEW: 85,
    SourceType.JOB_POSTING: 80,
    SourceType.NEWS: 75,
    SourceType.BLOG: 50,
    SourceType.OTHER: 50,
}


def freshness(published: date | None) -> float:
    """100 today, halves every FRESHNESS_HALF_LIFE_DAYS. Undated sources get 40."""
    if published is None:
        return 40.0
    age = max((date.today() - published).days, 0)
    return round(100 * 0.5 ** (age / FRESHNESS_HALF_LIFE_DAYS), 1)


def evidence_quality(signal: Signal, source: Source | None) -> float:
    base = SOURCE_QUALITY.get(source.source_type, 50) if source else 50
    if signal.about is About.PERSON:
        base += 10  # about the prospect themselves: more personal, more convincing
    return float(min(base, 100))


def total_score(fit: int, impact: int, fresh: float, quality: float) -> float:
    scale = lambda r: (r - 1) / 4 * 100  # 1-5 rubric -> 0-100
    w = FIT_WEIGHTS
    return round(w["fit"] * scale(fit) + w["business_impact"] * scale(impact)
                 + w["freshness"] * fresh + w["evidence_quality"] * quality, 1)


def shortlist(signals: list[Signal], sources: dict[str, Source]) -> list[Signal]:
    ranked = sorted(signals, key=lambda g: freshness(g.published_at)
                    + evidence_quality(g, sources.get(g.source_id)), reverse=True)
    picked, per_cat = [], {}
    for g in ranked:
        if per_cat.get(g.category, 0) >= MAX_PER_CATEGORY:
            continue
        per_cat[g.category] = per_cat.get(g.category, 0) + 1
        picked.append(g)
        if len(picked) == SHORTLIST_SIZE:
            break
    return picked


# ---------------------------------------------------------------------------
# 3. Fit mapping (LLM)
# ---------------------------------------------------------------------------

class AssessmentOut(BaseModel):
    signal_id: str
    likely_pain: str
    matched_capability: str
    expected_outcome: str
    fit: int
    business_impact: int
    thesis: str
    reasoning: str


class FitOut(BaseModel):
    assessments: list[AssessmentOut]
    icp_match: Literal["yes", "partly", "no"]
    icp_note: str
    buyer_owns_pain: bool
    suggested_recipient_role: str | None
    buyer_note: str


SYSTEM = (
    "You are a senior B2B sales strategist. You decide whether a company has a "
    "genuine need for a specific product, based only on the evidence provided. "
    "You are honest when the fit is weak. You never invent product capabilities."
)

PROMPT = """THE SELLER
Product: {seller_name} — {one_liner}
Problems it solves:
{problems}
Capabilities (the ONLY capabilities you may use):
{capabilities}
Best-fit customers: {customers}
Buyer roles: {buyers}

THE TARGET COMPANY: {company}
What they do: {what_they_do}
Products: {products}
Prospect: {person}, {title}

VERIFIED SIGNALS (each backed by a quote from a real source)
{signals}

TASK
Pick the up to 8 signals that give {seller_name} the strongest reason to contact
{company}, and assess each one:
- likely_pain: the operational problem this signal probably creates or reveals for {company}
- matched_capability: copy ONE capability exactly from the list above
- expected_outcome: what that capability would change for them, concretely
- fit (1-5):
    5 = the signal directly creates the exact problem this capability solves
    4 = strong, clear link with one reasonable inference
    3 = plausible but indirect link
    2 = weak link, mostly speculation
    1 = no real connection
- business_impact (1-5):
    5 = large, company-wide and time-sensitive (e.g. rapid expansion, major launch)
    3 = meaningful for one team or function
    1 = minor or cosmetic
- thesis: one sentence in this form: "Because {company} is <signal>, they likely
  face <pain>; <capability> helps by <outcome>."
- reasoning: one sentence on why this rating, including any doubt

Then the target-customer check: compare {company}'s size, industry and stage
with the seller's best-fit customers. For size and scale only, you may use
widely known facts about {company} (e.g. that it is a global enterprise).
Answer "no" if {company} is clearly far outside the stated customer range.
- icp_match: "yes", "partly" or "no"
- icp_note: one plain sentence explaining why

Then the buyer check: does {title} at {company} personally own the pains above?
- buyer_owns_pain: true or false
- suggested_recipient_role: if false, the role who owns it (from the buyer roles
  if one fits); otherwise null
- buyer_note: one plain sentence explaining the buyer check

Be strict:
- Judge {company}'s OWN operations. News about products or services {company}
  sells to its customers is not evidence of {company}'s internal pain, unless
  the signal itself says so.
- likely_pain must be a plain statement of a problem the evidence shows or
  strongly implies. If you can only describe it as a hypothetical ("If...",
  "could", "might"), rate fit 2 or lower.
- Generic facts true of almost any company (has a website contact form, a
  LinkedIn page, a careers page, uses email) are weak evidence: fit at most 2.
- A large, mature company is not automatically a good fit. If nothing connects
  well, give low ratings rather than stretching.
"""


def _signal_line(g: Signal, sources: dict[str, Source]) -> str:
    src = sources.get(g.source_id)
    when = g.published_at.isoformat() if g.published_at else "undated"
    kind = src.source_type.value if src else "unknown"
    who = ", about the prospect" if g.about is About.PERSON else ""
    return f'[{g.id}] ({g.category.value}, {when}, {kind}{who}) {g.description} — "{g.evidence_quote}"'


HYPOTHETICAL = re.compile(r"^\s*(if|assuming|should|in case)\b|\b(could potentially|might|may need|would need)\b",
                          re.IGNORECASE)


def is_hypothetical(pain: str) -> bool:
    """A pain the evidence doesn't actually show, only imagines."""
    return bool(HYPOTHETICAL.search(pain))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def match_capability(claimed: str, seller: SellerProfile) -> str | None:
    """Return the profile's own wording if the claimed capability is really in it."""
    c = _norm(claimed)
    for cap in seller.capabilities:
        n = _norm(cap)
        if c == n or (len(c) > 12 and (c in n or n in c)):
            return cap
    return None


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def run(run: Run) -> StageResult:
    p = run.request.prospect
    seller = get_seller_profile(run.request.seller_profile_id)
    sources = {s.id: s for s in run.sources}

    # 1. Safety gate
    excluded, usable = [], []
    for g in run.signals:
        reason = sensitive_reason(g)
        if reason:
            excluded.append(ExcludedSignal(signal_id=g.id, reason=reason))
        else:
            usable.append(g)
    if excluded:
        reasons = sorted({e.reason for e in excluded})
        emit(run.id, Stage.FIT, EventType.PROGRESS,
             f"Set aside {len(excluded)} sensitive signals as hooks ({', '.join(reasons)})")

    if not usable:
        run.fit = FitAnalysis(verdict=FitVerdict.WEAK, excluded=excluded,
                              buyer_note="No usable signals to judge fit.")
        return StageResult(Outcome.CONTINUE,
                           "Weak fit: no usable signals, the email will use a company-level angle")

    # 2. Shortlist
    picked = shortlist(usable, sources)
    by_id = {g.id: g for g in picked}
    emit(run.id, Stage.FIT, EventType.PROGRESS,
         f"Assessing {len(picked)} of {len(usable)} signals against "
         f"{len(seller.capabilities)} {seller.name} capabilities")

    # 3. Fit mapping
    snap = run.snapshot
    result = generate_structured(
        PROMPT.format(
            seller_name=seller.name, one_liner=seller.one_liner,
            problems="\n".join(f"- {x}" for x in seller.problems_solved),
            capabilities="\n".join(f"- {x}" for x in seller.capabilities),
            customers="; ".join(seller.target_customers) or "(not specified)",
            buyers=", ".join(seller.target_buyer_roles) or "(not specified)",
            company=p.company, person=p.name, title=p.title or "role unknown",
            what_they_do=(snap.what_they_do if snap else "") or "(unknown)",
            products=", ".join(snap.products) if snap and snap.products else "(unknown)",
            signals="\n".join(_signal_line(g, sources) for g in picked),
        ),
        FitOut, provider=run.request.provider,
        model=model_for(run.request.provider, Stage.FIT), system=SYSTEM,
    )
    track(run, result)
    out = result.data

    # 4. Code checks + 5. scoring
    ranked: list[FitCandidate] = []
    invented = speculative = 0
    for a in out.assessments:
        g = by_id.get(a.signal_id)
        if g is None:
            continue  # unknown id: the model referenced a signal we never gave it
        fit = min(max(a.fit, 1), 5)
        impact = min(max(a.business_impact, 1), 5)
        capability = match_capability(a.matched_capability, seller)
        reasoning = a.reasoning
        if capability is None:
            invented += 1
            capability = a.matched_capability
            fit = min(fit, 2)
            reasoning = f"Capability not in the {seller.name} profile, fit capped at 2. {reasoning}"
        if is_hypothetical(a.likely_pain) and fit > 2:
            speculative += 1
            fit = 2
            reasoning = f"Pain is hypothetical, not shown by the evidence; fit capped at 2. {reasoning}"
        fresh = freshness(g.published_at)
        quality = evidence_quality(g, sources.get(g.source_id))
        ranked.append(FitCandidate(
            signal_id=g.id, likely_pain=a.likely_pain, matched_capability=capability,
            expected_outcome=a.expected_outcome, thesis=a.thesis, reasoning=reasoning,
            score=FitScore(fit=fit, business_impact=impact, freshness=fresh,
                           evidence_quality=quality,
                           total=total_score(fit, impact, fresh, quality)),
        ))
    ranked.sort(key=lambda c: c.score.total, reverse=True)

    if speculative:
        emit(run.id, Stage.FIT, EventType.PROGRESS,
             f"Capped {speculative} speculative angle{'s' if speculative > 1 else ''}: "
             "the pain was hypothetical, not evidenced")
    if invented:
        emit(run.id, Stage.FIT, EventType.PROGRESS,
             f"Capped {invented} angle{'s' if invented > 1 else ''} that used a capability "
             f"{seller.name} doesn't list")

    # 6. Verdict
    chosen = ranked[0] if ranked else None
    runner_up = None
    if chosen:
        chosen_cat = by_id[chosen.signal_id].category
        # Prefer a runner-up that offers a genuinely different angle
        runner_up = next((c for c in ranked[1:]
                          if by_id[c.signal_id].category != chosen_cat
                          or c.matched_capability != chosen.matched_capability),
                         ranked[1] if len(ranked) > 1 else None)

    if chosen is None or chosen.score.fit <= 2 or chosen.score.total < WEAK_FIT_THRESHOLD:
        verdict = FitVerdict.WEAK
    elif chosen.score.total >= STRONG_FIT_THRESHOLD:
        verdict = FitVerdict.STRONG
    else:
        verdict = FitVerdict.MODERATE

    # Target-customer check: a company outside the seller's market can't be a strong fit
    if out.icp_match != "yes" and verdict is FitVerdict.STRONG:
        verdict = FitVerdict.MODERATE
        emit(run.id, Stage.FIT, EventType.PROGRESS,
             f"Capped at moderate: {'outside' if out.icp_match == 'no' else 'only partly within'} "
             f"{seller.name}'s target customers. {out.icp_note}")
    elif out.icp_match != "yes":
        emit(run.id, Stage.FIT, EventType.PROGRESS, f"Target-customer check ({out.icp_match}): {out.icp_note}")

    run.fit = FitAnalysis(
        verdict=verdict, chosen=chosen, runner_up=runner_up, excluded=excluded,
        buyer_owns_pain=out.buyer_owns_pain,
        suggested_recipient_role=None if out.buyer_owns_pain else out.suggested_recipient_role,
        buyer_note=out.buyer_note, icp_match=out.icp_match, icp_note=out.icp_note,
        ranked=ranked, assessed_count=len(picked),
    )

    # Recommendation: what a good rep would actually do with this prospect
    fa = run.fit
    if verdict is FitVerdict.WEAK and out.icp_match == "no":
        fa.recommendation = "hold"
        fa.recommendation_note = ("Not recommended to send: the fit is weak and "
                                  f"{p.company} is outside {seller.name}'s target customers.")
    elif not out.buyer_owns_pain and out.suggested_recipient_role:
        fa.recommendation = "redirect"
        fa.recommendation_note = f"Better sent to the {out.suggested_recipient_role}."
    if fa.recommendation != "send":
        emit(run.id, Stage.FIT, EventType.PROGRESS, f"Recommendation: {fa.recommendation_note}")

    if not out.buyer_owns_pain:
        emit(run.id, Stage.FIT, EventType.PROGRESS,
             f"Buyer check: {out.buyer_note}"
             + (f" Consider contacting the {out.suggested_recipient_role}."
                if out.suggested_recipient_role else ""))

    if chosen is None:
        return StageResult(Outcome.CONTINUE, "Weak fit: no signal connected to the product")
    label = {FitVerdict.STRONG: "Strong fit", FitVerdict.MODERATE: "Moderate fit",
             FitVerdict.WEAK: "Weak fit"}[verdict]
    return StageResult(Outcome.CONTINUE,
                       f"{label} ({chosen.score.total:.0f}/100): {chosen.likely_pain}")
