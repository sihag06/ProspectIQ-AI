"""
Stage 3: company and prospect research.

1. A fast model writes a research plan: one search query per category,
   tailored to what the seller solves. Finance automation and recruiting
   automation should look for different things at the same company.
2. All searches run in parallel and return full page text.
3. Code turns results into tagged Sources: source type, date, whether
   it's about the person or the company, and drops off-topic pages.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from pydantic import BaseModel

from core.config import model_for
from core.db import get_seller_profile
from core.events import emit
from core.models import About, EventType, Run, SellerProfile, Source, SourceCategory, SourceType, Stage
from core.pipeline import Outcome, StageResult, track
from providers.base import ProviderError, generate_structured
from search.tavily_search import SearchQuery, search_many

MAX_SOURCES = 25
MAX_TEXT_CHARS = 12_000
MIN_TEXT_CHARS = 150

JOB_SITES = ("greenhouse.io", "lever.co", "myworkdayjobs", "workday.com", "linkedin.com/jobs",
             "indeed.", "naukri.com", "wellfound.com", "ashbyhq.com", "smartrecruiters", "glassdoor")
PRESS_SITES = ("prnewswire", "businesswire", "globenewswire", "accesswire", "einpresswire", "newswire")
BLOG_SITES = ("medium.com", "substack.com", "/blog")
INTERVIEW_WORDS = ("interview", "podcast", "fireside", "keynote", "in conversation", "q&a")


# ---------------------------------------------------------------------------
# Research plan (LLM)
# ---------------------------------------------------------------------------

class PlanOut(BaseModel):
    overview: str
    news: str
    funding: str
    hiring: str
    technology: str
    challenges: str
    focus: str


PLAN_PROMPT = """We sell: {seller_name} — {one_liner}
Problems we solve: {problems}
Our buyers: {buyers}

Target company: {company}{domain_note}

Write one web search query per category to research {company} for a sales
email about our product. Each query: at most 8 words, must contain
"{company}", and should look for evidence relevant to the problems we solve.

