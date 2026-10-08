import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ai, checks, config, db, ranking, tailor, trust
from .poller import Poller

poller = Poller()
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    task = asyncio.create_task(poller.run_forever())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ---- template helpers -------------------------------------------------------

def _to_dt(value) -> datetime | None:
    if not value:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _span(seconds: float) -> str:
    seconds = abs(int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "<1m"


def ago(value) -> str:
    dt = _to_dt(value)
    return f"{_span((datetime.now(timezone.utc) - dt).total_seconds())} ago" if dt else "—"


def until(value) -> str:
    dt = _to_dt(value)
    return f"in {_span((dt - datetime.now(timezone.utc)).total_seconds())}" if dt else ""


def is_fresh(job) -> bool:
    dt = _to_dt(job["posted_at"])
    return bool(dt) and (datetime.now(timezone.utc) - dt).total_seconds() < 3600


def mentions_remote(job) -> bool:
    return "remote" in f"{job['title']} {job['location']}".lower()


def applicants_level(job) -> str:
    n = job["applicants"]
    if n is None:
        return "unknown"
    return "low" if n < 25 else "mid" if n < 100 else "high"


templates.env.filters.update(ago=ago, until=until)
templates.env.globals.update(
    is_fresh=is_fresh, mentions_remote=mentions_remote, applicants_level=applicants_level,
    flags_of=trust.parse, poller=poller,
)


def _status(value: str) -> str:
    """The `status` query param selects a view: new, flagged, applied or dismissed."""
    return value if value in db.VIEWS else "new"


def _sort(value: str) -> str:
    return value if value in ("match", "applicants", "newest") else "match"


def _annotate(rows, cfg: dict) -> list[dict]:
    """Rows -> dicts with fit / company kind / seniority / match score for ranking and badges."""
    companies = db.companies_by_url()
    fn = db.functions(cfg)
    out = []
    for r in rows:
        j = dict(r)
        co = companies.get(r["company_url"] or "")
        kind = ranking.company_kind(co)
        j["co"] = {"kind": kind, "is_startup": kind == "startup",
                   "size_text": co["size_text"] if co else "", "founded": co["founded"] if co else None}
        j["senior"] = ranking.is_senior(r)
        j["mid"] = ranking.is_mid(r)
        j["years"] = checks.years_required(r["description"])
        j["fit"] = fit = fn.fit(r["title"], r["description"])
        j["scope"], j["scope_evidence"] = fn.scope(r["title"], r["location"], r["description"])
        j["match"] = ranking.match_score(r, co, fit, j["scope"])
        j["ai_reasons_list"] = json.loads(r["ai_reasons"]) if r["ai_reasons"] else []
        j["cv"] = json.loads(r["cv_changes"]) if r["cv_changes"] else None
        out.append(j)
    return out


def _collapse_copies(jobs: list[dict]) -> list[dict]:
    """One entry per company + title: the same role posted in several cities shows once
    (the first, i.e. best-ranked, copy) with the other locations listed. Actions apply to all copies."""
    out, by_key = [], {}
    for j in jobs:
        key = db.copy_key(j["company"], j["title"])
        if key in by_key:
            by_key[key]["copies"].append(j["location"])
        else:
            j["copies"] = []
            by_key[key] = j
            out.append(j)
    return out


def _list_context(status: str, q: str, sort: str) -> dict:
    cfg = config.load()
    rows = db.list_jobs(status, q, "applicants" if sort == "match" else sort, remote_only=cfg["remote_only"])
    jobs = _annotate(rows, cfg)
    if sort == "match" and status == "new":
        jobs.sort(key=lambda j: j["match"], reverse=True)
    jobs = _collapse_copies(jobs)
    picks = []
    if status == "new":
        picks = ranking.top_picks(jobs, cfg["top_picks_exclude_title_words"])
        pick_ids = {p["id"] for p in picks}
        jobs = [j for j in jobs if j["id"] not in pick_ids]
    return {
        "status": status, "q": q, "sort": sort, "jobs": jobs, "picks": picks,
        "remote_only": cfg["remote_only"], "pending": db.pending_details() if cfg["remote_only"] else 0,
        "ai_on": ai.enabled(cfg),
        "easy_only": cfg["easy_apply_only"],
    }


# ---- pages & fragments ------------------------------------------------------

@app.get("/")
def index(request: Request, status: str = "new", q: str = "", sort: str = "match"):
    ctx = _list_context(_status(status), q, _sort(sort))
    ctx.update(counts=db.counts(ctx["remote_only"]), searches=config.load()["searches"])
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/list")
def job_list(request: Request, status: str = "new", q: str = "", sort: str = "match"):
    return templates.TemplateResponse(request, "_list.html", _list_context(_status(status), q, _sort(sort)))


@app.get("/tabs")
def tabs(request: Request, status: str = "new", q: str = "", sort: str = "match"):
    return templates.TemplateResponse(request, "_tabs.html", {
        "status": _status(status), "q": q, "sort": _sort(sort),
        "counts": db.counts(config.load()["remote_only"]),
    })


@app.get("/scan-status")
def scan_status(request: Request, was_scanning: int = 0):
    resp = templates.TemplateResponse(request, "_status.html", {})
    if was_scanning and not poller.scanning:
        resp.headers["HX-Trigger"] = "scanDone"
    return resp


# ---- actions ----------------------------------------------------------------

async def _start_scan() -> None:
    if not poller.scanning:
        task = asyncio.create_task(poller.scan())
        _background.add(task)
        task.add_done_callback(_background.discard)
        await asyncio.sleep(0)  # let the scan grab its lock so the fragment shows "Scanning…"


@app.post("/api/scan")
async def scan(request: Request):
    await _start_scan()
    return templates.TemplateResponse(request, "_status.html", {})


@app.post("/api/reset")
async def reset():
    """Drop every To Apply job and search the full lookback window again."""
    deleted = db.delete_unhandled()
    logging.getLogger(__name__).info("reset: deleted %d job(s)", deleted)
    poller.full_lookback = True
    await _start_scan()  # if a scan is already running, the next one does the full search
    return Response("", headers={"HX-Refresh": "true"})


# Job/company actions are sent by app.js, which updates the page optimistically.

@app.post("/api/jobs/{job_id}/status", status_code=204)
def set_status(job_id: str, to: str = Form(...)):
    db.set_status(job_id, _status(to))


def _add_company(list_name: str, company: str, remove_from: str) -> None:
    cfg = config.load()
    t = cfg["trust"]
    if company and company not in t[list_name]:
        t[list_name].append(company)
    t[remove_from] = [c for c in t[remove_from] if c != company]
    config.save(cfg)
    trust.refresh()


@app.post("/api/companies/block", status_code=204)
def block_company(company: str = Form(...)):
    """Hide every job from this company from now on (config trust.blocked_companies)."""
    _add_company("blocked_companies", company.strip(), "trusted_companies")


@app.post("/api/companies/trust", status_code=204)
def trust_company(company: str = Form(...)):
    """Never flag this company (config trust.trusted_companies)."""
    _add_company("trusted_companies", company.strip(), "blocked_companies")


# ---- tailored CVs (tailor.py) ---------------------------------------------------

def _cv_slot(request: Request, job_id: str):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return Response("", status_code=404)
    j = dict(row)
    j["cv"] = json.loads(row["cv_changes"]) if row["cv_changes"] else None
    return templates.TemplateResponse(request, "_cv.html", {"j": j})


@app.post("/api/jobs/{job_id}/cv")
async def make_cv(request: Request, job_id: str):
    """Start tailoring the CV to this job in the background; the slot polls until it's done."""
    db.set_cv(job_id, "working")
    task = asyncio.create_task(tailor.tailor(job_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return _cv_slot(request, job_id)


@app.get("/cv-slot/{job_id}")
def cv_slot(request: Request, job_id: str):
    return _cv_slot(request, job_id)


@app.get("/cv/{job_id}/{name}")
def cv_file(job_id: str, name: str):
    if name not in ("cv.pdf", "cv.tex") or not job_id.isdigit():
        return Response("", status_code=404)
    path = tailor.OUT_DIR / job_id / name
    if not path.exists():
        return Response("not generated yet", status_code=404)
    with db.connect() as conn:
        row = conn.execute("SELECT company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    company = re.sub(r"[^A-Za-z0-9]+", "_", row["company"] if row else "").strip("_") or job_id
    return FileResponse(path, filename=f"{os.environ.get('CV_NAME', 'My')}_CV_{company}{path.suffix}",
                        content_disposition_type="inline" if name == "cv.pdf" else "attachment")


@app.post("/api/searches")
def add_search(request: Request, keywords: str = Form(...), location: str = Form("")):
    cfg = config.load()
    cfg["searches"].append({"keywords": keywords.strip(), "location": location.strip()})
    config.save(cfg)
    return templates.TemplateResponse(request, "_searches.html", {"searches": cfg["searches"]})


@app.post("/api/searches/{idx}/delete")
def delete_search(request: Request, idx: int):
    cfg = config.load()
    if 0 <= idx < len(cfg["searches"]):
        cfg["searches"].pop(idx)
        config.save(cfg)
    return templates.TemplateResponse(request, "_searches.html", {"searches": cfg["searches"]})
