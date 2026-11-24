"""LinkedIn jobs through a paid Apify actor (default bebity/linkedin-jobs-scraper) instead of asking
LinkedIn directly, which blocks this PC's IP. Apify's servers do the requests; each job arrives with
its description, applicant count and apply type, so no detail page is read.

Pay per job returned (~US$0.001) plus a small fee per run start. The cheapest window the actor allows
is 24h, so a job can be billed once per run while it stays in that window: keep `rows` and the run
frequency low. Every run's cost is stored (db.apify_runs) and shown on /costs; `monthly_budget_usd`
stops runs before the free US$5/month credit is gone. Settings under "apify" in config.json; the
token is APIFY_TOKEN in .env.

CLI: `uv run python -m app.apify --from-file dataset.json` maps a saved dataset (no API call, free).
"""

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import scraper

API = "https://api.apify.com/v2"
WAIT_S = 240  # the run usually finishes in under a minute


class ApifyError(Exception):
    pass


@dataclass
class RunResult:
    run_id: str = ""
    status: str = ""
    usd: float = 0.0
    items: list[dict] = field(default_factory=list)


def token() -> str:
    t = os.environ.get("APIFY_TOKEN", "").strip()
    if not t:
        raise ApifyError("APIFY_TOKEN is missing from .env")
    return t


def _headers() -> dict:
    return {"Authorization": f"Bearer {token()}"}


def searches(cfg: dict) -> tuple[list[str], list[str]]:
    """The same searches as the dashboard's Searches panel (config "searches": keywords + location);
    the actor runs every keyword in every location. apify.titles/locations are only a fallback."""
    titles = list(dict.fromkeys(s["keywords"] for s in cfg["searches"] if s.get("keywords")))
    locations = list(dict.fromkeys(s.get("location") or "Worldwide" for s in cfg["searches"] if s.get("keywords")))
    return titles or cfg["apify"]["titles"], locations or cfg["apify"]["locations"]


def actor_input(cfg: dict) -> dict:
    o = cfg["apify"]
    titles, locations = searches(cfg)
    return {
        "titles": titles,
        "locations": locations,
        "rows": int(o["rows"]),
        "workTypes": ["2"],          # remote, filtered on LinkedIn's side
        "publishedAt": "r86400",     # the shortest window the actor accepts
        "easyApply": bool(cfg["easy_apply_only"]),  # To Apply hides the others anyway: don't pay for them
        "companyProfile": False,
        "enrichCompany": False,
    }


async def run(cfg: dict) -> RunResult:
    """Start the actor, wait for it, download its jobs. The charge cap is enforced by Apify."""
    o = cfg["apify"]
    titles, locations = searches(cfg)
    rows = int(o["rows"]) * max(1, len(titles) * len(locations))
    async with httpx.AsyncClient(timeout=WAIT_S + 30) as client:
        resp = await client.post(
            f"{API}/acts/{o['actor']}/runs", headers=_headers(), json=actor_input(cfg),
            params={"waitForFinish": WAIT_S, "maxItems": rows, "maxTotalChargeUsd": float(o["max_usd_per_run"])},
        )
        if resp.status_code >= 300:
            raise ApifyError(f"start failed: HTTP {resp.status_code} {resp.text[:200]}")
        r = resp.json()["data"]
        # Still running after WAIT_S: poll a little longer.
        for _ in range(6):
            if r.get("status") not in ("READY", "RUNNING"):
                break
            resp = await client.get(f"{API}/actor-runs/{r['id']}", headers=_headers(), params={"waitForFinish": 60})
            r = resp.json()["data"]
        # Apify settles the run's charges a few seconds after it finishes: read the cost again until stable.
        usd = float(r.get("usageTotalUsd") or 0)
        for _ in range(6):
            await asyncio.sleep(5)
            resp = await client.get(f"{API}/actor-runs/{r['id']}", headers=_headers())
            again = float(resp.json()["data"].get("usageTotalUsd") or 0) if resp.status_code < 300 else usd
            if again == usd and usd > 0:
                break
            usd = again
        result = RunResult(run_id=r["id"], status=r.get("status", ""), usd=usd)
        if r.get("defaultDatasetId"):
            resp = await client.get(f"{API}/datasets/{r['defaultDatasetId']}/items", headers=_headers(),
                                    params={"clean": "true", "format": "json"})
            if resp.status_code < 300:
                result.items = resp.json()
        return result


_account_cache: tuple[float, dict] | None = None


