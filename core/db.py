"""
ProspectIQ storage layer (SQLite).

Design choice: each Run is stored as one JSON document, plus a few
plain columns copied out of it (status, company, score...) so the
dashboard can filter and sort without loading every run in full.

Why not separate tables for sources, signals and drafts? Those are only
ever read as part of their run, so one document per run is simpler,
keeps saves atomic, and makes "resume from the last stage" trivial:
load the run, look at current_stage, continue.

Events get their own table because they are appended constantly while
a run is live, and the live view reads them in order.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from core.models import (
    RunEvent,
    Run,
    RunStatus,
    SellerProfile,
    utc_now,
)

DEFAULT_DB_PATH = Path("data") / "prospectiq.db"


def get_db_path() -> Path:
    """Read on every call so tests can point at a temporary database."""
    return Path(os.environ.get("PROSPECTIQ_DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """
    A fresh connection per operation. Streamlit runs code on several
    threads, and SQLite connections must not be shared across threads,
    so short-lived connections are the safe default.
    """
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # reads don't block while a run is writing
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS seller_profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    current_stage     TEXT NOT NULL,
    prospect_name     TEXT NOT NULL,
    company           TEXT NOT NULL,
    seller_profile_id TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    fit_verdict       TEXT,
    score             REAL,
    cost_usd          REAL NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    data              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage    TEXT NOT NULL,
    type     TEXT NOT NULL,
    message  TEXT NOT NULL,
    data     TEXT NOT NULL,
    at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every app start."""
    with _connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Seller profiles
# ---------------------------------------------------------------------------

def save_seller_profile(profile: SellerProfile) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO seller_profiles (id, name, data, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                data = excluded.data,
                updated_at = excluded.updated_at
            """,
            (profile.id, profile.name, profile.model_dump_json(), utc_now().isoformat()),
        )


def get_seller_profile(profile_id: str) -> SellerProfile | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM seller_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    return SellerProfile.model_validate_json(row["data"]) if row else None


def list_seller_profiles() -> list[SellerProfile]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data FROM seller_profiles ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [SellerProfile.model_validate_json(r["data"]) for r in rows]


def delete_seller_profile(profile_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM seller_profiles WHERE id = ?", (profile_id,))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def save_run(run: Run) -> None:
    """Insert or update a run. Called after every stage."""
    run.touch()
    fit_verdict = run.fit.verdict.value if run.fit else None
    score = run.fit.chosen.score.total if run.fit and run.fit.chosen else None
    p = run.request.prospect

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (
                id, status, current_stage, prospect_name, company,
                seller_profile_id, provider, model, fit_verdict, score,
                cost_usd, created_at, updated_at, data
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status        = excluded.status,
                current_stage = excluded.current_stage,
                prospect_name = excluded.prospect_name,
                company       = excluded.company,
                fit_verdict   = excluded.fit_verdict,
                score         = excluded.score,
                cost_usd      = excluded.cost_usd,
                updated_at    = excluded.updated_at,
                data          = excluded.data
            """,
            (
                run.id,
                run.status.value,
                run.current_stage.value,
                p.name,
                p.company,
                run.request.seller_profile_id,
                run.request.provider.value,
                run.request.model,
                fit_verdict,
                score,
                run.cost_usd,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
                run.model_dump_json(),
            ),
        )


def get_run(run_id: str) -> Run | None:
    with _connect() as conn:
        row = conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
    return Run.model_validate_json(row["data"]) if row else None


def list_runs(status: RunStatus | None = None, limit: int = 200) -> list[dict]:
    """
    Lightweight rows for the dashboard. Does not load the full run
    documents, so it stays fast as history grows.
    """
    query = """
        SELECT id, status, current_stage, prospect_name, company,
               seller_profile_id, provider, model, fit_verdict, score,
               cost_usd, created_at, updated_at
        FROM runs
    """
    params: tuple = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status.value,)
    query += " ORDER BY created_at DESC LIMIT ?"
    params += (limit,)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def delete_run(run_id: str) -> None:
    """Also removes the run's events (ON DELETE CASCADE)."""
    with _connect() as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def add_event(event: RunEvent) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO events (run_id, stage, type, message, data, at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.stage.value,
                event.type.value,
                event.message,
                json.dumps(event.data, default=str),
                event.at.isoformat(),
            ),
        )


def get_events(run_id: str) -> list[RunEvent]:
    """All events for a run, oldest first. Used for live view and replay."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, stage, type, message, data, at FROM events "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [
        RunEvent(
            run_id=r["run_id"],
            stage=r["stage"],
            type=r["type"],
            message=r["message"],
            data=json.loads(r["data"]),
            at=r["at"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Self-test: run with `python -m core.db`
# Uses a throwaway database, so your real data is never touched.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    from core.models import (
        EventType,
        ProspectInput,
        Provider,
        RunRequest,
        Stage,
    )

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["PROSPECTIQ_DB_PATH"] = str(Path(tmp) / "test.db")
        init_db()
        print("1. Tables created at", get_db_path())

        seller = SellerProfile(name="FlowOps", one_liner="AI workflow automation")
        save_seller_profile(seller)
        seller.one_liner = "AI workflow automation for finance teams"
        save_seller_profile(seller)  # update, not duplicate
        profiles = list_seller_profiles()
        print("2. Seller profiles:", len(profiles), "| updated one-liner:",
              profiles[0].one_liner == seller.one_liner)

        run = Run(
            request=RunRequest(
                prospect=ProspectInput(name="Alex Johnson", company="Acme Technologies"),
                seller_profile_id=seller.id,
                provider=Provider.OPENAI,
                model="gpt-model-name",
            )
        )
        save_run(run)
        run.status = RunStatus.RUNNING
        run.current_stage = Stage.RESEARCH
        save_run(run)

        loaded = get_run(run.id)
        print("3. Run saved and resumed at stage:", loaded.current_stage,
              "| status:", loaded.status)

        print("4. Dashboard rows (all):", len(list_runs()),
              "| running:", len(list_runs(RunStatus.RUNNING)),
              "| approved:", len(list_runs(RunStatus.APPROVED)))

        add_event(RunEvent(run_id=run.id, stage=Stage.IDENTITY,
                           type=EventType.COMPLETED, message="Identity confirmed"))
        add_event(RunEvent(run_id=run.id, stage=Stage.RESEARCH,
                           type=EventType.PROGRESS, message="12 sources found",
                           data={"count": 12}))
        events = get_events(run.id)
        print("5. Events in order:", [e.message for e in events])

        delete_run(run.id)
        print("6. Run deleted, events removed too:",
              get_run(run.id) is None and get_events(run.id) == [])

    print("\nAll database checks passed.")
