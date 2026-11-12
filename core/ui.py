"""Small shared UI helpers so every page shows statuses the same way."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from core.models import RunStatus


def local_time(dt: datetime | str, fmt: str = "%H:%M:%S") -> str:
    """
    Times are stored in UTC. Show them in the viewer's own timezone, read from
    their browser, so it's right on a laptop in India or a server in the US.
    """
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    try:
        tz = ZoneInfo(st.context.timezone) if st.context.timezone else None
    except Exception:
        tz = None
    return (dt.astimezone(tz) if tz else dt.astimezone()).strftime(fmt)

STATUS_STYLE: dict[RunStatus, tuple[str, str]] = {
    RunStatus.CREATED: ("Queued", "gray"),
    RunStatus.RUNNING: ("In progress", "blue"),
    RunStatus.NEEDS_INPUT: ("Needs your input", "orange"),
    RunStatus.LOW_SIGNAL: ("Low signal", "yellow"),
    RunStatus.NEEDS_REVIEW: ("Needs review", "orange"),
    RunStatus.READY: ("Ready for review", "violet"),
    RunStatus.APPROVED: ("Approved", "green"),
    RunStatus.REJECTED: ("Rejected", "gray"),
    RunStatus.FAILED: ("Failed", "red"),
}


def status_badge(status: RunStatus | str) -> None:
    label, color = STATUS_STYLE[RunStatus(status)]
    st.badge(label, color=color)


def status_label(status: RunStatus | str) -> str:
    return STATUS_STYLE[RunStatus(status)][0]


# ---------------------------------------------------------------------------
# Research and evidence display (used by the live view and, later, review)
# ---------------------------------------------------------------------------

from core.models import About, Run, SourceCategory  # noqa: E402

CATEGORY_LABELS = {
    SourceCategory.OVERVIEW: "Company overview",
    SourceCategory.NEWS: "News",
    SourceCategory.FUNDING: "Funding and expansion",
    SourceCategory.HIRING: "Hiring",
    SourceCategory.TECHNOLOGY: "Technology",
    SourceCategory.CHALLENGES: "Challenges",
    SourceCategory.LEADERSHIP: "Leadership and the prospect",
}

_MD_SPECIAL = "\\`*_[]<>#|$~"


def md_escape(text: str) -> str:
    """Stop scraped text from being read as Markdown or LaTeX."""
    out = "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in text)
    return out.replace("\n", " ")


def evidence(quote: str) -> str:
    """The highlighter treatment, reserved for verbatim evidence."""
    return f":yellow-background[\u201c{md_escape(quote)}\u201d]"


def render_sources(run: Run) -> None:
    if not run.sources:
        st.write("No usable sources.")
        return
    for cat in SourceCategory:
        items = [s for s in run.sources if s.category is cat]
        if not items:
            continue
        st.markdown(f"**{CATEGORY_LABELS[cat]}**")
        lines = []
        for s in items:
            meta = [s.source_type.value.replace("_", " ")]
            if s.published_at:
                meta.append(s.published_at.strftime("%d %b %Y"))
            if s.about is About.PERSON:
                meta.append("mentions prospect")
            lines.append(f"- [{md_escape(s.title or s.url)}]({s.url}) — {', '.join(meta)}")
        st.markdown("\n".join(lines))


def render_signals(run: Run) -> None:
    snap = run.snapshot
    if snap and snap.what_they_do:
        st.markdown(f"**What {md_escape(run.request.prospect.company)} does:** "
                    f"{md_escape(snap.what_they_do)}")
        if snap.products:
            st.caption("Products: " + md_escape(", ".join(snap.products)))
    if not run.signals:
        st.write("No verified signals.")
        return
    sources = {s.id: s for s in run.sources}
    for cat in SourceCategory:
        items = [g for g in run.signals if g.category is cat]
        if not items:
            continue
        st.markdown(f"**{CATEGORY_LABELS[cat]}**")
        for g in items:
            src = sources.get(g.source_id)
            when = f" · {g.published_at.strftime('%b %Y')}" if g.published_at else ""
            who = " · about the prospect" if g.about is About.PERSON else ""
            link = f"[source]({src.url})" if src else ""
            st.markdown(f"{md_escape(g.description)}  \n{evidence(g.evidence_quote)}")
            st.caption(f"{link}{when}{who}")


# ---------------------------------------------------------------------------
# Fit card (Stage 5)
# ---------------------------------------------------------------------------

from core.models import FitCandidate, FitVerdict  # noqa: E402

VERDICT_STYLE = {
    FitVerdict.STRONG: ("Strong fit", "green"),
    FitVerdict.MODERATE: ("Moderate fit", "blue"),
    FitVerdict.WEAK: ("Weak fit", "orange"),
}


def _score_rows(c: FitCandidate) -> None:
    s = c.score
    rows = [
        ("Product fit", (s.fit - 1) / 4, f"{s.fit}/5 · AI-rated"),
        ("Business impact", (s.business_impact - 1) / 4, f"{s.business_impact}/5 · AI-rated"),
        ("Freshness", s.freshness / 100, f"{s.freshness:.0f}/100 · from date"),
        ("Evidence quality", s.evidence_quality / 100, f"{s.evidence_quality:.0f}/100 · from source type"),
    ]
    for name, value, note in rows:
        a, b = st.columns([2, 3], vertical_alignment="center")
        a.caption(f"{name} — {note}")
        b.progress(min(max(value, 0.0), 1.0))


def render_fit(run: Run) -> None:
    fit = run.fit
    if fit is None:
        return
    label, color = VERDICT_STYLE[fit.verdict]
    signals = {g.id: g for g in run.signals}
    sources = {s.id: s for s in run.sources}

    st.badge(label, color=color)
    if fit.recommendation == "hold":
        st.markdown(f":red[:material/do_not_disturb_on:] **{md_escape(fit.recommendation_note)}**")
    elif fit.recommendation == "redirect":
        st.markdown(f":orange[:material/forward_to_inbox:] **{md_escape(fit.recommendation_note)}**")
    c = fit.chosen
    if c is None:
        st.write("No development at this company connects clearly to the product. "
                 "The email will use a softer, company-level angle and is labelled low personalisation.")
        return

    st.markdown(f"**{c.score.total:.0f}/100** · {md_escape(c.thesis)}")
    g = signals.get(c.signal_id)
    if g:
        src = sources.get(g.source_id)
        st.markdown(evidence(g.evidence_quote))
        if src:
            st.caption(f"[{md_escape(src.title or src.url)}]({src.url})"
                       + (f" · {g.published_at.strftime('%d %b %Y')}" if g.published_at else ""))
    st.markdown(f"**Likely pain:** {md_escape(c.likely_pain)}  \n"
                f"**Matching capability:** {md_escape(c.matched_capability)}  \n"
                f"**Expected outcome:** {md_escape(c.expected_outcome)}")
    _score_rows(c)
    st.caption(md_escape(c.reasoning))

    if fit.icp_note:
        icon = {"yes": ":green[:material/check_circle:]", "partly": ":orange[:material/adjust:]",
                "no": ":orange[:material/block:]"}.get(fit.icp_match, "")
        st.markdown(f"{icon} **Target customer ({fit.icp_match}):** {md_escape(fit.icp_note)}")

    if fit.buyer_note:
        if fit.buyer_owns_pain:
            st.markdown(f":green[:material/check_circle:] {md_escape(fit.buyer_note)}")
        else:
            role = f" Better recipient: **{md_escape(fit.suggested_recipient_role)}**." \
                if fit.suggested_recipient_role else ""
            st.markdown(f":orange[:material/person_alert:] {md_escape(fit.buyer_note)}{role}")

    if fit.runner_up:
        r = fit.runner_up
        st.markdown(f"**Runner-up ({r.score.total:.0f}/100):** {md_escape(r.thesis)}")

    if len(fit.ranked) > 2:
        st.markdown("**All assessed angles**")
        st.dataframe(
            [{"Score": round(x.score.total), "Fit": x.score.fit, "Impact": x.score.business_impact,
              "Pain": x.likely_pain, "Capability": x.matched_capability} for x in fit.ranked],
            hide_index=True, width="stretch",
        )
    if fit.excluded:
        st.caption(f"Set aside as hooks: {len(fit.excluded)} sensitive signals ("
                   + ", ".join(sorted({e.reason for e in fit.excluded})) + ")")


# ---------------------------------------------------------------------------
# Draft (Stage 6)
# ---------------------------------------------------------------------------

PERSONALISATION_STYLE = {
    "person": ("Personal to the prospect", "green"),
    "company": ("Company-specific", "blue"),
    "low": ("Low personalisation", "orange"),
}


def md_escape_block(text: str) -> str:
    """Like md_escape, but keeps line breaks (for email bodies)."""
    out = "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in text)
    return "  \n".join(out.splitlines())


def render_draft(run: Run) -> None:
    d = run.latest_draft
    if d is None:
        return
    label, color = PERSONALISATION_STYLE.get(d.personalisation, ("", "gray"))
    st.badge(label, color=color)
    with st.container(border=True):
        st.markdown(f"**Subject:** {md_escape(d.subject)}")
        st.markdown(md_escape_block(d.body))
    st.caption(f"{d.word_count} words · {len(d.claims)} factual claims · version {d.version}")
    render_claims(run, d)


def render_claims(run: Run, d) -> None:
    """Every factual claim in a draft, with the source or profile field behind it."""
    if not d.claims:
        return
    sources = {s.id: s for s in run.sources}
    st.markdown("**What each claim rests on**")
    for c in d.claims:
        if c.support_type.value == "source" and c.support_ref in sources:
            src = sources[c.support_ref]
            ref = f"[{md_escape(src.title or src.url)}]({src.url})"
        elif c.support_type.value == "source":
            ref = f":red[unknown source {md_escape(c.support_ref)}]"
        else:
            ref = f"seller profile · {md_escape(c.support_ref.replace('_', ' '))}"
        st.markdown(f"- {md_escape(c.text)} · {ref}")
    if d.angle == "rep_edit":
        st.caption("You edited this version, so these claims come from the version before your edits. "
                   "Use Save and re-check to confirm they still hold.")


# ---------------------------------------------------------------------------
# Verification (Stage 7)
# ---------------------------------------------------------------------------

def render_verification(run: Run) -> None:
    v = run.verification
    if v is None:
        return
    if v.passed:
        st.badge("Verified" + (" after one rewrite" if v.revised else ""), color="green")
    else:
        st.badge("Needs your review", color="orange")
    for c in v.checks:
        icon = ":green[:material/check:]" if c.passed else ":red[:material/close:]"
        line = f"{icon} **{md_escape(c.name)}**"
        if c.detail and (not c.passed or c.name.startswith(("Length", "Answers"))):
            line += f" — {md_escape(c.detail)}"
        st.markdown(line)
    if v.revised:
        st.caption("The first draft failed some checks and was rewritten once. "
                   "Email generation shows the rewritten version.")
