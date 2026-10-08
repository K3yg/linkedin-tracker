import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from types import SimpleNamespace

from . import checks, config, ranking, region, roles, scraper
from .config import ROOT

DB_PATH = ROOT / "data" / "jobs.db"  # the scout run points this at data/scout.db
STATUSES = ("new", "applied", "dismissed")
VIEWS = ("new", "flagged", "applied", "dismissed")  # "flagged" = new jobs that look suspicious

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    url             TEXT NOT NULL,
    posted_at       TEXT,
    first_seen_at   TEXT NOT NULL,
    search_keywords TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    applied_at      TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS companies (   -- cached public LinkedIn company pages
    url        TEXT PRIMARY KEY,
    size_text  TEXT,
    size_min   INTEGER,
    size_max   INTEGER,
    founded    INTEGER,
    org_type   TEXT,
    fetched_at TEXT NOT NULL
);
"""

# Columns added after the first release; created on startup if missing.
MIGRATIONS = {
    "applicants": "INTEGER",            # sortable: 0 = "first 25", 201 = "over 200"
    "applicants_text": "TEXT",          # display: "<25", "73", "200+", "closed"
    "applicants_checked_at": "TEXT",    # when the detail page was read
    "workplace": "TEXT",                # remote | not_remote | unknown (from the description)
    "workplace_evidence": "TEXT",       # the snippet that decided `workplace`
    "description": "TEXT",
    "seniority": "TEXT",
    "employment_type": "TEXT",
    "job_function": "TEXT",
    "industries": "TEXT",
    "company_url": "TEXT",
    "apply_type": "TEXT",               # easy_apply | offsite
    "salary": "TEXT",
    "flags": "TEXT",                    # JSON list of trust flags, see trust.py
    "suspicious": "INTEGER",            # 1 = hidden from To Apply, shown under Flagged
    "ai_verdict": "TEXT",               # apply | maybe | skip (ai.py); skip = shown under Flagged
    "ai_score": "INTEGER",              # 0-100, how worth applying
    "ai_summary": "TEXT",
    "ai_reasons": "TEXT",               # JSON list of short reasons
    "ai_model": "TEXT",
    "ai_checked_at": "TEXT",
    "cv_status": "TEXT",                # working | done | error (tailor.py; files in cv/out/<id>/)
    "cv_error": "TEXT",
    "cv_changes": "TEXT",               # JSON: what changed vs base.tex and why
    "cv_language": "TEXT",
    "cv_model": "TEXT",
    "cv_created_at": "TEXT",
}


def copy_key(company: str | None, title: str | None) -> tuple[str, str]:
    """Same company + same title (ignoring case/punctuation) = the same role posted in another city."""
    return (company or "", " ".join(re.sub(r"[^a-z0-9+#]+", " ", (title or "").lower()).split()))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_FUNCTIONS: dict[str, SimpleNamespace] = {}


def functions(cfg: dict) -> SimpleNamespace:
    """Title filter, skills fit, description checks and remote region, built from config.json.
    Rebuilt only when the config keys they read change; results are memoized per job text,
    since scoring every description on each page load took ~2s."""
    key = json.dumps([cfg["role_keywords"], cfg["exclude_title_words"], cfg["profile"],
                      cfg["remote_region"]], sort_keys=True)
    if key not in _FUNCTIONS:
        scope = lru_cache(maxsize=8192)(region.build(cfg))
        _FUNCTIONS.clear()
        _FUNCTIONS[key] = SimpleNamespace(
            is_dev_role=lru_cache(maxsize=8192)(roles.build(cfg)),
            fit=lru_cache(maxsize=8192)(ranking.build_fit(cfg)),
            job_issue=lru_cache(maxsize=8192)(checks.build(cfg)),
            scope=scope,
            remote_scope=lambda title, location, description: scope(title, location, description)[0],
        )
    return _FUNCTIONS[key]


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # As SQL functions, re-read from config.json so edits apply immediately.
    fn = functions(config.load())
    conn.create_function("is_dev_role", 1, fn.is_dev_role, deterministic=True)
    conn.create_function("fit_score", 2, fn.fit, deterministic=True)
    conn.create_function("job_issue", 2, fn.job_issue, deterministic=True)
    conn.create_function("remote_scope", 3, fn.remote_scope, deterministic=True)
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for col, kind in MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")
        # A CV being tailored when the server stopped will never finish.
        conn.execute("UPDATE jobs SET cv_status = 'error', cv_error = 'interrupted by a restart — try again'"
                     " WHERE cv_status = 'working'")
        # Re-run the remote classifier on stored descriptions so improvements reach jobs already read.
        rows = conn.execute("SELECT id, title, location, description FROM jobs"
                            " WHERE status = 'new' AND description IS NOT NULL AND description != ''").fetchall()
        conn.executemany(
            "UPDATE jobs SET workplace = ?, workplace_evidence = ? WHERE id = ?",
            [(*scraper.classify_workplace(r["title"], r["location"] or "", r["description"]), r["id"]) for r in rows],
        )


def last_seen_at() -> datetime | None:
    """When the newest job was found; lets a restarted poller resume instead of re-scanning 24h."""
    with connect() as conn:
        value = conn.execute("SELECT MAX(first_seen_at) FROM jobs").fetchone()[0]
    return datetime.fromisoformat(value) if value else None


def upsert_jobs(jobs) -> list:
    """Insert jobs not seen before. Returns only the newly inserted ones."""
    new = []
    seen_at = now_iso()
    with connect() as conn:
        for j in jobs:
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (id, title, company, location, url, posted_at, first_seen_at, search_keywords)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (j.id, j.title, j.company, j.location, j.url, j.posted_at, seen_at, j.search_keywords),
            )
            if cur.rowcount:
                new.append(j)
            if j.easy_apply:  # found by an Easy Apply-only search: known before reading the page
                conn.execute("UPDATE jobs SET apply_type = 'easy_apply' WHERE id = ? AND apply_type IS NULL",
                             (j.id,))
    return new


# ---- detail pages -------------------------------------------------------------

NEEDS_DETAILS = (
    "status = 'new' AND is_dev_role(title)"
    " AND (applicants_checked_at IS NULL OR workplace IS NULL OR description IS NULL)"
)


def jobs_needing_details(limit: int) -> list[sqlite3.Row]:
    """Jobs whose detail page hasn't been read (with the current set of fields), best bets first.
    LinkedIn only allows a few detail pages per scan, so they go to jobs most likely to reach
    To Apply: not from a blocked company or limited to another country by the title, known to be
    Easy Apply (when easy_apply_only), posted in the last 3 days, not senior (the user is mid-level),
    found by a "... remote" query, newest first; copies of a posting already read (or picked) go last."""
    cfg = config.load()
    fn = functions(cfg)
    blocked = roles._pattern(cfg["trust"]["blocked_companies"])
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM jobs WHERE {NEEDS_DETAILS}").fetchall()
        read = {copy_key(r["company"], r["title"])
                for r in conn.execute("SELECT company, title FROM jobs WHERE description IS NOT NULL")}

    def priority(r) -> tuple:
        hopeless = (bool(blocked and blocked.search(r["company"] or ""))
                    or fn.scope(r["title"], "", "")[0] == "restricted")
        return (
            hopeless,
            cfg["easy_apply_only"] and r["apply_type"] != "easy_apply",
            ranking.age_hours(r) > 72,
            ranking.is_senior(r),
            "remot" not in (r["search_keywords"] or "").lower(),
            -datetime.fromisoformat(r["posted_at"]).timestamp() if r["posted_at"] else 0,
        )

    # One posting per company + title first: the copies (same role, other cities) can wait.
    first, copies, seen = [], [], set(read)
    for r in sorted(rows, key=priority):
        key = copy_key(r["company"], r["title"])
        (copies if key in seen else first).append(r)
        seen.add(key)
    return (first + copies)[:limit]


def pending_details() -> int:
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {NEEDS_DETAILS}").fetchone()[0]


DETAIL_FIELDS = (
    "applicants", "applicants_text", "workplace", "workplace_evidence", "description", "seniority",
    "employment_type", "job_function", "industries", "company_url", "apply_type", "salary",
)


def set_details(job_id: str, d) -> None:
    # An unreadable apply button keeps what an Easy Apply-only search already told us.
    assignments = ", ".join("apply_type = COALESCE(NULLIF(?, ''), apply_type)" if f == "apply_type" else f"{f} = ?"
                            for f in DETAIL_FIELDS)
    with connect() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments}, applicants_checked_at = ? WHERE id = ?",
            (*(getattr(d, f) for f in DETAIL_FIELDS), now_iso(), job_id),
        )


def jobs_to_recheck(limit: int, older_than_hours: int = 24) -> list[sqlite3.Row]:
    """Jobs shown in To Apply whose page was read more than a day ago, oldest first: re-reading
    catches postings that stopped accepting applications and refreshes the applicant count."""
    cfg = config.load()
    since = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM jobs WHERE {_view_where('new', cfg['remote_only'])} AND applicants_checked_at < ?"
            " ORDER BY applicants_checked_at LIMIT ?",
            (since, limit),
        ).fetchall()


# ---- AI verdicts (ai.py) ------------------------------------------------------

def jobs_for_ai(limit: int) -> list[sqlite3.Row]:
    """Jobs that pass every rule but haven't been judged by the AI yet, newest first."""
    cfg = config.load()
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM jobs WHERE {_passes_rules(cfg, cfg['remote_only'])} AND ai_checked_at IS NULL"
            " ORDER BY posted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def set_cv(job_id: str, status: str, error: str = "", changes: str | None = None,
           language: str | None = None, model: str | None = None) -> None:
    """status 'working' keeps the previous CV's details (shown again if this run fails)."""
    with connect() as conn:
        if status == "working":
            conn.execute("UPDATE jobs SET cv_status = 'working', cv_error = NULL WHERE id = ?", (job_id,))
        else:
            conn.execute(
                "UPDATE jobs SET cv_status = ?, cv_error = ?, cv_changes = COALESCE(?, cv_changes),"
                " cv_language = COALESCE(?, cv_language), cv_model = ?,"
                " cv_created_at = CASE WHEN ? = 'done' THEN ? ELSE cv_created_at END WHERE id = ?",
                (status, error, changes, language, model, status, now_iso(), job_id),
            )


