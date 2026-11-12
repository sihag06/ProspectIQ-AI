"""
Live run: every pipeline stage as it executes.

When a run is ready to continue, this page calls the orchestrator and
redraws a stage block every time that stage emits an event. When a run
is paused, it shows the decision the rep needs to make.
"""
import streamlit as st

from core.db import get_events, get_run, get_seller_profile, list_runs
from core.models import (
    STAGE_LABELS,
    STAGE_ORDER,
    EventType,
    IdentityStatus,
    RunStatus,
    Stage,
)
from core.orchestrator import HANDLERS, advance, can_advance, retry
from core.ui import local_time, render_draft, render_verification, render_fit, render_signals, render_sources, status_badge, status_label
from stages.s1_intake import IntakeError, create_run
from stages.s2_identity import choose_candidate, reject_identity, resolve_job_change

LIFECYCLE = {EventType.STARTED, EventType.COMPLETED, EventType.FAILED, EventType.PAUSED}

# ---------------------------------------------------------------------------
# Which run?
# ---------------------------------------------------------------------------

run_id = st.query_params.get("run") or st.session_state.get("run_id")

if not run_id:
    st.title("Live run")
    recent = list_runs(limit=20)
    if not recent:
        st.info("No runs yet. Start one from New research.")
        st.page_link("pages/new_run.py", label="Start new research", icon=":material/add_circle:")
        st.stop()
    choice = st.selectbox(
        "Open a recent run", recent,
        format_func=lambda r: f"{r['prospect_name']} · {r['company']} · {status_label(r['status'])}",
    )
    if st.button("Open run", type="primary"):
        st.query_params["run"] = choice["id"]
        st.rerun()
    st.stop()

run = get_run(run_id)
if run is None:
    st.error(f"Run {run_id} wasn't found. It may have been deleted.")
    st.stop()

st.session_state["run_id"] = run.id
p = run.request.prospect
seller = get_seller_profile(run.request.seller_profile_id)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

left, right = st.columns([3, 1], vertical_alignment="bottom")
with left:
    st.title(p.name)
    st.write(f"{p.title + ' at ' if p.title else ''}{p.company}"
             + (f" · selling {seller.name}" if seller else ""))
FINISHED = {RunStatus.READY, RunStatus.NEEDS_REVIEW, RunStatus.LOW_SIGNAL,
            RunStatus.APPROVED, RunStatus.REJECTED, RunStatus.FAILED}
with right:
    status_badge(run.status)
    if run.status in FINISHED:
        if st.button("Run again", icon=":material/replay:",
                     help="Starts a new run with the same prospect, seller and settings. "
                          "Cached searches make it faster."):
            try:
                again = create_run(run.request.prospect, run.request.seller_profile_id,
                                   run.request.provider, run.request.search_provider)
            except IntakeError as e:
                st.error(str(e))
                st.stop()
            st.session_state["run_id"] = again.id
            st.query_params["run"] = again.id
            st.rerun()
    st.caption(f"{run.request.provider.value} · {run.request.model} · {run.request.search_provider.value}"
               + (f" · {run.tokens_in + run.tokens_out:,} tokens" if run.tokens_in else ""))

# ---------------------------------------------------------------------------
# Decisions the rep needs to make
# ---------------------------------------------------------------------------

if run.status is RunStatus.NEEDS_INPUT and run.identity:
    ident = run.identity
    with st.container(border=True):
        if ident.status is IdentityStatus.AMBIGUOUS:
            st.subheader(f"Which {p.name} did you mean?")
            st.write(ident.explanation)
            options = list(range(len(ident.candidates)))

            def describe(i):
                c = ident.candidates[i]
                parts = [c.name, c.title, c.company]
                return " · ".join(x for x in parts if x) + (f" ({c.note})" if c.note else "")

            pick = st.radio("Matches found", options, format_func=describe, index=None)
            for i in options:
                if ident.candidates[i].evidence_url:
                    st.caption(f"Source for {ident.candidates[i].name}: {ident.candidates[i].evidence_url}")
            c1, c2 = st.columns([1, 3])
            if c1.button("Use this person", type="primary", disabled=pick is None):
                choose_candidate(run.id, pick)
                st.rerun()
            if c2.button("None of these: stop this run"):
                reject_identity(run.id)
                st.rerun()

        elif ident.status is IdentityStatus.ROLE_MISMATCH:
            st.subheader(f"{p.name} may have changed jobs")
            st.write(ident.explanation)
            for c in ident.candidates:
                if c.evidence_url:
                    st.caption(f"Source: {c.evidence_url}")
            c1, c2 = st.columns(2)
            if c1.button(f"Research {ident.current_company}", type="primary"):
                resolve_job_change(run.id, use_new_company=True)
                st.rerun()
            if c2.button(f"Keep {p.company}"):
                resolve_job_change(run.id, use_new_company=False)
                st.rerun()

if run.status is RunStatus.FAILED:
    with st.container(border=True):
        st.error("This run stopped with an error. The details are on the failed stage below.")
        if st.button("Retry from the failed stage", type="primary"):
            retry(run.id)
            st.rerun()

# ---------------------------------------------------------------------------
# Stage blocks
# ---------------------------------------------------------------------------

placeholders = {stage: st.empty() for stage in STAGE_ORDER}

# What each finished stage shows when expanded
DETAILS = {
    Stage.RESEARCH: render_sources,
    Stage.EXTRACT: render_signals,
    Stage.FIT: render_fit,
    Stage.DRAFT: render_draft,
    Stage.VERIFY: render_verification,
}


def draw_stage(stage: Stage) -> None:
    evs = [e for e in get_events(run.id) if e.stage == stage]
    i = STAGE_ORDER.index(stage) + 1
    label = f"{i}. {STAGE_LABELS[stage]}"
    lifecycle = [e for e in evs if e.type in LIFECYCLE]
    last = lifecycle[-1] if lifecycle else None

    with placeholders[stage].container():
        if last is None:
            with st.container(border=True):
                st.markdown(f"<span style='opacity:.55'>{label}</span>", unsafe_allow_html=True)
                if stage == run.current_stage and stage not in HANDLERS:
                    st.caption("Built in an upcoming step")
            return

        if last.type is EventType.PAUSED:
            with st.container(border=True):
                st.markdown(f":orange[:material/front_hand: **{label}** — {last.message}]")
                if stage is Stage.REVIEW:
                    st.page_link("pages/review.py", label="Open the review page",
                                 icon=":material/rate_review:", query_params={"run": run.id})
                for e in evs:
                    st.caption(f"{local_time(e.at)}  {e.message}")
            return

        state = {EventType.STARTED: "running", EventType.COMPLETED: "complete",
                 EventType.FAILED: "error"}[last.type]
        title = label if last.type is EventType.STARTED else f"{label} — {last.message}"
        # Errors and the fit verdict open by default: they're what the rep needs to see
        expand = state == "error" or (stage in (Stage.FIT, Stage.DRAFT, Stage.VERIFY) and state == "complete")
        with st.status(title, state=state, expanded=expand):
            for e in evs:
                st.write(f"{local_time(e.at)}  {e.message}")
            if last.type is EventType.COMPLETED and stage in DETAILS:
                st.divider()
                DETAILS[stage](get_run(run.id))


for stage in STAGE_ORDER:
    draw_stage(stage)

# ---------------------------------------------------------------------------
# Run the pipeline when the run is ready to continue
# ---------------------------------------------------------------------------

if can_advance(run):
    advance(run.id, on_event=lambda e: draw_stage(e.stage))
    st.rerun()
