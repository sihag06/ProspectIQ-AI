"""
ProspectIQ entry point. Run with:  streamlit run app.py

This file only sets up the app and the navigation. Each page lives in
pages/ and is registered here, so the order and names are in one place.
"""
import streamlit as st

from core.db import init_db
from core.presets import seed_presets

st.set_page_config(page_title="ProspectIQ", page_icon=":material/travel_explore:",
                   layout="wide")


@st.cache_resource
def _setup() -> bool:
    """Runs once per app process, not on every click."""
    init_db()
    seed_presets()
    return True


_setup()

pages = {
    "Outreach": [
        st.Page("pages/new_run.py", title="New research", icon=":material/add_circle:", default=True),
        st.Page("pages/live_run.py", title="Live run", icon=":material/play_circle:"),
        st.Page("pages/review.py", title="Review", icon=":material/rate_review:"),
        st.Page("pages/dashboard.py", title="Dashboard", icon=":material/table_rows:"),
    ],
    "Setup": [
        st.Page("pages/settings.py", title="Settings", icon=":material/tune:"),
        st.Page("pages/about.py", title="How it works", icon=":material/info:"),
    ],
}

# Streamlit's built-in navigation always sits above custom sidebar content,
# so it's hidden and drawn here instead, with the product name on top.
current = st.navigation(pages, position="hidden")
with st.sidebar:
    st.markdown("### ProspectIQ")
    st.caption("Evidence-backed outreach, from prospect to draft.")
    for section, items in pages.items():
        st.caption(section)
        for page in items:
            st.page_link(page)
current.run()
