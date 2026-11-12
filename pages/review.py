"""
Review: the human-in-the-loop step. Nothing leaves ProspectIQ until the rep
approves it, and even then it's copied by the rep, never sent automatically.
"""
import streamlit as st

from core.db import get_run, list_runs, save_run
from core.events import emit
from core.models import Draft, EventType, FitVerdict, RunStatus, Stage
from core.ui import (
    PERSONALISATION_STYLE,
    VERDICT_STYLE,
    local_time,
    md_escape,
    render_claims,
    render_fit,
    render_verification,
    status_badge,
    status_label,
)
from providers.base import ProviderError
from search.tavily_search import SearchError
from stages import s6_draft, s7_verify

REVIEWABLE = {RunStatus.READY, RunStatus.NEEDS_REVIEW, RunStatus.LOW_SIGNAL}
DONE = {RunStatus.APPROVED, RunStatus.REJECTED}
REJECT_REASONS = ["Tone or wording", "Inaccurate or unsupported", "Too generic",
                  "Wrong person or company", "Fit is too weak", "Not the right time", "Other"]
# Reasons about the wording can be fixed by a rewrite; reasons about the prospect can't
REWRITABLE = {"Tone or wording", "Inaccurate or unsupported", "Too generic", "Other"}

# ---------------------------------------------------------------------------
# Which run?
# ---------------------------------------------------------------------------

run_id = st.query_params.get("run") or st.session_state.get("run_id")
run = get_run(run_id) if run_id else None

if run is None or run.status not in REVIEWABLE | DONE:
    st.title("Review")
    waiting = [r for r in list_runs() if r["status"] in {s.value for s in REVIEWABLE | DONE}]
    if run is not None:
        st.info(f"{run.request.prospect.name}'s run isn't ready for review yet "
                f"({status_label(run.status).lower()}). It's still on the live view.")
        st.page_link("pages/live_run.py", label="Open live view", icon=":material/play_circle:",
                      query_params={"run": run.id})
    if not waiting:
        st.write("Drafts appear here once a run finishes verification.")
        st.stop()
    choice = st.selectbox(
        "Drafts", waiting,
        format_func=lambda r: f"{r['prospect_name']} · {r['company']} · {status_label(r['status'])}")
    if st.button("Open", type="primary"):
        st.query_params["run"] = choice["id"]
        st.session_state["run_id"] = choice["id"]
        st.rerun()
    st.stop()

st.session_state["run_id"] = run.id
p = run.request.prospect
fit = run.fit
d = run.latest_draft

# ---------------------------------------------------------------------------
# Header and alerts
# ---------------------------------------------------------------------------

st.title(p.name)
st.write(f"{p.title + ' at ' if p.title else ''}{p.company}")
badges = st.container(horizontal=True)
with badges:
    status_badge(run.status)
    if fit:
        label, color = VERDICT_STYLE[fit.verdict]
        st.badge(label, color=color)
    if d:
        label, color = PERSONALISATION_STYLE.get(d.personalisation, ("", "gray"))
        st.badge(label, color=color)

def _short(text: str, limit: int = 180) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


if run.status not in DONE:
    if fit and fit.recommendation == "hold":
        st.error(f"**{fit.recommendation_note}** The draft is here if you disagree, "
                 f"but a weak touch can cost you this prospect later.", icon=":material/do_not_disturb_on:")
    elif fit and fit.recommendation == "redirect":
        st.info(f"**{fit.recommendation_note}** {_short(fit.buyer_note)}",
                icon=":material/forward_to_inbox:")
    if run.status is RunStatus.NEEDS_REVIEW and run.verification:
        failing = [c for c in run.verification.checks if not c.passed]
        st.warning("**Check before approving**\n\n" + "\n".join(
            f"- **{c.name}:** {md_escape(_short(c.detail))}" for c in failing),
            icon=":material/fact_check:")
    elif fit and fit.verdict is FitVerdict.WEAK and fit.recommendation != "hold":
        st.info("Weak fit: the email asks rather than asserts, because the evidence for a need is thin.")