- overview: what the company does, its products and customers
- news: recent announcements relevant to us
- funding: funding, acquisitions, expansion
- hiring: open roles that signal the problems we solve (e.g. hiring for teams we help)
- technology: systems or tools they use or are adopting that relate to us
- challenges: publicly discussed problems related to what we solve
- focus: one sentence describing what this research is looking for and why
"""


def _plan(run: Run, seller: SellerProfile, domain: str | None) -> tuple[dict[SourceCategory, str], str]:
    company = run.request.prospect.company
    default = {
        SourceCategory.OVERVIEW: f"{company} company products customers",
        SourceCategory.NEWS: f"{company} news announcement",
        SourceCategory.FUNDING: f"{company} funding acquisition expansion",
        SourceCategory.HIRING: f"{company} hiring jobs",
        SourceCategory.TECHNOLOGY: f"{company} technology platform software",
        SourceCategory.CHALLENGES: f"{company} challenges",
    }
    try:
        result = generate_structured(
            PLAN_PROMPT.format(
                seller_name=seller.name, one_liner=seller.one_liner,
                problems="; ".join(seller.problems_solved),
                buyers=", ".join(seller.target_buyer_roles) or "(not specified)",
                company=company, domain_note=f" ({domain})" if domain else "",
            ),
            PlanOut, provider=run.request.provider,
            model=model_for(run.request.provider, Stage.IDENTITY),  # fast tier
        )
    except ProviderError:
        return default, "Standard research plan (the AI planner was unavailable)"
    track(run, result)
    out = result.data
    plan = {}
    for cat in default:
        q = " ".join(getattr(out, cat.value).split()[:10])
        # Code check: every query must name the company, or results drift
        if company.lower() not in q.lower():
            q = f"{company} {q}"
        plan[cat] = q
    return plan, out.focus


# ---------------------------------------------------------------------------
# Turning search hits into Sources
# ---------------------------------------------------------------------------

def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    parsed = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value).date()
        except (TypeError, ValueError):
            return None
    return parsed if parsed <= date.today() else None


def classify(url: str, title: str, category: SourceCategory, domain: str | None) -> SourceType:
    u, t = url.lower(), title.lower()
    host = urlparse(url).hostname or ""
    if domain and (host == domain or host.endswith("." + domain)):
        return SourceType.JOB_POSTING if "career" in u or "/jobs" in u else SourceType.COMPANY_SITE
    if any(s in u for s in JOB_SITES):
        return SourceType.JOB_POSTING
    if any(s in u for s in PRESS_SITES):
        return SourceType.PRESS_RELEASE
    if any(w in t for w in INTERVIEW_WORDS):
        return SourceType.INTERVIEW
    if any(s in u for s in BLOG_SITES):
        return SourceType.BLOG
    if category in (SourceCategory.NEWS, SourceCategory.FUNDING):
        return SourceType.NEWS
    return SourceType.OTHER


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _company_key(company: str) -> str:
    """'Acme Logistics Pvt. Ltd.' -> 'acmelogistics'"""
    cleaned = re.sub(r"\b(inc|ltd|llc|limited|pvt|private|corp|corporation|co|plc|gmbh)\b\.?",
                     "", company.lower())
    return _norm(cleaned)


def _mentions_company(text: str, url: str, company: str, domain: str | None) -> bool:
    if domain and domain in url:
        return True
    key = _company_key(company)
    blob = _norm(text[:20_000])
    return bool(key) and (key in blob or (domain and _norm(domain.split(".")[0]) in blob))


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def run(run: Run) -> StageResult:
    p = run.request.prospect
    seller = get_seller_profile(run.request.seller_profile_id)
    domain = run.identity.company_domain if run.identity else None

    plan, focus = _plan(run, seller, domain)
    emit(run.id, Stage.RESEARCH, EventType.PROGRESS, f"Research plan: {focus}",
         {"queries": {k.value: v for k, v in plan.items()}})

    queries: list[tuple[SourceCategory, SearchQuery]] = []
    for cat, q in plan.items():
        recent = cat in (SourceCategory.NEWS, SourceCategory.FUNDING, SourceCategory.HIRING)
        queries.append((cat, SearchQuery(
            query=q, max_results=5, include_raw=True,
            topic="news" if cat in (SourceCategory.NEWS, SourceCategory.FUNDING) else "general",
            time_range="year" if recent else None,
        )))
    if domain:
        queries.append((SourceCategory.OVERVIEW, SearchQuery(
            query=f"{p.company} about products", max_results=3, include_raw=True,
            include_domains=[domain])))
    queries.append((SourceCategory.LEADERSHIP, SearchQuery(
        query=f'"{p.name}" {p.company} interview OR said OR podcast', max_results=5,
        include_raw=True)))

    results = search_many([q for _, q in queries], provider=run.request.search_provider)
    cached = all(r.from_cache for r in results)

    sources: list[Source] = []
    seen: set[str] = set()
    dropped_off_topic = dropped_thin = 0
    for (cat, _), result in zip(queries, results):
        for h in result.hits:
            if h.url in seen:
                continue
            seen.add(h.url)
            text = (h.raw_content or h.snippet or "").strip()
            if len(text) < MIN_TEXT_CHARS:
                dropped_thin += 1
                continue
            if not _mentions_company(f"{h.title} {text}", h.url, p.company, domain):
                dropped_off_topic += 1
                continue
            about_person = _norm(p.name) in _norm(f"{h.title} {text[:5000]}")
            sources.append(Source(
                url=h.url, title=h.title, category=cat,
                source_type=classify(h.url, h.title, cat, domain),
                about=About.PERSON if about_person else About.COMPANY,
                published_at=parse_date(h.published_date),
                text=text[:MAX_TEXT_CHARS],
            ))

    run.sources = sources[:MAX_SOURCES]
    counts = {}
    for s in run.sources:
        counts[s.category.value] = counts.get(s.category.value, 0) + 1

    msg = f"Ran {len(results)} searches" + (" (from cache)" if cached else "")
    if dropped_off_topic or dropped_thin:
        msg += f"; dropped {dropped_off_topic} off-topic and {dropped_thin} near-empty pages"
    emit(run.id, Stage.RESEARCH, EventType.PROGRESS, msg)

    person_count = sum(1 for s in run.sources if s.about is About.PERSON)
    if not run.sources:
        return StageResult(Outcome.CONTINUE, "No usable public sources found",
                           {"low_signal": True})
    return StageResult(
        Outcome.CONTINUE,
        f"{len(run.sources)} sources collected, {person_count} mention {p.name}",
        {"by_category": counts},
    )
