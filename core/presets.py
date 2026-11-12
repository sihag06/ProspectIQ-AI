"""
Two demo seller profiles, deliberately different from each other, so
the same target company can be shown producing two different hooks.

Both are fictional products, so their proof points are invented for the
demo. Replace them, or add your own seller, in Settings.
"""
from __future__ import annotations

from core.db import list_seller_profiles, save_seller_profile
from core.models import SellerProfile

PRESETS: list[SellerProfile] = [
    SellerProfile(
        id="seller_ledgerline",
        name="Ledgerline",
        one_liner="AI agents that run accounts payable and finance operations end to end",
        target_customers=[
            "Mid-size and enterprise companies (200–5,000 employees)",
            "Companies processing hundreds of vendor invoices a month",
            "Finance teams scaling faster than headcount",
        ],
        target_buyer_roles=["CFO", "VP Finance", "Financial Controller", "Head of Accounts Payable"],
        problems_solved=[
            "Manual invoice entry and PO matching",
            "Slow month-end close",
            "Payment errors and duplicate payments",
            "Vendor onboarding and compliance checks done by hand",
        ],
        capabilities=[
            "Extracts data from invoices in any format, including scanned PDFs",
            "Matches invoices to purchase orders and flags mismatches",
            "Routes exceptions to the right approver with reasons",
            "Verifies new vendor details before first payment",
            "Works on top of existing ERPs without replacing them",
        ],
        differentiators=[
            "Handles exceptions with explained decisions, not just the clean cases",
            "Live in weeks, with no ERP migration",
        ],
        proof_points=[
            "A 900-person logistics customer cut invoice processing time from 6 days to 1",
        ],
        forbidden_claims=[
            "Guaranteed cost savings or ROI figures",
            "Replacing the finance team",
            "Any certification or compliance status not listed here",
        ],
        preferred_cta="A 20-minute walkthrough using a sample of their own invoices",
    ),
    SellerProfile(
        id="seller_talentloop",
        name="Talentloop",
        one_liner="AI recruiting coordinator that schedules, screens and follows up with candidates",
        target_customers=[
            "Fast-growing companies hiring 20+ people a quarter",
            "Talent teams of 2–15 recruiters",
        ],
        target_buyer_roles=["Head of Talent Acquisition", "VP People", "CHRO", "Recruiting Operations Lead"],
        problems_solved=[
            "Recruiters losing hours to interview scheduling",
            "Slow candidate response times during hiring surges",
            "Inconsistent first-round screening",
        ],
        capabilities=[
            "Schedules interviews across interviewer calendars automatically",
            "Runs structured first-round screens and summarises them",
            "Keeps candidates updated so none go silent",
            "Integrates with common applicant tracking systems",
        ],
        differentiators=[
            "Designed for hiring surges, not steady-state hiring",
            "Recruiters stay in control of every hiring decision",
        ],
        proof_points=[
            "A Series B fintech reduced time-to-schedule from 4 days to under 1",
        ],
        forbidden_claims=[
            "Making hiring decisions automatically",
            "Removing bias from hiring",
            "Specific time-to-hire guarantees",
        ],
        preferred_cta="A 15-minute call to look at their current hiring pipeline",
    ),
]


def seed_presets() -> None:
    """Add the presets the first time the app runs (or after a reset)."""
    existing = list_seller_profiles()
    if not existing:
        for profile in PRESETS:
            save_seller_profile(profile)
        return
    # One-time fix for presets saved by an earlier version, whose proof points
    # began with "Demo data:" (that label leaked into emails)
    for profile in existing:
        cleaned = [p.removeprefix("Demo data: ").removeprefix("Demo data:") for p in profile.proof_points]
        cleaned = [p[0].upper() + p[1:] if p else p for p in cleaned]
        if cleaned != profile.proof_points:
            profile.proof_points = cleaned
            save_seller_profile(profile)