def save_and_note(stage: Stage, message: str) -> None:
    save_run(run)
    emit(run.id, stage, EventType.PROGRESS, message)


def verify_latest(rewrite: bool) -> None:
    """Verify the newest draft. With rewrite=True, the full Stage 7 (one automatic fix)."""
    if rewrite:
        result = s7_verify.run(run)
        run.status = result.status
        emit(run.id, Stage.VERIFY, EventType.COMPLETED, result.summary)
    else:
        from core.db import get_seller_profile
        report, _ = s7_verify.verify(run, run.latest_draft, get_seller_profile(run.request.seller_profile_id))
        run.verification = report
        ok = report.passed
        weak = fit and fit.verdict is FitVerdict.WEAK
        run.status = (RunStatus.LOW_SIGNAL if weak else RunStatus.READY) if ok else RunStatus.NEEDS_REVIEW
        failed = sum(not c.passed for c in report.checks)
        emit(run.id, Stage.VERIFY, EventType.COMPLETED,
             "Your edits passed every check" if ok else f"Your edits: {failed} check{'s' * (failed > 1)} failing")
    save_run(run)


left, right = st.columns([1, 1], gap="large")

# ---------------------------------------------------------------------------
# Left: the email
# ---------------------------------------------------------------------------

with left:
    if run.status is RunStatus.APPROVED:
        st.success("Approved. Copy it with the icon in the top-right corner of the box.")
        st.code(run.final_email or "", language=None, wrap_lines=True)
        st.download_button("Download as text", run.final_email or "",
                           file_name=f"{p.name.lower().replace(' ', '-')}-email.txt",
                           icon=":material/download:")
    elif run.status is RunStatus.REJECTED:
        st.error(f"Rejected: {run.rejection_reason or 'no reason given'}")
        reason = (run.rejection_reason or "").split(":")[0]
        if reason == "Wrong person or company":
            st.caption("Start a new run with the corrected name or company; identity resolution will check it.")
        st.caption("Rejection reasons are collected on the dashboard, so patterns show which "
                   "prompts or seller profiles need work.")
        if d:
            st.markdown(f"**Subject:** {md_escape(d.subject)}")
            st.text(d.body)

    if run.status in DONE:
        if st.button("Reopen for editing", icon=":material/undo:"):
            ok = run.verification and run.verification.passed
            run.status = RunStatus.READY if ok else RunStatus.NEEDS_REVIEW
            save_run(run)
            emit(run.id, Stage.REVIEW, EventType.PAUSED, "Reopened for review")
            st.rerun()

    else:
        key = f"{run.id}_{d.version}"  # new version -> fresh text boxes
        subject = st.text_input("Subject", d.subject, key=f"subject_{key}")
        body = st.text_area("Email", d.body, height=440, key=f"body_{key}")
        edited = subject.strip() != d.subject or body.strip() != d.body
        st.caption(f"{len(body.split())} words · version {d.version}"
                   + (" · unsaved edits" if edited else ""))

        def save_edits() -> None:
            run.drafts.append(Draft(
                version=d.version + 1, subject=subject.strip(), body=body.strip(), claims=d.claims,
                based_on_signal_id=d.based_on_signal_id, personalisation=d.personalisation, angle="rep_edit"))
            emit(run.id, Stage.REVIEW, EventType.PROGRESS, f"You edited the draft (version {d.version + 1})")

        actions = st.container(horizontal=True)
        needs_review = run.status is RunStatus.NEEDS_REVIEW or (fit and fit.recommendation == "hold")
        if actions.button("Approve anyway" if needs_review else "Approve", type="primary",
                          icon=":material/check:"):
            if edited:
                save_edits()
            final = run.latest_draft
            run.final_email = f"Subject: {final.subject}\n\n{final.body}"
            run.status = RunStatus.APPROVED
            save_run(run)
            emit(run.id, Stage.REVIEW, EventType.COMPLETED,
                 "Approved by you" + (" with edits" if edited else "")
                 + (" despite failing checks" if needs_review else ""))
            st.rerun()

        if actions.button("Save and re-check", disabled=not edited, icon=":material/fact_check:",
                          help="Saves your edits, then runs the verification checks on them."):
            save_edits()
            with st.spinner("Checking your edits…"):
                try:
                    verify_latest(rewrite=False)
                except (ProviderError, SearchError) as e:
                    save_run(run)
                    st.error(str(e))
                    st.stop()
            st.rerun()

        has_runner_up = bool(fit and fit.runner_up)
        if actions.button("Try the runner-up angle", disabled=not has_runner_up or d.angle == "runner_up",
                          icon=":material/swap_horiz:",
                          help="Writes and verifies a new draft from the second-best fit."
                          if has_runner_up else "No second angle was found for this company."):
            with st.spinner("Writing and checking a draft from the runner-up angle…"):
                try:
                    new = s6_draft.generate(run, angle="runner_up")
                    run.drafts.append(new)
                    emit(run.id, Stage.DRAFT, EventType.PROGRESS,
                         f"Draft {new.version} from the runner-up angle: {new.word_count} words")
                    verify_latest(rewrite=True)
                except (ProviderError, SearchError) as e:
                    save_run(run)
                    st.error(str(e))
                    st.stop()
            st.rerun()

        with actions.popover("Reject or request changes", icon=":material/close:"):
            reason = st.selectbox("What's wrong?", REJECT_REASONS)
            note = st.text_area("Tell it what to change" if reason in REWRITABLE else "Note (optional)",
                                placeholder="e.g. Softer opening, and don't mention the funding round",
                                height=90)
            feedback = reason + (f": {note.strip()}" if note.strip() else "")
            if reason in REWRITABLE:
                if st.button("Rewrite with this feedback", type="primary", icon=":material/edit_note:"):
                    with st.spinner("Rewriting and checking…"):
                        try:
                            base = run.latest_draft
                            new = s6_draft.generate(
                                run, angle=base.angle if base.angle != "rep_edit" else "chosen",
                                previous=base, problems=[f"The sales rep asked for changes. {feedback}"])
                            run.drafts.append(new)
                            emit(run.id, Stage.REVIEW, EventType.PROGRESS,
                                 f"You asked for changes ({feedback}); draft {new.version} written")
                            verify_latest(rewrite=False)
                        except (ProviderError, SearchError) as e:
                            save_run(run)
                            st.error(str(e))
                            st.stop()
                    st.rerun()
            else:
                st.caption("This is about the prospect, not the wording, so a rewrite won't fix it.")
            if st.button("Reject and close", type="secondary" if reason in REWRITABLE else "primary"):
                run.status = RunStatus.REJECTED
                run.rejection_reason = feedback
                save_run(run)
                emit(run.id, Stage.REVIEW, EventType.COMPLETED, f"Rejected: {feedback}")
                st.rerun()

# ---------------------------------------------------------------------------
# Right: why this email, and how it was checked
# ---------------------------------------------------------------------------

with right:
    tab_why, tab_checks, tab_versions = st.tabs(["Why", "Checks", "Versions"])
    with tab_why:
        if fit and fit.chosen and d.angle != "runner_up":
            st.markdown(f"**The angle:** {md_escape(fit.chosen.thesis)}")
        elif fit and fit.runner_up and d.angle == "runner_up":
            st.markdown(f"**The angle (runner-up):** {md_escape(fit.runner_up.thesis)}")
        render_claims(run, d)
        st.page_link("pages/live_run.py", label="See the full research", icon=":material/travel_explore:",
                     query_params={"run": run.id})
    with tab_checks:
        render_verification(run)
    with tab_versions:
        names = {"chosen": "AI draft", "runner_up": "Runner-up angle", "rep_edit": "Your edit"}
        for v in reversed(run.drafts):
            with st.expander(f"Version {v.version} · {names.get(v.angle, v.angle)} · "
                             f"{v.word_count} words · {local_time(v.created_at, '%H:%M')}"):
                st.markdown(f"**Subject:** {md_escape(v.subject)}")
                st.text(v.body)