def set_ai(job_id: str, verdict: str, score: int, summary: str, reasons: list[str], model: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET ai_verdict = ?, ai_score = ?, ai_summary = ?, ai_reasons = ?, ai_model = ?,"
            " ai_checked_at = ? WHERE id = ?",
            (verdict, score, summary, json.dumps(reasons, ensure_ascii=False), model, now_iso(), job_id),
        )


# ---- trust flags --------------------------------------------------------------

def recent_jobs(days: int = 7) -> list[sqlite3.Row]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs WHERE first_seen_at >= ?", (since,)).fetchall()


def set_flags(updates: list[tuple[str, str, int]]) -> None:
    """updates: (job_id, flags_json, suspicious)"""
    with connect() as conn:
        conn.executemany("UPDATE jobs SET flags = ?, suspicious = ? WHERE id = ?",
                         [(flags, suspicious, job_id) for job_id, flags, suspicious in updates])


# ---- dashboard ----------------------------------------------------------------

def _passes_rules(cfg: dict, remote_only: bool) -> str:
    """Every rule-based filter for To Apply (everything except the AI verdict)."""
    min_fit = int(cfg["profile"]["min_fit"])
    where = (
        "status = 'new' AND is_dev_role(title) AND IFNULL(suspicious, 0) = 0"
        f" AND (description IS NULL OR fit_score(title, description) >= {min_fit})"
        " AND job_issue(title, description) = ''"  # too senior / not English (checks.py)
        " AND IFNULL(applicants_text, '') != 'closed'"  # no longer accepting applications
    )
    if cfg["easy_apply_only"]:
        where += " AND apply_type = 'easy_apply'"
    if not remote_only:
        return where
    # description IS NOT NULL: verified by the current (strict) classifier, not an older one.
    # remote_scope: "remote within the US" etc. is no use from Brazil (region.py).
    scopes = ", ".join(f"'{s}'" for s in region.shown(cfg))
    return where + (" AND workplace = 'remote' AND description IS NOT NULL"
                    f" AND remote_scope(title, location, description) IN ({scopes})")


