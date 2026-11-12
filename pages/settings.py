"""Settings: seller profiles (what you sell) and AI providers."""
import streamlit as st
from pydantic import BaseModel, ValidationError

from core.config import MODELS, STAGE_TIER
from core.db import delete_seller_profile, list_seller_profiles, save_seller_profile
from core.models import Provider, SellerProfile, STAGE_LABELS
from search.tavily_search import CACHE_DIR, SearchError, SearchQuery, demo_mode, search
from providers.base import (
    PROVIDER_ENV_KEYS,
    PROVIDER_LABELS,
    ProviderError,
    available_providers,
    generate_structured,
)

st.title("Settings")
tab_sellers, tab_providers, tab_search = st.tabs(["Seller profiles", "AI providers", "Web search"])

# ---------------------------------------------------------------------------
# Seller profiles
# ---------------------------------------------------------------------------

LIST_FIELDS = {
    "target_customers": ("Target customers", "Who buys this, e.g. company size or industry"),
    "target_buyer_roles": ("Buyer roles", "Job titles that own the problem"),
    "problems_solved": ("Problems solved", "The pains your product removes"),
    "capabilities": ("Capabilities", "What the product actually does"),
    "differentiators": ("Differentiators", "Why you rather than an alternative"),
    "proof_points": ("Proof points", "Only real, verifiable results"),
    "forbidden_claims": ("Claims the email must never make", "e.g. guaranteed ROI"),
}


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


with tab_sellers:
    st.write("The seller profile is what fit analysis matches against. The more specific "
             "the capabilities and problems, the sharper the email. One item per line.")
    st.caption("Ledgerline and Talentloop are fictional sellers for demos; their proof points are invented.")

    profiles = list_seller_profiles()
    NEW = "__new__"
    options = [p.id for p in profiles] + [NEW]
    names = {p.id: p.name for p in profiles} | {NEW: "New profile"}
    selected_id = st.selectbox("Profile", options, format_func=names.get)
    current = next((p for p in profiles if p.id == selected_id), None)
    base = current or SellerProfile.model_construct(
        name="", one_liner="", preferred_cta="A short conversation",
        **{f: [] for f in LIST_FIELDS},
    )

    with st.form(f"seller_{selected_id}"):
        c1, c2 = st.columns([1, 2])
        name = c1.text_input("Product name", base.name)
        one_liner = c2.text_input("One-line description", base.one_liner)
        values = {}
        cols = st.columns(2)
        for i, (field, (label, hint)) in enumerate(LIST_FIELDS.items()):
            values[field] = cols[i % 2].text_area(
                label, "\n".join(getattr(base, field)), help=hint, height=120)
        cta = st.text_input("Preferred call to action", base.preferred_cta)
        saved = st.form_submit_button("Save profile", type="primary")

    if saved:
        try:
            profile = SellerProfile(
                **({"id": current.id} if current else {}),
                name=name.strip(), one_liner=one_liner.strip(),
                preferred_cta=cta.strip() or "A short conversation",
                **{f: _lines(v) for f, v in values.items()},
            )
            save_seller_profile(profile)
            st.toast(f"Saved {profile.name}", icon=":material/check:")
            st.rerun()
        except ValidationError:
            st.error("Product name and one-line description are required.")

    if current:
        with st.expander("Delete this profile"):
            st.write(f"Past runs keep their results, but you won't be able to start new "
                     f"runs with {current.name}.")
            if st.button(f"Delete {current.name}", type="secondary"):
                delete_seller_profile(current.id)
                st.rerun()

# ---------------------------------------------------------------------------
# AI providers
# ---------------------------------------------------------------------------

with tab_providers:
    st.write("A provider appears here once its API key is in .env. Each stage uses a "
             "fast model for high-volume work and a smart model where judgment matters.")
    ready = available_providers()

    for p in Provider:
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 3, 1], vertical_alignment="center")
            c1.markdown(f"**{PROVIDER_LABELS[p]}**")
            if p in ready:
                c1.badge("Key found", color="green")
            else:
                c1.badge(f"Add {PROVIDER_ENV_KEYS[p]}", color="gray")
            c2.markdown(f"Fast: `{MODELS[p]['fast']}`  \nSmart: `{MODELS[p]['smart']}`")
            if c3.button("Test", key=f"test_{p.value}", disabled=p not in ready):
                class Ping(BaseModel):
                    reply: str
                try:
                    r = generate_structured("Reply with the word 'ready'.", Ping,
                                            provider=p, model=MODELS[p]["fast"], max_tokens=2000)
                    st.success(f"Connected in {r.latency_s}s using {r.model}.")
                except ProviderError as e:
                    st.error(str(e))

    st.subheader("Model used at each stage")
    st.dataframe(
        [{"Stage": STAGE_LABELS[s], "Tier": t} for s, t in STAGE_TIER.items()],
        hide_index=True, width="content",
    )
    st.caption("Change models and tiers in core/config.py.")

# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

with tab_search:
    import os
    import shutil

    st.write("ProspectIQ uses Tavily to search the web. Results are cached, so running "
             "the same prospect again is instant and uses no search credits.")

    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 3, 1], vertical_alignment="center")
        c1.markdown("**Tavily**")
        has_key = bool(os.environ.get("TAVILY_API_KEY"))
        if has_key:
            c1.badge("Key found", color="green")
        else:
            c1.badge("Add TAVILY_API_KEY", color="gray")
        c2.markdown("Basic search depth, up to 5 results per query, full page text "
                    "for research sources.")
        if c3.button("Test", key="test_tavily", disabled=not has_key):
            try:
                # A unique query each time, so the test always reaches Tavily
                import time as _t
                r = search(SearchQuery(query=f"Tavily search API test {int(_t.time())}", max_results=1))
                st.success(f"Connected. Tavily returned {len(r.hits)} result.")
            except SearchError as e:
                st.error(str(e))

    with st.container(border=True):
        files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.markdown(f"**Search cache** · {len(files)} saved searches")
        if demo_mode():
            c1.badge("Demo mode on: cached results never expire", color="violet")
        else:
            c1.caption("Cached results expire after 72 hours. Add PROSPECTIQ_DEMO_MODE=1 "
                       "to .env before a demo to keep them indefinitely.")
        if c2.button("Clear cache", disabled=not files):
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
            st.toast("Search cache cleared", icon=":material/delete:")
            st.rerun()
