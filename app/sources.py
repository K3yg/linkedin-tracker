"""Job boards besides LinkedIn: public JSON APIs / RSS feeds that don't rate-limit a few calls an hour.

Each fetcher returns normalized dicts ready for db.upsert_external(). Remote boards (Himalayas,
Remotive, Remote OK, We Work Remotely) only list remote jobs; company boards (Greenhouse, Lever,
Ashby) list everything, so only their remote postings are kept. The description starts with a
"Remote — <where>" line so region.py can tell whether the job is open to Brazil.
Settings under "sources" in config.json.
"""

import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job tracker)", "Accept": "application/json, */*"}
TIMEOUT = 30

# Remotive asks for at most a few calls a day; the others are fine hourly.
MIN_HOURS = {"remotive": 6}
LABELS = {"himalayas": "Himalayas", "remotive": "Remotive", "remoteok": "Remote OK",
          "weworkremotely": "We Work Remotely", "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}


def _text(html: str | None) -> str:
    if not html:
        return ""
    return "\n".join(line.strip() for line in HTMLParser(html).text(separator="\n").splitlines() if line.strip())


def _iso(value) -> str | None:
    """Epoch seconds/ms or ISO string -> UTC ISO."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            n = float(value)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, timezone.utc).isoformat(timespec="seconds")
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def _short_id(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:12]


def _job(source: str, native_id, title, company, where: str, url, posted_at, description: str,
         salary: str = "", employment_type: str = "", seniority: str = "") -> dict:
    where = where.strip() or "location not stated"
    return {
        "id": f"{source}-{native_id}",
        "source": source,
        "title": " ".join((title or "").split()),
        "company": (company or "").strip(),
        "location": f"Remote · {where}",
        "url": url,
        "posted_at": _iso(posted_at),
        "description": f"Remote — {where}\n\n{description}",
        "salary": salary or "",
        "employment_type": employment_type or "",
        "seniority": seniority or "",
    }


# ---- remote job boards ---------------------------------------------------------

async def himalayas(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    """Search API filtered to jobs that hire in `country` (an empty restriction list = worldwide)."""
    out = []
    for q in opts.get("queries", []):
        resp = await client.get("https://himalayas.app/jobs/api/search", headers=HEADERS, timeout=TIMEOUT,
                                params={"q": q, "country": opts.get("country", "Brazil"), "sort": "recent"})
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            where = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
            if len(where) > 80:  # long country lists: say the user's country is in it
                where = f"{opts.get('country', 'Brazil')} and {len(j['locationRestrictions']) - 1} other countries"
            salary = ""
            if j.get("minSalary"):
                salary = f"{j.get('currency') or ''} {j['minSalary']}–{j.get('maxSalary') or j['minSalary']}".strip()
            out.append(_job("himalayas", _short_id(j["guid"]), j["title"], j["companyName"], where,
                            j.get("applicationLink") or j["guid"], j.get("pubDate"), _text(j.get("description")),
                            salary, j.get("employmentType", ""), ", ".join(j.get("seniority") or [])))
    return out


async def remotive(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    resp = await client.get("https://remotive.com/api/remote-jobs", headers=HEADERS, timeout=TIMEOUT,
                            params={"category": opts.get("category", "software-dev")})
    resp.raise_for_status()
    return [_job("remotive", j["id"], j["title"], j["company_name"], j.get("candidate_required_location", ""),
                 j["url"], j.get("publication_date"), _text(j.get("description")), j.get("salary", ""),
                 (j.get("job_type") or "").replace("_", "-").title())
            for j in resp.json().get("jobs", [])]


async def remoteok(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    resp = await client.get("https://remoteok.com/api", headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    out = []
    for j in resp.json()[1:]:  # [0] is the terms of use
        salary = f"${j['salary_min']}–${j['salary_max']}" if j.get("salary_min") else ""
        out.append(_job("remoteok", j["id"], j.get("position"), j.get("company"), j.get("location", ""),
                        j.get("url"), j.get("date") or j.get("epoch"), _text(j.get("description")), salary))
    return out


async def weworkremotely(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    out = []
    for category in opts.get("categories", []):
        resp = await client.get(f"https://weworkremotely.com/categories/{category}.rss",
                                headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        for item in ET.fromstring(resp.content).iter("item"):
            get = lambda tag: (item.findtext(tag) or "").strip()
            company, _, title = get("title").partition(": ")
            where = get("region") or get("country")
            try:
                posted = datetime.strptime(get("pubDate"), "%a, %d %b %Y %H:%M:%S %z").isoformat()
            except ValueError:
                posted = None
            out.append(_job("weworkremotely", _short_id(get("guid") or get("link")), title or company,
                            company if title else "", where, get("link"), posted, _text(get("description")),
                            employment_type=get("type")))
    return out


# ---- company job boards (remote postings only) ----------------------------------

async def greenhouse(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    out = []
    for board in opts.get("companies", []):
        resp = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                                params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            where = (j.get("location") or {}).get("name", "")
            if "remote" not in where.lower():
                continue
            out.append(_job("greenhouse", j["id"], j["title"], j.get("company_name") or board, where,
                            j["absolute_url"], j.get("first_published") or j.get("updated_at"),
                            _text(HTMLParser(j.get("content") or "").text())))  # content is escaped HTML
    return out


async def lever(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    out = []
    for board in opts.get("companies", []):
        resp = await client.get(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"},
                                headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        for j in resp.json():
            cats = j.get("categories") or {}
            where = ", ".join(cats.get("allLocations") or [cats.get("location") or ""])
            if j.get("workplaceType") != "remote" and "remote" not in where.lower():
                continue
            out.append(_job("lever", j["id"], j["text"], board, where, j["hostedUrl"], j.get("createdAt"),
                            j.get("descriptionPlain") or _text(j.get("description")),
                            employment_type=cats.get("commitment", "")))
    return out


async def ashby(client: httpx.AsyncClient, opts: dict) -> list[dict]:
    out = []
    for board in opts.get("companies", []):
        resp = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                                params={"includeCompensation": "true"}, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        for j in resp.json().get("jobs", []):
            if not j.get("isListed", True) or not (j.get("isRemote") or j.get("workplaceType") == "Remote"):
                continue
            places = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
            remote_places = [p for p in places if "remote" in p.lower()] or places
            comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
            out.append(_job("ashby", j["id"], j["title"], board, ", ".join(p for p in remote_places if p),
                            j["jobUrl"], j.get("publishedAt"), j.get("descriptionPlain") or "", comp,
                            j.get("employmentType", "")))
    return out


FETCHERS = {"himalayas": himalayas, "remotive": remotive, "remoteok": remoteok,
            "weworkremotely": weworkremotely, "greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def due(cfg: dict, last_run: dict[str, str]) -> list[str]:
    """Enabled sources whose interval has passed."""
    opts = cfg.get("sources") or {}
    every = float(opts.get("every_minutes", 60))
    now = datetime.now(timezone.utc)
    names = []
    for name in FETCHERS:
        o = opts.get(name)
        if not o or not o.get("enabled", True):
            continue
        wait = timedelta(minutes=max(every, MIN_HOURS.get(name, 0) * 60))
        last = last_run.get(name)
        if not last or now - datetime.fromisoformat(last) >= wait:
            names.append(name)
    return names


async def fetch(cfg: dict, names: list[str]) -> tuple[list[dict], dict[str, str]]:
    """-> (jobs posted in the last max_age_days, {source: error}) for the given sources."""
    opts = cfg.get("sources") or {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=float(opts.get("max_age_days", 14)))
    jobs, errors = [], {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for name in names:
            try:
                found = await FETCHERS[name](client, opts[name])
            except (httpx.HTTPError, ValueError, KeyError, ET.ParseError) as e:
                errors[name] = repr(e)
                log.warning("source %s failed: %r", name, e)
                continue
            fresh = [j for j in found if j["title"] and j["url"]
                     and (not j["posted_at"] or datetime.fromisoformat(j["posted_at"]) >= cutoff)]
            log.info("source %s: %d jobs, %d recent", name, len(found), len(fresh))
            jobs += fresh
    return jobs, errors
