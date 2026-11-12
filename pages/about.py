"""How it works: the pipeline in one screen, plus the assumptions behind it."""
from pathlib import Path

import streamlit as st

from core.models import STAGE_LABELS, STAGE_ORDER

st.title("How ProspectIQ works")
st.write("A rep names a prospect. ProspectIQ researches the company, decides whether and "
         "why the seller's product fits, writes an email grounded in what it found, checks "
         "every claim, and hands the draft to the rep. Nothing is sent automatically.")

WHAT = {
    "intake": "Checks the request and the seller profile, then saves a run.",
    "identity": "Confirms the right person and company; fixes names and titles from sources; "
                "pauses for the rep on an ambiguous name or a job change.",
    "research": "Writes a research plan tailored to the seller, then runs searches in parallel.",
    "extract": "Pulls signals with verbatim quotes; code checks every quote exists on the page.",
    "fit": "Maps signal → pain → capability; scores fit; checks target customer and buyer; "
           "recommends send, redirect or don't send.",
    "draft": "Writes the email from the fit thesis; every fact is tagged with its source.",
    "verify": "10 checks, code and AI; one automatic rewrite; never passes an unchecked draft.",
    "review": "The rep edits, re-checks, tries another angle, approves or rejects.",
}
st.dataframe(
    [{"Stage": f"{i}. {STAGE_LABELS[s]}", "What it does": WHAT[s.value]}
     for i, s in enumerate(STAGE_ORDER, start=1)],
    hide_index=True, width="stretch",
)

st.divider()
assumptions = Path(__file__).resolve().parent.parent / "docs" / "ASSUMPTIONS.md"
if assumptions.exists():
    st.markdown(assumptions.read_text().replace("# Assumptions and limitations", "## Assumptions and limitations", 1))
