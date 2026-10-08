"""Fetch job cards from LinkedIn's public (logged-out) jobs search endpoint.

Verified behaviour of the logged-out endpoint: `keywords`, `location`, `f_TPR`
(posted within N seconds), `start` and `f_AL=true` (Easy Apply only) work; the workplace
filter (`f_WT`, remote/hybrid) and `sortBy` are silently ignored, so we don't send them.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from selectolax.parser import HTMLParser

BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
PAGE_SIZE = 10
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

UNIT_SECONDS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400,
}
RELATIVE_RE = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)


class RateLimited(Exception):
    pass


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    posted_at: str | None
    search_keywords: str
    easy_apply: bool = False  # found by an Easy Apply-only search


def build_params(keywords: str, location: str, lookback_s: int, start: int, easy_apply: bool = False) -> dict:
    params = {"keywords": keywords, "f_TPR": f"r{int(lookback_s)}", "start": start}
    if location:
        params["location"] = location
    if easy_apply:
        params["f_AL"] = "true"
    return params


def _text(node, selector: str) -> str:
    el = node.css_first(selector)
    return " ".join(el.text().split()) if el else ""


def _posted_at(node, scraped_at: datetime) -> str | None:
    """LinkedIn gives 'N hours ago' (precise) plus a date-only datetime attr."""
    el = node.css_first("time")
    if not el:
        return None
    m = RELATIVE_RE.search(el.text())
    if m:
        delta = timedelta(seconds=int(m.group(1)) * UNIT_SECONDS[m.group(2).lower()])
        return (scraped_at - delta).isoformat(timespec="seconds")
    date = el.attributes.get("datetime")
    return f"{date}T00:00:00+00:00" if date else None


def parse(html: str, keywords: str, easy_apply: bool = False) -> list[Job]:
    scraped_at = datetime.now(timezone.utc)
    jobs = []
    for card in HTMLParser(html).css("[data-entity-urn^='urn:li:jobPosting:']"):
        job_id = card.attributes["data-entity-urn"].rsplit(":", 1)[-1]
        title = _text(card, ".base-search-card__title")
        if not job_id or not title:
            continue
        jobs.append(Job(
            id=job_id,
            title=title,
            company=_text(card, ".base-search-card__subtitle"),
            location=_text(card, ".job-search-card__location"),
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            posted_at=_posted_at(card, scraped_at),
            search_keywords=keywords,
            easy_apply=easy_apply,
        ))
    return jobs


async def search(
    client: httpx.AsyncClient,
    keywords: str,
    location: str = "",
    lookback_s: int = 86400,
    start: int = 0,
    easy_apply: bool = False,
) -> list[Job]:
    resp = await client.get(
        BASE_URL,
        params=build_params(keywords, location, lookback_s, start, easy_apply),
        headers=HEADERS,
        timeout=20,
    )
    if resp.status_code in (429, 999):
        raise RateLimited(f"HTTP {resp.status_code}")
    if resp.status_code == 400:  # returned when paging past the last result
        return []
    resp.raise_for_status()
    return parse(resp.text, keywords, easy_apply)


def parse_applicants(caption: str) -> tuple[int | None, str]:
    """'Be among the first 25 applicants' -> (0, '<25'); 'Over 200 applicants' -> (201, '200+')."""
    text = " ".join(caption.split())
    if m := re.search(r"first (\d+)", text, re.I):
        return 0, f"<{m.group(1)}"
    if m := re.search(r"over ([\d,]+)", text, re.I):
        n = int(m.group(1).replace(",", ""))
        return n + 1, f"{n}+"
    if m := re.search(r"([\d,]+)\s+applicant", text, re.I):
        n = int(m.group(1).replace(",", ""))
        return n, str(n)
    return None, ""


# Logged-out pages carry no workplace-type field, so remote-ness is read from the description.
REMOTE_STRONG = re.compile(
    r"\b(?:fully|full|100%|completely|entirely|permanently)[ -]remote\b"
    r"|\bthis (?:is an?|role is|position is|job is)(?: a)? (?:fully |100% )?remote\b"
    r"|\bremote (?:position|role|opportunity|job|work|within|in the|\(u\.?s)"
    r"|\bwork from (?:anywhere|home)\b|\bremote-only\b|\bremote[- ]eligible\b"
    r"|\btelecommut\w*|\(remote\)|/\s*remote\b|\blocation:?[^.]{0,40}\bremote\b"
    r"|\bbased remotely\b|\bremotely based\b"
    # Portuguese / Spanish: "100% remoto", "trabalho remoto", "modalidade: remota", "100% home office"
    r"|\b(?:100%|totalmente|completamente|full) remot[oa]\b|\btrabajo remoto\b|\btrabalho remoto\b"
    r"|\bmodal(?:idade|idad)(?: de trabalho| de trabajo)?:? (?:100% )?remot[oa]\b|\b100% home[ -]office\b",
    re.I,
)
REMOTE_WEAK = re.compile(
    r"\bremote[- ](?:first|friendly)\b|\bdistributed (?:team|company)\b|\bjoin our (?:fully )?remote team\b"
    r"|\bwork(?:ing)? remotely\b",
    re.I,
)
NOT_REMOTE = re.compile(
    r"\bnot (?:a )?(?:fully )?remote(?:-only)?\b|\bno remote\b"
    r"|\b\d\s*(?:\+\s*)?days? (?:a|per|each) week\b|\bcommut"
    r"|\bon-?site (?:role|position|requirement|only)\b|\bin[- ]office (?:role|position|requirement)\b"
    r"|\bmust (?:be able to )?(?:report|work) (?:to|from|in) (?:our|the) office\b",
    re.I,
)
HYBRID = re.compile(r"\bhybrid\b|\bon-?site\b|\bin-person\b", re.I)
# Any of these means the job needs presence somewhere (strict: user can only work 100% remote).
OFFICE = re.compile(
    r"\bhybrid\b|\bon-?site\b|\bin[- ]office\b|\bin the office\b|\boffice[- ]based\b|\bin[- ]person\b"
    r"|\bh[íi]brid[oa]\b|\bpresencial\b"
    r"|\brelocat\w*|\b(?:mostly|partially|partly|primarily|largely)[ -]remote\b",
    re.I,
)
# Only an explicit statement like this outweighs an office mention elsewhere (e.g. boilerplate).
REMOTE_EXPLICIT = re.compile(
    r"\b(?:fully|full|100%|completely|entirely|permanently)[ -]remote\b|\bremote-only\b", re.I
)
NEGATION = re.compile(r"\b(?:no|not|without|never|zero)\b[^.]{0,15}$", re.I)


def _office_hit(text: str) -> re.Match | None:
    """First office/hybrid mention that isn't negated ("no relocation needed")."""
    for m in OFFICE.finditer(text):
        if not NEGATION.search(text[max(0, m.start() - 20):m.start()]):
            return m
    return None


