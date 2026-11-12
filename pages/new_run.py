"""New research: the rep names a prospect and picks what they're selling."""
import streamlit as st
from pydantic import ValidationError

from core.db import list_seller_profiles, save_seller_profile
from core.models import ProspectInput, SellerProfile, Tone
from providers.base import PROVIDER_LABELS, available_providers
from search.tavily_search import SEARCH_LABELS, available_search_providers
from stages.s1_intake import IntakeError, create_run, normalise_website

NEW = "__new__"


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@st.dialog("Add a new seller", width="large")
def add_seller_dialog() -> None:
    st.write("Describe the product you're selling. Fit analysis can only use what you "
             "list here, so be specific. One item per line.")
    name = st.text_input("Product name", placeholder="Shipwise")
    one_liner = st.text_input("One-line description", placeholder="Freight invoice auditing for shippers")
    c1, c2 = st.columns(2)
    problems = c1.text_area("Problems it solves", placeholder="Overcharged freight invoices\nManual carrier billing checks")
    capabilities = c2.text_area("What it does", placeholder="Audits every freight invoice automatically\nFlags overcharges with evidence")
    buyers = st.text_area("Who buys it (job titles)", placeholder="Head of Logistics\nVP Supply Chain", height=80)
    with st.expander("More detail (makes emails sharper)"):
        customers = st.text_area("Target customers", placeholder="Manufacturers shipping 500+ loads a month")
        differentiators = st.text_area("Why you, not an alternative")
        proof = st.text_area("Proof points (real, verifiable results only)")
        forbidden = st.text_area("Claims the email must never make", placeholder="Guaranteed savings")
        cta = st.text_input("Preferred call to action", "A short conversation")

    if st.button("Save seller", type="primary"):
        missing = [label for label, value in [("product name", name), ("one-line description", one_liner),
                                              ("problems it solves", problems), ("what it does", capabilities)]
                   if not value.strip()]
        if missing:
            st.error("Add " + ", ".join(missing) + " to continue.")
            return
        profile = SellerProfile(
            name=name.strip(), one_liner=one_liner.strip(),
            problems_solved=_lines(problems), capabilities=_lines(capabilities),
            target_buyer_roles=_lines(buyers), target_customers=_lines(customers),
            differentiators=_lines(differentiators), proof_points=_lines(proof),
            forbidden_claims=_lines(forbidden), preferred_cta=cta.strip() or "A short conversation",
        )
        save_seller_profile(profile)
        st.session_state["pending_seller"] = profile.id  # selected on the next run
        st.rerun()


def _on_seller_change() -> None:
    # Choosing "+ Add a new seller" opens the dialog and puts the old choice back
    if st.session_state.get("seller_choice") == NEW:
        st.session_state["open_seller_dialog"] = True
        st.session_state["seller_choice"] = st.session_state.get("last_seller")
    else:
        st.session_state["last_seller"] = st.session_state.get("seller_choice")


st.title("New research")
st.write("Name a prospect and pick what you're selling. ProspectIQ researches the "
         "company, finds where your product fits, and drafts an email for you to review.")

sellers = list_seller_profiles()
providers = available_providers()
search_providers = available_search_providers()
if not search_providers:
    st.warning("No web search is configured. Add TAVILY_API_KEY to your .env file, "
               "then restart the app.")
    st.stop()
if not providers:
    st.warning("No AI provider is configured. Add OPENAI_API_KEY, ANTHROPIC_API_KEY "
               "or GEMINI_API_KEY to your .env file, then restart the app.")
    st.stop()

by_id = {s.id: s for s in sellers}
if "pending_seller" in st.session_state:
    st.session_state["seller_choice"] = st.session_state.pop("pending_seller")
    st.session_state["last_seller"] = st.session_state["seller_choice"]
if st.session_state.get("seller_choice") not in by_id:
    st.session_state["seller_choice"] = sellers[0].id if sellers else None
    st.session_state["last_seller"] = st.session_state["seller_choice"]

with st.container(border=True):
    st.subheader("Prospect")
    c1, c2 = st.columns(2)
    name = c1.text_input("Name", placeholder="Priya Raman")
    title = c2.text_input("Job title", placeholder="VP Finance")
    c3, c4 = st.columns(2)
    company = c3.text_input("Company", placeholder="Acme Logistics")
    website = c4.text_input("Company website (optional)", placeholder="acmelogistics.com",
                            help="Helps confirm we research the right company.")

    st.subheader("Your offer")
    c5, c6, c7 = st.columns([2, 1.3, 1])
    seller_id = c5.selectbox(
        "What are you selling?", [s.id for s in sellers] + [NEW], key="seller_choice",
        format_func=lambda i: "+ Add a new seller" if i == NEW else by_id[i].name,
        on_change=_on_seller_change,
    )
    provider = c6.selectbox("AI provider", providers, format_func=lambda p: PROVIDER_LABELS[p])
    search_provider = c7.selectbox("Web search", search_providers,
                                   format_func=lambda p: SEARCH_LABELS[p],
                                   help="Add more search providers in Settings → Web search.")
    tone = st.segmented_control("Tone", list(Tone), default=Tone.PROFESSIONAL,
                                format_func=lambda t: t.value.capitalize(), required=True)
    seller = by_id.get(seller_id)
    if seller:
        st.caption(f"{seller.one_liner}. Best buyers: {', '.join(seller.target_buyer_roles) or 'not set'}.")
    submitted = st.button("Start research", type="primary", disabled=seller is None)

if st.session_state.pop("open_seller_dialog", False) or not sellers:
    add_seller_dialog()

if submitted:
    try:
        prospect = ProspectInput(
            name=name.strip(), title=title.strip(), company=company.strip(),
            website=normalise_website(website), tone=tone,
        )
    except ValidationError as e:
        problems = []
        for err in e.errors():
            field = err["loc"][0]
            problems.append("Website doesn't look like a valid address." if field == "website"
                            else f"{str(field).capitalize()} is required.")
        st.error(" ".join(dict.fromkeys(problems)))
        st.stop()
    try:
        run = create_run(prospect, seller.id, provider, search_provider)
    except IntakeError as e:
        st.error(str(e))
        st.stop()
    st.session_state["run_id"] = run.id
    st.switch_page("pages/live_run.py", query_params={"run": run.id})
