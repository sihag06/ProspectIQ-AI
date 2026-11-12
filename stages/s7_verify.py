"""
Stage 7: email verification.

Two layers:
  1. Code checks: exact, instant and free. Do the claims point at real
     sources and real profile fields? Length, one ask, no banned or
     internal phrases, no fake familiarity, the right names.
  2. AI verifier: judgments only a model can make. Is each claim actually
     supported by its source? Any fact in the email that isn't listed as a
     claim? Any forbidden product claim? Does it answer the reader's three
     questions? It uses a different provider from the writer when one is
     configured, so the model isn't grading its own work.

A failing draft is rewritten once with the exact problems listed, then
checked again. If it still fails, the run goes to the rep as "Needs review"
with every failing check shown. Nothing is ever sent automatically.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from core.config import EMAIL_MAX_WORDS, verifier_for
from core.db import get_seller_profile
from core.events import emit
from core.models import (
    Draft,
    EventType,
    FitVerdict,
    Run,
    RunStatus,
    SellerProfile,
    Stage,
    SupportType,
    VerificationCheck,
    VerificationReport,
)
from core.pipeline import Outcome, StageResult, track
from providers.base import ProviderError, available_providers, generate_structured
from stages.s6_draft import BANNED_PHRASES, PROFILE_FIELDS, generate

MAX_SUBJECT_WORDS = 8
MIN_QUESTION_SCORE = 3

INTERNAL_LABELS = re.compile(
    r"demo data|sample data|\bsrc_[0-9a-f]+|\bsig_[0-9a-f]+|seller profile|support_ref|\[source",
    re.IGNORECASE)
FIXED_SLOT = re.compile(r"\b\d+\s*-?\s*(min|mins|minute|minutes)\b|\bhalf an hour\b|\ban hour\b",
                        re.IGNORECASE)
FAKE_FAMILIARITY = re.compile(
    r"as we discussed|great (chatting|speaking|meeting|talking)|following up on our|"
    r"nice to meet|per our (conversation|call)|as promised|thanks for your time (on|last)",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Layer 1: code checks
# ---------------------------------------------------------------------------

def code_checks(run: Run, d: Draft, seller: SellerProfile) -> list[VerificationCheck]:
    p = run.request.prospect
    first = p.name.split()[0]
    text = f"{d.subject}\n{d.body}"
    lower = text.lower()
    source_ids = {s.id for s in run.sources}
    checks = []

    bad_refs = []
    for c in d.claims:
        if c.support_type is SupportType.SOURCE and c.support_ref not in source_ids:
            bad_refs.append(c.text)
        if c.support_type is SupportType.SELLER_PROFILE and (
                c.support_ref not in PROFILE_FIELDS or not getattr(seller, c.support_ref, None)):
            bad_refs.append(c.text)
    checks.append(VerificationCheck(
        name="Every claim points to real evidence", passed=not bad_refs,
        detail="" if not bad_refs else "No valid source for: " + "; ".join(bad_refs[:3])))

    words, subject_words = d.word_count, len(d.subject.split())
    checks.append(VerificationCheck(
        name="Length", passed=words <= EMAIL_MAX_WORDS and subject_words <= MAX_SUBJECT_WORDS,
        detail=f"{words} words (limit {EMAIL_MAX_WORDS}), subject {subject_words} words "
               f"(limit {MAX_SUBJECT_WORDS})"))

    questions = d.body.count("?")
    # Only the ask itself (sentences ending in "?") — a proof point may mention time legitimately
    ask_sentences = " ".join(re.findall(r"[^.!?\n]*\?", d.body))
    fixed_slot = FIXED_SLOT.search(ask_sentences)
    ask_problems = []
    if questions != 1:
        ask_problems.append(f"{questions} questions in the email; it should have exactly one, the ask")
    if fixed_slot:
        ask_problems.append(f'asks for a fixed meeting length ("{fixed_slot.group(0)}"); '
                            "a first email should only check interest")
    checks.append(VerificationCheck(
        name="One light ask", passed=not ask_problems, detail="; ".join(ask_problems)))

    first_line = d.body.strip().splitlines()[0] if d.body.strip() else ""
    checks.append(VerificationCheck(
        name="Right greeting", passed=first in first_line,
        detail=f'Should greet "{first}"; starts with "{first_line[:40]}"'))

    banned = [b for b in BANNED_PHRASES if b in lower]
    leaked = INTERNAL_LABELS.findall(text)
    familiar = FAKE_FAMILIARITY.findall(text)
    problems = ([f'cliché "{b}"' for b in banned] + [f'internal label "{m}"' for m in leaked]
                + ["implies a past conversation"] * bool(familiar))
    checks.append(VerificationCheck(
        name="No clichés, internal labels or fake familiarity", passed=not problems,
        detail="; ".join(problems)))
    return checks


# ---------------------------------------------------------------------------
# Layer 2: AI verifier
# ---------------------------------------------------------------------------

class ClaimCheckOut(BaseModel):
    claim_number: int
    supported: bool
    note: str


class VerifyOut(BaseModel):
    claim_checks: list[ClaimCheckOut]
    unlisted_facts: list[str]
    forbidden_claim_used: str | None
    product_claims_ok: bool
    product_note: str
    entity_correct: bool
    entity_note: str
    why_you: int
    why_care: int
    why_respond: int
    subject_natural: bool
    subject_note: str
    improvement_notes: list[str]


SYSTEM = (
    "You are a strict fact-checker and editor for B2B sales emails. You check "
    "claims only against the evidence provided, and you are specific about problems."
)

PROMPT = """RECIPIENT (context for you, NOT part of the email; do not check it)
{name}, {title} at {company}