def _evidence(m: re.Match | None, text: str, pad: int = 70) -> str:
    if not m:
        return ""
    start, end = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def classify_workplace(title: str, location: str, description: str) -> tuple[str, str]:
    """-> ('remote' | 'not_remote' | 'unknown', evidence snippet). Strict: when in doubt, not remote."""
    header = f"{title} · {location}"
    if m := _office_hit(header):
        return "not_remote", f"title/location: {header}"
    if m := NOT_REMOTE.search(description):
        return "not_remote", _evidence(m, description)
    if (m := _office_hit(description)) and not REMOTE_EXPLICIT.search(description):
        return "not_remote", _evidence(m, description)
    if re.search(r"\bremot[eoa]\b", header, re.I):  # remote / remoto / remota
        return "remote", f"title/location: {header}"
    for m in REMOTE_STRONG.finditer(description):
        # "Onsite / Remote / Flexible" lists options — not a remote guarantee.
        if not re.search(r"on-?site|hybrid|office", description[max(0, m.start() - 25):m.start()], re.I):
            return "remote", _evidence(m, description)
    if m := REMOTE_WEAK.search(description):
        return "remote", _evidence(m, description)
    return "unknown", ""


@dataclass
class Company:
    size_text: str = ""
    size_min: int | None = None
    size_max: int | None = None
    founded: int | None = None
    org_type: str = ""


