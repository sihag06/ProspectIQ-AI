"""
Stage 6: email generation.

Turns the fit thesis into an email that answers the recipient's three
questions: Why are you contacting me? Why should I care? Why should I respond?

The writer only gets material it is allowed to state: the chosen signal's
verbatim evidence (plus up to two supporting signals), and the seller's
profile. Every factual sentence comes back as a claim tagged with what
backs it, so Stage 7 can check each one.

The email adapts to the situation:
  - evidence about the prospect personally -> it can reference them directly
  - weak fit -> a softer, question-led email, labelled low personalisation
  - the prospect doesn't own the pain -> it asks to be pointed to the right person
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from core.config import EMAIL_MAX_WORDS, model_for
from core.db import get_seller_profile
from core.events import emit
from core.models import (
    About,
    Claim,
    Draft,
    EventType,
    FitCandidate,
    FitVerdict,
    Run,
    Signal,
    Source,
    Stage,
    SupportType,
)
from core.pipeline import Outcome, StageResult, track
from providers.base import generate_structured

BANNED_PHRASES = [
    "i hope this email finds you well", "i hope this finds you well", "hope you're doing well",
    "i came across", "i wanted to reach out", "i'm reaching out", "touch base", "circle back",
    "synergy", "game-changer", "game changer", "revolutionize", "revolutionise",
    "cutting-edge", "best-in-class", "world-class", "low-hanging fruit", "just checking in",
]

PROFILE_FIELDS = ["one_liner", "problems_solved", "capabilities", "differentiators", "proof_points"]

TONE_GUIDE = {
    "professional": "clear, polite and businesslike; complete sentences; no slang",
    "conversational": "warm and plain, like a knowledgeable peer; contractions are fine",
    "direct": "short sentences, gets to the point in the first line, no pleasantries",
}


class ClaimOut(BaseModel):
    text: str
    support_type: Literal["source", "seller_profile"]
    support_ref: str


class DraftOut(BaseModel):
    subject: str
    body: str
    claims: list[ClaimOut]


SYSTEM = (
    "You write short B2B sales emails that a busy executive would actually read. "
    "You are specific, honest and plain-spoken. You state only facts you were "
    "given, and you never invent numbers, customers or familiarity."
)

PROMPT = """WRITE A FIRST-CONTACT SALES EMAIL

From: a sales rep at {seller_name} — {one_liner}
To: {first_name} ({title} at {company})
Tone: {tone} — {tone_guide}

WHY WE'RE WRITING (the fit thesis)
{thesis}
Likely pain: {pain}
Our matching capability: {capability}
Our reasoning about the outcome (NOT a product promise; never state it as
something {seller_name} does or guarantees): {outcome}

EVIDENCE YOU MAY STATE AS FACT (cite by source id)
{evidence}

SELLER FACTS YOU MAY STATE (cite by field name)
{seller_facts}

{situation}

STRUCTURE (one short paragraph each, no headings or bullet points)
1. Opening: ONE specific observation from the primary evidence (the first item).
   Don't stack several figures or sources into one sentence. Answers "why me?"
2. The likely pain, framed as reasoning, not as a known fact about them:
   "At that pace, ... usually ..." rather than "Your issue is ...".
   Answers "why should I care?"
3. How {seller_name} helps, one capability and, if available, one proof point.
4. One light, low-commitment ask that checks interest, e.g. "Worth a conversation?"
   or "Open to comparing notes?". You may mention what you'd offer ({cta}), but
   don't push a meeting length or time slot. This answers "why respond?"
Then sign off with "Best," and "[Your name]" on the next line.

RULES
- Greeting: "Hi {first_name},"
- Body at most {max_words} words including greeting and sign-off.
- Subject: 3 to 7 words, written the way a person titles a note to a colleague:
  about THEIR situation, not our product or process. Sentence case, plain words.
  No internal jargon ("intake", "workflow automation", "solution"), no clickbait,
  no "quick question".
  Good: "Scaling AP after the Series C", "Vendor checks at 12k invoices a month".
  Bad: "Acme demo form intake", "AI automation for your business".
- Only state facts from the two lists above. No other numbers, names or events.
- Say what {seller_name} would do for {company} in your own plain words. Don't
  paste the one-liner, list categories ("for small businesses and..."), or write
  "one of its capabilities is".
- Everything you say about {seller_name} must be in the seller facts. Don't add
  outcomes such as speed, growth or savings unless a proof point states them.
- Exactly one question in the whole email: the ask.
- Aim for 70-100 words. Shorter and specific beats complete.
- A job posting is a signal, not a fact about the team: write "you're hiring a
  ..., which suggests ..." rather than describing the team from the posting.
- Write plainly and concretely. No hedging like "can make X important" or "may potentially".
- Never mention internal labels such as "demo data", "sample", source ids or "seller profile".
- Never write as if you have met, spoken or been introduced.
- Never use: {banned}.
- Never use these claims: {forbidden}.
- Do not copy the evidence word for word; refer to it naturally.

