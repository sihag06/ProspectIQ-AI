"""Dashboard: every run, what happened, and what needs the rep's attention."""
import streamlit as st

from core.db import delete_run, list_runs, list_seller_profiles
from core.models import RunStatus
from core.ui import STATUS_STYLE, local_time, status_label

st.title("Dashboard")
runs = list_runs(limit=500)
if not runs:
    st.info("No runs yet. Start one from New research.")
    st.page_link("pages/new_run.py", label="Start new research", icon=":material/add_circle:")
    st.stop()

sellers = {s.id: s.name for s in list_seller_profiles()}
waiting = {RunStatus.READY.value, RunStatus.NEEDS_REVIEW.value, RunStatus.LOW_SIGNAL.value}
scores = [r["score"] for r in runs if r["score"] is not None]

m = st.columns(4)
m[0].metric("Runs", len(runs))
m[1].metric("Waiting for review", sum(r["status"] in waiting for r in runs))
m[2].metric("Approved", sum(r["status"] == RunStatus.APPROVED.value for r in runs))
m[3].metric("Average fit score", f"{sum(scores) / len(scores):.0f}" if scores else "—")

# Filters
f1, f2, f3 = st.columns([3, 1.2, 1.5], vertical_alignment="bottom")
present = [s for s in RunStatus if any(r["status"] == s.value for r in runs)]
chosen = f1.pills("Status", present, selection_mode="multi", format_func=lambda s: STATUS_STYLE[s][0])
seller = f2.selectbox("Seller", ["All"] + sorted(set(sellers.values())))
query = f3.text_input("Search", placeholder="Name or company")

rows = []
for r in runs:
    if chosen and r["status"] not in {c.value for c in chosen}:
        continue
    seller_name = sellers.get(r["seller_profile_id"], "Deleted seller")
    if seller != "All" and seller_name != seller:
        continue
    if query and query.lower() not in f"{r['prospect_name']} {r['company']}".lower():
        continue
    rows.append(r)

if not rows:
    st.write("No runs match these filters.")
    st.stop()

# Send advice lives inside each run; load it only for the rows being shown
from core.db import get_run  # noqa: E402
ADVICE = {}
REJECTIONS: dict[str, int] = {}
for r in rows[:200]:
    full = get_run(r["id"])
    if full and full.status is RunStatus.REJECTED and full.rejection_reason:
        key = full.rejection_reason.split(":")[0]
        REJECTIONS[key] = REJECTIONS.get(key, 0) + 1
    if full and full.fit:
        ADVICE[r["id"]] = {"send": "Send", "redirect": "Redirect", "hold": "Don't send"}[full.fit.recommendation]

table = [{
    "Prospect": r["prospect_name"],
    "Company": r["company"],
    "Seller": sellers.get(r["seller_profile_id"], "Deleted seller"),
    "Status": status_label(r["status"]),
    "Fit": (r["fit_verdict"] or "").capitalize() or "—",
    "Advice": ADVICE.get(r["id"], "—"),
    "Score": r["score"],
    "AI": r["provider"],
    "Cost": f"${r['cost_usd']:.3f}" if r["cost_usd"] else "—",
    "Started": local_time(r["created_at"], "%d %b, %H:%M"),
} for r in rows]

event = st.dataframe(
    table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
    column_config={"Score": st.column_config.ProgressColumn("Fit score", min_value=0, max_value=100,
                                                            format="%.0f")},
)
if REJECTIONS:
    with st.expander(f"Why drafts were rejected ({sum(REJECTIONS.values())})"):
        st.bar_chart(REJECTIONS, horizontal=True, x_label="Drafts", y_label="")
        st.caption("Wording problems point to the writing prompts; prospect problems point to "
                   "research, fit analysis or the seller profile.")

st.caption("Select a row to open it. Cost appears once model prices are set in core/config.py.")

picked = event.selection.rows
if picked:
    r = rows[picked[0]]
    with st.container(border=True):
        st.markdown(f"**{r['prospect_name']}** · {r['company']} · {status_label(r['status'])}")
        bar = st.container(horizontal=True)
        bar.page_link("pages/live_run.py", label="Open live view", icon=":material/play_circle:",
                      query_params={"run": r["id"]})
        if r["status"] in waiting | {RunStatus.APPROVED.value, RunStatus.REJECTED.value}:
            bar.page_link("pages/review.py", label="Open review", icon=":material/rate_review:",
                          query_params={"run": r["id"]})
        with bar.popover("Delete", icon=":material/delete:"):
            st.write("This removes the run and its history. It can't be undone.")
            if st.button("Delete run", type="primary"):
                delete_run(r["id"])
                st.rerun()