def _ai_hides(cfg: dict) -> bool:
    return bool(cfg["ai"].get("enabled") and cfg["ai"].get("hide_skips"))


def _view_where(view: str, remote_only: bool) -> str:
    cfg = config.load()
    if view == "new":
        where = _passes_rules(cfg, remote_only)
        return where + (" AND IFNULL(ai_verdict, '') != 'skip'" if _ai_hides(cfg) else "")
    if view == "flagged":
        # Suspicious, or the AI said it isn't worth applying (it only reads jobs that pass the rules).
        ai_skip = f" OR ({_passes_rules(cfg, remote_only)} AND ai_verdict = 'skip')" if _ai_hides(cfg) else ""
        return f"((status = 'new' AND is_dev_role(title) AND suspicious = 1){ai_skip})"
    return f"status = '{view if view in STATUSES else 'new'}'"


ORDER = {
    "applicants": "applicants IS NULL, applicants ASC, apply_type = 'easy_apply' DESC, posted_at DESC",
    "newest": "posted_at DESC, first_seen_at DESC",
    "flagged": "first_seen_at DESC",
    "applied": "applied_at DESC",
    "dismissed": "first_seen_at DESC",
}


def list_jobs(view: str = "new", q: str = "", sort: str = "applicants", remote_only: bool = False) -> list[sqlite3.Row]:
    sql = f"SELECT * FROM jobs WHERE {_view_where(view, remote_only)}"
    args: list = []
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args += [f"%{q}%"] * 3
    order = ORDER[sort if sort in ("applicants", "newest") else "applicants"] if view == "new" else ORDER[view]
    sql += f" ORDER BY {order} LIMIT 500"
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def counts(remote_only: bool = False) -> dict[str, int]:
    with connect() as conn:
        return {
            v: conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {_view_where(v, remote_only)}").fetchone()[0]
            for v in VIEWS
        }