CLAIMS
List every factual statement in the email as a claim:
- about {company} or {first_name}: support_type "source", support_ref = the source id
- about {seller_name}: support_type "seller_profile", support_ref = one of {fields}
Opinions, questions and the ask are not claims.
"""

SITUATION_STRONG = "SITUATION: the fit is clear. Be confident but not pushy."
SITUATION_WEAK = (
    "SITUATION: the fit is uncertain. Do NOT assert that they have the pain. Open with "
    "the observation, then ASK whether the pain applies to them. Keep it humble and short."
)
SITUATION_BUYER = (
    "SITUATION: {first_name} probably doesn't own this problem day to day; the {role} "
    "usually does. Keep the pitch brief. After the ask, add one separate sentence that "
    "is NOT a question, e.g. \"If this sits with your {role}, I'd be grateful for a pointer.\""
)


def _evidence_block(signals: list[Signal], sources: dict[str, Source]) -> str:
    lines = []
    for g in signals:
        src = sources.get(g.source_id)
        when = g.published_at.strftime("%B %Y") if g.published_at else "date unknown"
        lines.append(f'[{g.source_id}] {g.description} ({when}, {src.title if src else "source"})\n'
                     f'   Quote: "{g.evidence_quote}"')
    return "\n".join(lines) or "(none)"


def _seller_facts(seller) -> str:
    out = [f"one_liner: {seller.one_liner}"]
    for field in PROFILE_FIELDS[1:]:
        items = getattr(seller, field)
        if items:
            out.append(f"{field}: " + "; ".join(items))
    return "\n".join(out)


REVISION = """

REVISION
Your previous draft was:
Subject: {subject}
{body}

It failed these checks. Fix every one, and keep whatever was already good:
{problems}
"""


def generate(run: Run, angle: str = "chosen", previous: Draft | None = None,
             problems: list[str] | None = None) -> Draft:
    """
    Write one draft. angle="runner_up" uses the second-best fit (review page).
    With previous + problems, it rewrites that draft to fix the listed problems.
    """
    p = run.request.prospect
    seller = get_seller_profile(run.request.seller_profile_id)
    fit = run.fit
    sources = {s.id: s for s in run.sources}
    signals = {g.id: g for g in run.signals}

    candidate: FitCandidate | None = fit.runner_up if angle == "runner_up" else fit.chosen
    weak = fit.verdict is FitVerdict.WEAK or candidate is None

    # Evidence: the angle's own signal, plus up to two supporting ones from other angles
    evidence: list[Signal] = []
    if candidate and candidate.signal_id in signals:
        evidence.append(signals[candidate.signal_id])
    for c in fit.ranked:
        if len(evidence) >= 3:
            break
        g = signals.get(c.signal_id)
        if g and g not in evidence and c.score.fit >= 3:
            evidence.append(g)
    if not evidence and run.signals:
        evidence = run.signals[:2]

    if weak:
        situation = SITUATION_WEAK
    elif not fit.buyer_owns_pain and fit.suggested_recipient_role:
        situation = SITUATION_BUYER.format(first_name=p.name.split()[0], role=fit.suggested_recipient_role)
    else:
        situation = SITUATION_STRONG

    snap = run.snapshot
    tone = p.tone.value
    prompt = PROMPT.format(
            seller_name=seller.name, one_liner=seller.one_liner,
            first_name=p.name.split()[0], title=p.title or "leader", company=p.company,
            tone=tone, tone_guide=TONE_GUIDE[tone],
            thesis=candidate.thesis if candidate else
            f"{p.company} ({snap.what_they_do if snap else 'unknown business'}) may face "
            f"problems {seller.name} solves; we have no strong evidence yet.",
            pain=candidate.likely_pain if candidate else "unknown — ask, don't assume",
            capability=candidate.matched_capability if candidate else "; ".join(seller.capabilities[:2]),
            outcome=candidate.expected_outcome if candidate else "(not established)",
            evidence=_evidence_block(evidence, sources),
            seller_facts=_seller_facts(seller), situation=situation,
            cta=seller.preferred_cta, max_words=EMAIL_MAX_WORDS,
            banned="; ".join(f'"{b}"' for b in BANNED_PHRASES),
            forbidden="; ".join(seller.forbidden_claims) or "(none listed)",
            fields=", ".join(PROFILE_FIELDS),
        )
    if previous is not None and problems:
        prompt += REVISION.format(subject=previous.subject, body=previous.body,
                                  problems="\n".join(f"- {x}" for x in problems))
    result = generate_structured(
        prompt,
        DraftOut, provider=run.request.provider,
        model=model_for(run.request.provider, Stage.DRAFT), system=SYSTEM,
    )
    track(run, result)
    out = result.data

    if weak:
        personalisation = "low"
    elif candidate and signals.get(candidate.signal_id) and \
            signals[candidate.signal_id].about is About.PERSON:
        personalisation = "person"
    else:
        personalisation = "company"

    return Draft(
        version=len(run.drafts) + 1,
        subject=out.subject.strip(),
        body=out.body.strip(),
        claims=[Claim(text=c.text, support_type=SupportType(c.support_type),
                      support_ref=c.support_ref.strip()) for c in out.claims],
        based_on_signal_id=candidate.signal_id if candidate else None,
        personalisation=personalisation,
        angle=angle,
    )


def run(run: Run) -> StageResult:
    draft = generate(run)
    run.drafts.append(draft)
    labels = {"person": "personal to the prospect", "company": "company-specific",
              "low": "low personalisation"}
    emit(run.id, Stage.DRAFT, EventType.PROGRESS,
         f"Draft {draft.version}: {draft.word_count} words, {len(draft.claims)} factual claims, "
         f"{labels[draft.personalisation]}")
    return StageResult(Outcome.CONTINUE, f"\u201c{draft.subject}\u201d ({draft.word_count} words)")