async def account_usage() -> dict:
    """{'used': US$ this billing cycle, 'limit': monthly limit, 'cycle_end': ISO} for the whole Apify
    account (includes runs started outside the tracker). Cached 10 min; {} when unavailable."""
    global _account_cache
    if _account_cache and time.monotonic() - _account_cache[0] < 600:
        return _account_cache[1]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{API}/users/me/limits", headers=_headers())
        resp.raise_for_status()
        d = resp.json()["data"]
        out = {"used": float((d.get("current") or {}).get("monthlyUsageUsd") or 0),
               "limit": float((d.get("limits") or {}).get("maxMonthlyUsageUsd") or 0),
               "cycle_end": (d.get("monthlyUsageCycle") or {}).get("endAt")}
    except (ApifyError, httpx.HTTPError, ValueError, KeyError):
        out = {}
    _account_cache = (time.monotonic(), out)
    return out


# ---- mapping ------------------------------------------------------------------

def _posted_at(item: dict, scraped_at: datetime) -> str | None:
    if m := scraper.RELATIVE_RE.search(item.get("postedTime") or ""):
        delta = timedelta(seconds=int(m.group(1)) * scraper.UNIT_SECONDS[m.group(2).lower()])
        return (scraped_at - delta).isoformat(timespec="seconds")
    date = item.get("publishedAt")
    return f"{date}T00:00:00+00:00" if date else None


def _company_url(url: str) -> str:
    """'https://be.linkedin.com/company/x?trk=…' -> 'https://www.linkedin.com/company/x'"""
    url = (url or "").split("?")[0].rstrip("/")
    return re.sub(r"^https?://[a-z]{2,3}\.linkedin\.com", "https://www.linkedin.com", url)


def to_row(item: dict, scraped_at: datetime, search: str) -> dict | None:
    """An actor item -> the columns of a LinkedIn job row (same id as the tracker's own search)."""
    job_id = str(item.get("id") or "").strip()
    title = " ".join((item.get("title") or "").split()).lstrip("- ").strip()
    if not job_id.isdigit() or not title:
        return None
    location = item.get("location") or ""
    description = item.get("description") or ""
    workplace, evidence = scraper.classify_workplace(title, location, description)
    if workplace != "not_remote":  # the search asked LinkedIn for remote jobs only
        workplace, evidence = "remote", evidence or "LinkedIn remote filter"
    applicants, applicants_text = scraper.parse_applicants(item.get("applicationsCount") or "")
    level = item.get("experienceLevel") or ""
    return {
        "id": job_id,
        "title": title,
        "company": (item.get("companyName") or "").strip(),
        "location": location,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "posted_at": _posted_at(item, scraped_at),
        "search_keywords": f"apify: {search}",
        "description": description,
        "applicants": applicants,
        "applicants_text": applicants_text,
        "workplace": workplace,
        "workplace_evidence": evidence,
        "seniority": "" if level == "Not Applicable" else level,
        "employment_type": item.get("contractType") or "",
        "job_function": item.get("jobFunction") or "",
        "industries": item.get("sector") or "",
        "company_url": _company_url(item.get("companyUrl") or ""),
        "apply_type": "easy_apply" if (item.get("applyType") or "").upper() == "EASY_APPLY" else "offsite",
        "salary": item.get("salary") or "",
    }


def to_rows(items: list[dict], scraped_at: datetime | None = None) -> list[dict]:
    scraped_at = scraped_at or datetime.now(timezone.utc)
    rows = []
    for item in items:
        search = (item.get("scrapingInfo") or {}).get("title") or "LinkedIn"
        if row := to_row(item, scraped_at, search):
            rows.append(row)
    return rows


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Map a saved Apify dataset to job rows (no API call)")
    ap.add_argument("--from-file")
    ap.add_argument("--show-input", action="store_true", help="print what the next run would send (free)")
    args = ap.parse_args()
    if args.show_input:
        from . import config
        cfg = config.load()
        print(json.dumps(actor_input(cfg), indent=1, ensure_ascii=False))
        return
    rows = to_rows(json.loads(Path(args.from_file).read_text(encoding="utf-8-sig")))
    for r in rows:
        print(f"{r['id']} | {r['title'][:50]} | {r['company']} | {r['posted_at']} | {r['applicants_text']} |"
              f" {r['apply_type']} | {r['workplace']} | {r['company_url']}")
    print(f"{len(rows)} row(s)")


if __name__ == "__main__":
    _cli()
