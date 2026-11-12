"""
Web search adapter (Tavily), with a disk cache.

Stages call search() or search_many() and get back plain SearchHit
objects, so swapping Tavily for another search API only changes this file.

The cache matters for two reasons:
  - reruns of the same prospect are instant and cost no search credits
  - demo mode (PROSPECTIQ_DEMO_MODE=1) always uses cached results when
    they exist, so a live interview can't be derailed by a slow search API
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

import core.config  # noqa: F401  (loads .env)

CACHE_DIR = Path(os.environ.get("PROSPECTIQ_CACHE_DIR", "data/cache/search"))
CACHE_TTL_HOURS = float(os.environ.get("PROSPECTIQ_SEARCH_CACHE_HOURS", "72"))
MAX_RAW_CHARS = 20_000


def demo_mode() -> bool:
    return os.environ.get("PROSPECTIQ_DEMO_MODE", "").lower() in ("1", "true", "yes")


class SearchError(Exception):
    """A clear, user-facing search problem."""


class SearchHit(BaseModel):
    title: str = ""
    url: str
    snippet: str = ""
    raw_content: str | None = None
    published_date: str | None = None
    score: float = 0.0


class SearchQuery(BaseModel):
    query: str
    max_results: int = 5
    topic: str = "general"            # "general" or "news"
    time_range: str | None = None     # "day", "week", "month", "year"
    include_raw: bool = False         # full page text, for research sources
    include_domains: list[str] | None = None


class SearchResult(BaseModel):
    query: SearchQuery
    hits: list[SearchHit]
    from_cache: bool


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_path(q: SearchQuery) -> Path:
    key = hashlib.sha1(q.model_dump_json().encode()).hexdigest()[:20]
    return CACHE_DIR / f"{key}.json"


def _read_cache(q: SearchQuery) -> list[SearchHit] | None:
    path = _cache_path(q)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    age_hours = (time.time() - data["saved_at"]) / 3600
    if age_hours > CACHE_TTL_HOURS and not demo_mode():
        return None
    return [SearchHit(**h) for h in data["hits"]]


def _write_cache(q: SearchQuery, hits: list[SearchHit]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(q).write_text(json.dumps({
        "saved_at": time.time(),
        "query": q.model_dump(),
        "hits": [h.model_dump() for h in hits],
    }))


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            raise SearchError("Web search isn't configured: add TAVILY_API_KEY to your .env file.")
        from tavily import TavilyClient
        _client = TavilyClient(api_key=key)
    return _client


def _friendly(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    text = str(exc)
    if "apikey" in name or "invalid" in name or "401" in text:
        return "Tavily rejected the API key. Check TAVILY_API_KEY in .env."
    if "usage" in name or "limit" in name or "432" in text or "429" in text:
        return "Tavily search limit reached. Check your Tavily plan, or turn on demo mode."
    if "timeout" in name:
        return "Web search timed out. Try again."
    return f"Web search failed: {type(exc).__name__}: {exc}"


def search(q: SearchQuery) -> SearchResult:
    cached = _read_cache(q)
    if cached is not None:
        return SearchResult(query=q, hits=cached, from_cache=True)

    kwargs = dict(
        query=q.query,
        search_depth="basic",
        topic=q.topic,
        max_results=q.max_results,
    )
    if q.time_range:
        kwargs["time_range"] = q.time_range
    if q.include_raw:
        kwargs["include_raw_content"] = "text"
    if q.include_domains:
        kwargs["include_domains"] = q.include_domains

    try:
        response = _get_client().search(**kwargs)
    except SearchError:
        raise
    except Exception as exc:
        raise SearchError(_friendly(exc)) from exc

    hits = []
    for r in response.get("results", []):
        if not r.get("url"):
            continue
        raw = r.get("raw_content")
        hits.append(SearchHit(
            title=r.get("title") or "",
            url=r["url"],
            snippet=(r.get("content") or "")[:1500],
            raw_content=raw[:MAX_RAW_CHARS] if raw else None,
            published_date=r.get("published_date"),
            score=float(r.get("score") or 0),
        ))
    _write_cache(q, hits)
    return SearchResult(query=q, hits=hits, from_cache=False)


# ---------------------------------------------------------------------------
# Search providers. Adding one (e.g. Exa, Brave) = one search function with
# the same signature as search() above, plus an entry in each dict below.
# ---------------------------------------------------------------------------

from core.models import SearchProvider  # noqa: E402

SEARCH_LABELS = {SearchProvider.TAVILY: "Tavily"}
SEARCH_ENV_KEYS = {SearchProvider.TAVILY: "TAVILY_API_KEY"}


def available_search_providers() -> list[SearchProvider]:
    return [p for p, key in SEARCH_ENV_KEYS.items() if os.environ.get(key)]


def _search_fn(provider: SearchProvider):
    return {SearchProvider.TAVILY: lambda q: search(q)}[provider]


def search_many(queries: list[SearchQuery], max_workers: int = 6,
                provider: SearchProvider = SearchProvider.TAVILY) -> list[SearchResult]:
    """Run several searches in parallel. Results come back in the same order."""
    fn = _search_fn(provider)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(fn, queries))


def unique_hits(results: list[SearchResult]) -> list[SearchHit]:
    """All hits across results, first occurrence of each URL only."""
    seen, out = set(), []
    for r in results:
        for h in r.hits:
            if h.url not in seen:
                seen.add(h.url)
                out.append(h)
    return out