THE EMAIL (check only the subject and body below)
Subject: {subject}
{body}

CLAIMS THE WRITER LISTED
{claims}

EVIDENCE (what each source actually says)
{evidence}

THE SELLER'S PROFILE (the only true facts about {seller})
{profile}
Claims the email must never make: {forbidden}

CHECK
1. claim_checks: for each listed claim, is it fully supported by its cited
   evidence or profile field? Numbers, dates and names must match exactly.
   Reasonable rewording is fine; anything added or exaggerated is not.
2. unlisted_facts: specific factual statements about {company}, {name} or
   {seller} that are NOT in the claim list. These do NOT count:
   - reasoning about what companies in this situation typically face
     ("at that scale, vendor checks usually..."), as long as it is framed as
     reasoning rather than as a known fact about {company}
   - opinions, questions and the ask
   But a pain stated as a definite fact about {company} ("your team is
   struggling with...") without a source DOES count.
3. forbidden_claim_used: the forbidden claim the email makes, or null.
4. product_claims_ok: does everything said about {seller} match the profile?
5. entity_correct: is the email clearly about {name} at {company}, with
   nothing that belongs to another person or company?
6. Score 1-5 how well the email answers the reader's questions:
   why_you ("why are you contacting ME?"), why_care ("why should I care?"),
   why_respond ("why should I reply?"). 3 means acceptable.
7. subject_natural: would a busy executive read the subject as a natural, relevant
   note about their situation? False if it sounds like internal jargon, a ticket
   title or a product pitch. subject_note: one sentence why.
8. improvement_notes: up to 3 specific edits that would most improve it.
"""


def _evidence_for(run: Run, d: Draft) -> str:
    """For each cited source, the verified quotes we hold from it."""
    sources = {s.id: s for s in run.sources}
    cited = {c.support_ref for c in d.claims if c.support_type is SupportType.SOURCE}
    blocks = []
    for sid in cited:
        src = sources.get(sid)
        if not src:
            continue
        quotes = [g.evidence_quote for g in run.signals if g.source_id == sid][:6]
        blocks.append(f"[{sid}] {src.title}\n" + "\n".join(f'  - "{q}"' for q in quotes))
    return "\n".join(blocks) or "(no sources cited)"


def ai_checks(run: Run, d: Draft, seller: SellerProfile) -> tuple[list[VerificationCheck], VerifyOut]:
    p = run.request.prospect
    provider, model = verifier_for(run.request.provider, available_providers())
    profile = "\n".join(f"{f}: {getattr(seller, f)}" for f in PROFILE_FIELDS if getattr(seller, f))
    result = generate_structured(
        PROMPT.format(
            name=p.name, title=p.title or "(unknown role)", company=p.company, seller=seller.name,
            subject=d.subject, body=d.body,
            claims="\n".join(f"{i}. {c.text}  [{c.support_type.value}: {c.support_ref}]"
                             for i, c in enumerate(d.claims, start=1)) or "(none)",
            evidence=_evidence_for(run, d), profile=profile,
            forbidden="; ".join(seller.forbidden_claims) or "(none)",
        ),
        VerifyOut, provider=provider, model=model, system=SYSTEM,
    )
    track(run, result)
    out = result.data

    unsupported = [f"claim {c.claim_number}: {c.note}" for c in out.claim_checks if not c.supported]
    checks = [
        VerificationCheck(
            name="Facts match their sources", passed=not unsupported and not out.unlisted_facts,
            detail="; ".join(unsupported + [f"unlisted fact: {f}" for f in out.unlisted_facts])),
        VerificationCheck(
            name="Product claims stay within the profile",
            passed=out.product_claims_ok and not out.forbidden_claim_used,
            detail=(f"Forbidden claim: {out.forbidden_claim_used}. " if out.forbidden_claim_used else "")
            + ("" if out.product_claims_ok else out.product_note)),
        VerificationCheck(
            name="Right person and company", passed=out.entity_correct,
            detail="" if out.entity_correct else out.entity_note),
    ]
    checks.append(VerificationCheck(
        name="Subject reads naturally", passed=out.subject_natural,
        detail="" if out.subject_natural else out.subject_note))
    clamp = lambda x: min(max(x, 1), 5)
    out.why_you, out.why_care, out.why_respond = map(clamp, (out.why_you, out.why_care, out.why_respond))
    weak = [name for name, v in (("why you", out.why_you), ("why care", out.why_care),
                                 ("why respond", out.why_respond)) if v < MIN_QUESTION_SCORE]
    checks.append(VerificationCheck(
        name="Answers the reader's three questions", passed=not weak,
        detail=f"why you {out.why_you}/5, why care {out.why_care}/5, why respond {out.why_respond}/5"
               + (f"; weak on {', '.join(weak)}" if weak else "")))
    return checks, out


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def verify(run: Run, d: Draft, seller: SellerProfile) -> tuple[VerificationReport, list[str]]:
    """Returns the report and the verifier's improvement notes (used for a rewrite)."""
    checks = code_checks(run, d, seller)
    scores = (1, 1, 1)
    notes: list[str] = []
    try:
        ai, out = ai_checks(run, d, seller)
        checks += ai
        scores = (out.why_you, out.why_care, out.why_respond)
        notes = out.improvement_notes
    except ProviderError as exc:
        # Never fail open: a draft the AI couldn't check goes to the rep as unchecked
        checks.append(VerificationCheck(name="AI fact-check", passed=False,
                                        detail=f"Couldn't run: {exc}"))
    report = VerificationReport(checks=checks, why_you=scores[0], why_care=scores[1],
                                why_respond=scores[2])
    return report, notes


def _problems(report: VerificationReport, notes: list[str]) -> list[str]:
    items = [f"{c.name}: {c.detail}" for c in report.checks if not c.passed]
    return items + notes[:3]


def run(run: Run) -> StageResult:
    seller = get_seller_profile(run.request.seller_profile_id)
    draft = run.latest_draft
    report, notes = verify(run, draft, seller)
    failed = [c for c in report.checks if not c.passed]

    if failed:
        emit(run.id, Stage.VERIFY, EventType.PROGRESS,
             f"Draft {draft.version} failed {len(failed)} of {len(report.checks)} checks: "
             + "; ".join(c.name for c in failed) + ". Rewriting once.",
             {"failed": [c.model_dump() for c in failed]})
        revised = generate(run, angle=draft.angle, previous=draft, problems=_problems(report, notes))
        run.drafts.append(revised)
        emit(run.id, Stage.VERIFY, EventType.PROGRESS,
             f"Draft {revised.version}: {revised.word_count} words, {len(revised.claims)} factual claims")
        report, _ = verify(run, revised, seller)
        report.revised = True
        failed = [c for c in report.checks if not c.passed]

    run.verification = report
    passed = len(report.checks) - len(failed)
    scores = f"why you {report.why_you}, why care {report.why_care}, why respond {report.why_respond}"

    if failed:
        return StageResult(
            Outcome.CONTINUE,
            f"Needs your review: {len(failed)} check{'s' if len(failed) > 1 else ''} still failing "
            f"({', '.join(c.name for c in failed)})",
            status=RunStatus.NEEDS_REVIEW)
    weak_fit = run.fit and run.fit.verdict is FitVerdict.WEAK
    return StageResult(
        Outcome.CONTINUE,
        f"All {passed} checks passed" + (" after one rewrite" if report.revised else "")
        + f" ({scores})",
        status=RunStatus.LOW_SIGNAL if weak_fit else RunStatus.READY)
