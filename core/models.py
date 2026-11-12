"""
ProspectIQ data models.

These are the shapes of data that move between pipeline stages and get
saved to the database. Every stage reads and writes these objects, so
they are defined once, here.

Note: the schemas an LLM fills in directly (e.g. "extract signals from
this page") live next to each stage, not here. Providers have different
rules for structured output, and keeping those schemas separate stops
those rules from leaking into the app's own records.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, ValidationError, computed_field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def new_id(prefix: str) -> str:
    """Short readable IDs like 'run_3f9a1c2b' or 'src_77de01aa'."""
    return f"{prefix}_{uuid4().hex[:8]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums: every fixed set of values in the app
# ---------------------------------------------------------------------------

class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"      # paused, e.g. ambiguous identity
    LOW_SIGNAL = "low_signal"        # weak fit, softer angle used
    NEEDS_REVIEW = "needs_review"    # verification failed twice
    READY = "ready"                  # verified, waiting for the rep
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"                # technical error


class Stage(StrEnum):
    INTAKE = "intake"
    IDENTITY = "identity"
    RESEARCH = "research"
    EXTRACT = "extract"
    FIT = "fit"
    DRAFT = "draft"
    VERIFY = "verify"
    REVIEW = "review"


STAGE_ORDER: list[Stage] = list(Stage)

STAGE_LABELS: dict[Stage, str] = {
    Stage.INTAKE: "Intake",
    Stage.IDENTITY: "Identity resolution",
    Stage.RESEARCH: "Company and prospect research",
    Stage.EXTRACT: "Signal extraction",
    Stage.FIT: "Product–company fit",
    Stage.DRAFT: "Email generation",
    Stage.VERIFY: "Verification",
    Stage.REVIEW: "Human review",
}


class EventType(StrEnum):
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class Provider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


class SearchProvider(StrEnum):
    TAVILY = "tavily"


class Tone(StrEnum):
    PROFESSIONAL = "professional"
    CONVERSATIONAL = "conversational"
    DIRECT = "direct"


class SourceCategory(StrEnum):
    OVERVIEW = "overview"
    NEWS = "news"
    FUNDING = "funding"
    HIRING = "hiring"
    TECHNOLOGY = "technology"
    CHALLENGES = "challenges"
    LEADERSHIP = "leadership"


class SourceType(StrEnum):
    COMPANY_SITE = "company_site"
    PRESS_RELEASE = "press_release"
    NEWS = "news"
    JOB_POSTING = "job_posting"
    INTERVIEW = "interview"
    BLOG = "blog"
    OTHER = "other"


class About(StrEnum):
    PERSON = "person"
    COMPANY = "company"


class IdentityStatus(StrEnum):
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    ROLE_MISMATCH = "role_mismatch"
    UNVERIFIED = "unverified"        # company found, person has no public footprint


class FitVerdict(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class SupportType(StrEnum):
    SOURCE = "source"                  # backed by a research source
    SELLER_PROFILE = "seller_profile"  # backed by the seller's own profile


# ---------------------------------------------------------------------------
# Stage 1: intake
# ---------------------------------------------------------------------------

class SellerProfile(BaseModel):
    """What we sell. The foundation of product–company fit analysis."""
    id: str = Field(default_factory=lambda: new_id("seller"))
    name: str = Field(min_length=1)
    one_liner: str = Field(min_length=1)
    target_customers: list[str] = Field(default_factory=list)
    target_buyer_roles: list[str] = Field(default_factory=list)
    problems_solved: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    proof_points: list[str] = Field(default_factory=list)    # only real, verifiable ones
    forbidden_claims: list[str] = Field(default_factory=list)  # things the email must never say
    preferred_cta: str = "A short conversation"


class ProspectInput(BaseModel):
    """Who we are reaching out to, as typed by the rep."""
    name: str = Field(min_length=1)
    title: str = ""
    company: str = Field(min_length=1)
    website: HttpUrl | None = None
    tone: Tone = Tone.PROFESSIONAL


class RunRequest(BaseModel):
    prospect: ProspectInput
    seller_profile_id: str
    provider: Provider
    model: str
    search_provider: SearchProvider = SearchProvider.TAVILY  # default keeps older runs loadable


# ---------------------------------------------------------------------------
# Stage 2: identity resolution
# ---------------------------------------------------------------------------

class IdentityCandidate(BaseModel):
    name: str
    title: str = ""
    company: str = ""
    evidence_url: str | None = None
    note: str = ""


class IdentityResult(BaseModel):
    status: IdentityStatus
    company_domain: str | None = None
    current_company: str | None = None   # differs from input on a job change
    candidates: list[IdentityCandidate] = Field(default_factory=list)
    explanation: str = ""


# ---------------------------------------------------------------------------
# Stage 3: research
# ---------------------------------------------------------------------------

class Source(BaseModel):
    id: str = Field(default_factory=lambda: new_id("src"))
    url: str
    title: str = ""
    source_type: SourceType = SourceType.OTHER
    category: SourceCategory
    about: About = About.COMPANY
    published_at: date | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    text: str = ""


# ---------------------------------------------------------------------------
# Stage 4: signal extraction
# ---------------------------------------------------------------------------

class Signal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sig"))
    category: SourceCategory
    description: str
    evidence_quote: str          # must appear verbatim in the source text
    source_id: str
    about: About = About.COMPANY
    published_at: date | None = None
    quote_verified: bool = False  # set by the code check, never by the LLM


class CompanySnapshot(BaseModel):
    """Baseline understanding of the company, not just recent events."""
    what_they_do: str = ""
    products: list[str] = Field(default_factory=list)
    customers: list[str] = Field(default_factory=list)
    size_estimate: str = ""
    business_model: str = ""


# ---------------------------------------------------------------------------
# Stage 5: product–company fit (the core)
# ---------------------------------------------------------------------------

class FitScore(BaseModel):
    """
    S = 0.35*Fit + 0.30*Business impact + 0.20*Freshness + 0.15*Evidence quality

    fit and business_impact come from the LLM on a 1–5 rubric and are
    scaled to 0–100 in Stage 5. freshness and evidence_quality are
    computed in code. total is the weighted result, 0–100.
    """
    fit: int = Field(ge=1, le=5)
    business_impact: int = Field(ge=1, le=5)
    freshness: float = Field(ge=0, le=100)
    evidence_quality: float = Field(ge=0, le=100)
    total: float = Field(ge=0, le=100)


class FitCandidate(BaseModel):
    signal_id: str
    likely_pain: str
    matched_capability: str      # must exist in the seller profile
    expected_outcome: str
    thesis: str                  # "Because X is doing Y, they likely face Z; ..."
    reasoning: str = ""
    score: FitScore


class ExcludedSignal(BaseModel):
    signal_id: str
    reason: str                  # e.g. "sensitive: layoffs"


class FitAnalysis(BaseModel):
    verdict: FitVerdict
    chosen: FitCandidate | None = None
    runner_up: FitCandidate | None = None
    excluded: list[ExcludedSignal] = Field(default_factory=list)
    buyer_owns_pain: bool = True
    suggested_recipient_role: str | None = None
    buyer_note: str = ""
    icp_match: str = ""          # "yes" / "partly" / "no": is this the seller's kind of customer?
    icp_note: str = ""
    recommendation: str = "send"   # "send", "redirect" (better recipient) or "hold" (don't send)
    recommendation_note: str = ""
    ranked: list[FitCandidate] = Field(default_factory=list)  # every assessed signal, best first
    assessed_count: int = 0


# ---------------------------------------------------------------------------
# Stage 6: email generation
# ---------------------------------------------------------------------------

class Claim(BaseModel):
    text: str
    support_type: SupportType
    support_ref: str             # a source id, or a seller profile field name


class Draft(BaseModel):
    id: str = Field(default_factory=lambda: new_id("draft"))
    version: int = 1
    subject: str
    body: str
    claims: list[Claim] = Field(default_factory=list)
    based_on_signal_id: str | None = None
    personalisation: str = "company"   # "person", "company" or "low"
    angle: str = "chosen"              # "chosen" or "runner_up"
    created_at: datetime = Field(default_factory=utc_now)

    @computed_field
    @property
    def word_count(self) -> int:
        return len(self.body.split())


# ---------------------------------------------------------------------------
# Stage 7: verification
# ---------------------------------------------------------------------------

class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class VerificationReport(BaseModel):
    checks: list[VerificationCheck] = Field(default_factory=list)
    why_you: int = Field(default=1, ge=1, le=5)      # "Why are you contacting me?"
    why_care: int = Field(default=1, ge=1, le=5)     # "Why should I care?"
    why_respond: int = Field(default=1, ge=1, le=5)  # "Why should I respond?"
    revised: bool = False

    @computed_field
    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


# ---------------------------------------------------------------------------
# Events and the run itself
# ---------------------------------------------------------------------------

class RunEvent(BaseModel):
    """One line in the live run view and the run history."""
    run_id: str
    stage: Stage
    type: EventType
    message: str
    data: dict = Field(default_factory=dict)
    at: datetime = Field(default_factory=utc_now)


class Run(BaseModel):
    """
    Everything known about one research run. Saved after every stage,
    which is what makes the pipeline resumable.
    """
    id: str = Field(default_factory=lambda: new_id("run"))
    request: RunRequest
    status: RunStatus = RunStatus.CREATED
    current_stage: Stage = Stage.INTAKE

    identity: IdentityResult | None = None
    sources: list[Source] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    snapshot: CompanySnapshot | None = None
    fit: FitAnalysis | None = None
    drafts: list[Draft] = Field(default_factory=list)
    verification: VerificationReport | None = None

    final_email: str | None = None
    rejection_reason: str | None = None

    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def latest_draft(self) -> Draft | None:
        return self.drafts[-1] if self.drafts else None

    def touch(self) -> None:
        self.updated_at = utc_now()


# ---------------------------------------------------------------------------
# Self-test: run with `python -m core.models`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    seller = SellerProfile(
        name="FlowOps",
        one_liner="AI workflow automation for finance teams",
        problems_solved=["Manual invoice processing"],
        capabilities=["Document data extraction", "Automated approvals"],
    )
    prospect = ProspectInput(
        name="Alex Johnson",
        title="CFO",
        company="Acme Technologies",
        website="https://example.com",
    )
    run = Run(
        request=RunRequest(
            prospect=prospect,
            seller_profile_id=seller.id,
            provider=Provider.OPENAI,
            model="gpt-model-name",
        )
    )
    run.drafts.append(Draft(subject="Hello", body="This is a short test body."))

    print("1. Valid run created:", run.id)
    print("   Status:", run.status, "| Stage:", run.current_stage)
    print("   Draft word count:", run.latest_draft.word_count)

    # Round trip: save to JSON and load back, as the database will do
    restored = Run.model_validate_json(run.model_dump_json())
    print("2. JSON round trip OK:", restored.id == run.id)

    # Bad input should be rejected
    try:
        ProspectInput(name="Alex", company="")
    except ValidationError as e:
        print("3. Empty company rejected:", e.errors()[0]["msg"])

    try:
        FitScore(fit=7, business_impact=3, freshness=50, evidence_quality=50, total=60)
    except ValidationError:
        print("4. Out-of-range fit score rejected")

    print("\nAll model checks passed.")
