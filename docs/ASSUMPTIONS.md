# Assumptions and limitations

The brief says to treat ambiguity as part of the exercise: make an assumption, note it, and move on. These are the assumptions ProspectIQ is built on.

## The user and the workflow

- **The user is a sales rep (SDR or AE) working one prospect at a time.** They type a name and company; ProspectIQ does the research. Bulk lists are out of scope for this version.
- **The rep knows what they sell.** Product–company fit is only as good as the seller profile, so ProspectIQ asks for problems solved, capabilities, buyer roles, proof points and claims to avoid, rather than a one-line description.
- **Nothing is ever sent automatically.** The furthest an email gets is approved and copied by a human. This is a deliberate product decision, not a missing feature.
- **One rep, one workspace.** There are no user accounts. Run history lives in a local SQLite database.

## Research

- **Public web only, through a search API (Tavily).** ProspectIQ does not scrape LinkedIn or any site that forbids it. LinkedIn pages appear only when a search engine has indexed them.
- **Many prospects have no public footprint.** When the person can't be verified, ProspectIQ says so and researches the company instead, rather than inventing personal details.
- **The rep's input can be wrong.** Names pasted from LinkedIn ("Priya Raman Acme"), outdated titles and job changes are expected, so identity resolution checks and corrects them, but only when a real source backs the correction.
- **Company names can be ambiguous.** Pages about a different organisation, or an everyday phrase with the same name, are removed before they can influence the email.

## Judgment

- **Fit scoring weights are a reasoned starting point, not a calibrated model:** S = 0.35·Fit + 0.30·Business impact + 0.20·Freshness + 0.15·Evidence quality. With real reply data, they would be tuned.
- **Freshness halves every 120 days.** Undated sources score 40 out of 100.
- **Verdict thresholds:** 75 and above is a strong fit, 55–74 moderate, below 55 weak. A fit rating of 2 or lower is always weak, however fresh the news.
- **A strong fit requires the company to clearly match the seller's target customers.** The model may use widely known facts about a company's size for this check only; those facts never appear in the email.
- **Sensitive news is never a sales hook.** Layoffs, lawsuits, investigations, deaths, financial distress, security breaches and scandals are detected by keyword patterns. That list is deliberately conservative and English-only.
- **Generic facts are weak evidence.** "Has a contact form" or "has a careers page" is true of almost any company, so it can't support a strong fit.

## The email

- **A first-touch email:** 120 words at most (aiming for 70–100), one light ask that checks interest, and no fixed meeting lengths.
- **Every factual statement must point to a source or to the seller profile.** Reasoning about likely pain is allowed, but only when framed as reasoning, not as a known fact about the company.
- **Emails are written in English.**

## Verification

- **AI verification reduces errors; it doesn't eliminate them.** That's why the code checks run first, the AI verifier uses a different provider when one is available, failing drafts are rewritten once and then shown to the rep, and a human always approves.
- **If the AI verifier can't run, the draft is marked unchecked,** never passed.

## Operations

- **Search results are cached for 72 hours,** or indefinitely in demo mode, so reruns are fast and repeatable.
- **Costs are tracked in tokens.** Prices aren't hard-coded because they change; add current prices in `core/config.py` to see dollar costs.
- **On Streamlit Community Cloud, the local database resets when the app restarts.** A production version would use a hosted database.