def companies_needed(limit: int) -> list[str]:
    """Company pages not cached yet, for jobs that could reach To Apply (remote, not flagged)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT company_url FROM jobs
               WHERE status = 'new' AND company_url != '' AND workplace = 'remote'
                 AND IFNULL(suspicious, 0) = 0
                 AND company_url NOT IN (SELECT url FROM companies)
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [r["company_url"] for r in rows]


def save_company(url: str, c) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO companies VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, c.size_text, c.size_min, c.size_max, c.founded, c.org_type, now_iso()),
        )


def companies_by_url() -> dict[str, sqlite3.Row]:
    with connect() as conn:
        return {r["url"]: r for r in conn.execute("SELECT * FROM companies")}


def all_jobs() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM jobs ORDER BY first_seen_at DESC").fetchall()


def delete_unhandled() -> int:
    """Remove every job still in To Apply. Applied/dismissed jobs, and jobs with a tailored CV, are kept."""
    with connect() as conn:
        return conn.execute("DELETE FROM jobs WHERE status = 'new' AND cv_status IS NULL").rowcount


def set_status(job_id: str, status: str) -> None:
    """Also moves the job's copies (same company + title in other cities, same current status),
    which the dashboard shows as a single entry."""
    if status not in STATUSES:
        raise ValueError(status)
    applied_at = now_iso() if status == "applied" else None
    with connect() as conn:
        job = conn.execute("SELECT company, title, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return
        key = copy_key(job["company"], job["title"])
        ids = [r["id"] for r in conn.execute("SELECT id, company, title FROM jobs WHERE company IS ? AND status = ?",
                                             (job["company"], job["status"]))
               if copy_key(r["company"], r["title"]) == key]
        conn.executemany("UPDATE jobs SET status = ?, applied_at = ? WHERE id = ?",
                         [(status, applied_at, i) for i in {job_id, *ids}])