async def fetch_company(client: httpx.AsyncClient, url: str) -> Company:
    """Size / founding year / type from the public company page (cached per company)."""
    resp = await client.get(url, headers=HEADERS, timeout=20)
    if resp.status_code in (429, 999):
        raise RateLimited(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return Company()
    tree = HTMLParser(resp.text)

    def field(name: str) -> str:
        el = tree.css_first(f'[data-test-id="about-us__{name}"] dd')
        return " ".join(el.text().split()) if el else ""

    c = Company(size_text=field("size"), org_type=field("organizationType"))
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", c.size_text)]
    if nums:
        c.size_min = nums[0]
        c.size_max = nums[1] if len(nums) > 1 else 10**6  # "10,001+ employees"
    if founded := re.search(r"\d{4}", field("foundedOn")):
        c.founded = int(founded.group(0))
    return c


# LinkedIn's top card on a closed posting ("No longer accepting applications"), or the text saying so.
CLOSED_PAGE = re.compile(r"no longer accepting applications|class=\"[^\"]*closed-job", re.I)
CLOSED_TEXT = re.compile(
    r"\b(?:we are|we're|we\s+are\s+now|is|are)\s+no longer accepting (?:applications|applicants|candidates|resumes)"
    r"|\b(?:this|the) (?:job|position|role|vacancy|opening) (?:has been|is) (?:now )?(?:filled|closed)\b"
    r"|\bapplications? (?:are|is|have) (?:now )?closed\b|\bvaga (?:encerrada|preenchida)\b",
    re.I,
)


def is_closed(page_html: str, description: str) -> bool:
    return bool(CLOSED_PAGE.search(page_html) or CLOSED_TEXT.search(description))


SALARY_RE = re.compile(
    r"\$\s?\d{2,3}(?:[,.]\d{3}|k)(?:\s?(?:-|–|to)\s?\$?\s?\d{2,3}(?:[,.]\d{3}|k))?(?:\s?(?:per|/)\s?(?:year|yr|annum))?"
    r"|\$\s?\d{2,3}(?:\.\d{2})?\s?(?:-|–|to)\s?\$?\s?\d{2,3}(?:\.\d{2})?\s?(?:per|/)\s?(?:hour|hr)",
    re.I,
)


@dataclass
class Details:
    applicants: int | None = None
    applicants_text: str = ""
    workplace: str = "unknown"
    workplace_evidence: str = ""
    description: str = ""
    seniority: str = ""
    employment_type: str = ""
    job_function: str = ""
    industries: str = ""
    company_url: str = ""
    apply_type: str = ""      # offsite | easy_apply | ""
    salary: str = ""


async def fetch_details(client: httpx.AsyncClient, job_id: str, title: str, location: str) -> Details:
    """Everything beyond the search card lives on the job's detail page (one request per job)."""
    resp = await client.get(DETAIL_URL.format(job_id), headers=HEADERS, timeout=20)
    if resp.status_code in (429, 999):
        raise RateLimited(f"HTTP {resp.status_code}")
    if resp.status_code in (404, 410):
        return Details(applicants_text="closed")
    resp.raise_for_status()
    tree = HTMLParser(resp.text)
    d = Details()

    for el in tree.css(".num-applicants__caption"):
        if el.text().strip():
            d.applicants, d.applicants_text = parse_applicants(el.text())
            break

    desc = tree.css_first(".description__text, .show-more-less-html__markup")
    d.description = " ".join(desc.text(separator=" ").split()) if desc else ""
    d.workplace, d.workplace_evidence = classify_workplace(title, location, d.description)
    if is_closed(resp.text, d.description):
        d.applicants, d.applicants_text = None, "closed"

    criteria = {}
    for item in tree.css(".description__job-criteria-item"):
        head = item.css_first(".description__job-criteria-subheader")
        value = item.css_first(".description__job-criteria-text")
        if head and value:
            criteria[" ".join(head.text().split()).lower()] = " ".join(value.text().split())
    d.seniority = criteria.get("seniority level", "")
    d.employment_type = criteria.get("employment type", "")
    d.job_function = criteria.get("job function", "")
    d.industries = criteria.get("industries", "")

    if org := tree.css_first(".topcard__org-name-link"):
        d.company_url = (org.attributes.get("href") or "").split("?")[0]
    # Apply button tracking codes: "apply-link-onsite" / "apply-link-simple_onsite" = Easy Apply.
    if re.search(r"apply-link-(?:simple_)?onsite", resp.text):
        d.apply_type = "easy_apply"
    elif "apply-link-offsite" in resp.text:
        d.apply_type = "offsite"
    salary_el = tree.css_first(".compensation__salary, .salary")
    if salary_el:
        d.salary = " ".join(salary_el.text().split())
    elif m := SALARY_RE.search(d.description):
        d.salary = m.group(0)
    return d
