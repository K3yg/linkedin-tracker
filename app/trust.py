"""Flag postings that look fake, spammy or low quality.

Hard flags hide a job on their own; soft flags only hide it when two or more pile up.
Every flag keeps a human-readable reason so the dashboard and intel reports can explain it.
Rules and word lists live under "trust" in config.json.
"""

import json
import re
from collections import Counter

from . import config, db
from .roles import _pattern

MARKDOWN = re.compile(r"(?:^|\s)#{1,3} |\*\*[^*]{2,60}\*\*")
EMPLOYER = re.compile(r"(?:^|[.!?]\s+)([A-Z][\w&.'\- ]{1,40}?) (?:is|are) (?:seeking|hiring|looking for)\b")
LEGAL_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|gmbh|plc|pvt|limited)\b\.?", re.I)


def norm_company(name: str | None) -> str:
    return " ".join(LEGAL_SUFFIX.sub("", (name or "").lower()).replace(",", " ").split())


def norm_title(title: str | None) -> str:
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", t).split())


def evaluate(job, same_title: int, company_total: int, t: dict, patterns: dict) -> list[dict]:
    flags: list[dict] = []

    def flag(reason: str, hard: bool) -> None:
        flags.append({"reason": reason, "hard": hard})

    company = job["company"] or ""
    text = f"{job['title']} {job['description'] or ''}"

    if patterns["blocked"] and patterns["blocked"].search(company):
        flag("blocked company", True)
    # Soft, and one flag even when both apply: posting one role in several cities raises both
    # counts, and big employers do that legitimately (ElevenLabs: same title in 8 cities).
    volume = []
    if same_title >= t["max_same_title_per_company"]:
        volume.append(f"same title posted {same_title}× by this company")
    if company_total >= t["max_jobs_per_company_week"]:
        volume.append(f"{company_total} postings from this company this week")
    if volume:
        flag(" · ".join(volume), False)
    if patterns["hide"]:
        for phrase in sorted({m.lower() for m in patterns["hide"].findall(text)}):
            flag(f"mentions “{phrase}”", True)
    if patterns["warn"]:
        for phrase in sorted({m.lower() for m in patterns["warn"].findall(text)}):
            flag(f"mentions “{phrase}”", False)

    desc = job["description"] or ""
    # Literal Markdown (##, **bold**) never appears in real LinkedIn posts: copied / AI-rewritten repost.
    if len(MARKDOWN.findall(desc)) >= 3:
        flag("copied listing (Markdown-formatted repost)", True)
    if (m := EMPLOYER.search(desc[:400])) and company:
        named = m.group(1).strip()
        generic = named.split()[0].lower() in {"we", "our", "the", "this", "they", "you", "it", "who", "my", "a", "an"}
        if not generic and norm_company(named) not in norm_company(company) \
                and norm_company(company) not in norm_company(named):
            flag(f"description names another employer ({named})", False)

    # Only judge the detail page if it was read with the current scraper (description stored).
    if job["description"] is not None and job["applicants_text"] != "closed":
        desc_len = len(job["description"] or "")
        if desc_len < t["min_description_chars"]:
            flag(f"very short description ({desc_len} chars)", False)
        if "staffing" in (job["industries"] or "").lower():
            flag("staffing / recruiting agency", False)
        if job["employment_type"] in ("Contract", "Temporary", "Part-time"):
            flag(f"{job['employment_type'].lower()} role", False)
        if not job["company_url"]:
            flag("no LinkedIn company page", False)
    return flags


def refresh() -> list[str]:
    """Re-score every To Apply job. Returns ids that became suspicious in this pass."""
    cfg = config.load()
    t = cfg["trust"]
    patterns = {
        "blocked": _pattern(t["blocked_companies"]),
        "hide": _pattern(t["hide_phrases"]),
        "warn": _pattern(t["warn_phrases"]),
    }
    trusted = {norm_company(c) for c in t["trusted_companies"]}

    recent = db.recent_jobs(days=7)
    by_company = Counter(norm_company(r["company"]) for r in recent)
    by_company_title = Counter((norm_company(r["company"]), norm_title(r["title"])) for r in recent)

    updates, newly = [], []
    for job in db.all_jobs():
        if job["status"] != "new":
            continue
        company = norm_company(job["company"])
        if company in trusted:
            flags = []
        else:
            flags = evaluate(
                job,
                by_company_title[(company, norm_title(job["title"]))],
                by_company[company],
                t,
                patterns,
            )
        hard = any(f["hard"] for f in flags)
        soft = sum(not f["hard"] for f in flags)
        suspicious = int(hard or soft >= t["soft_flags_to_hide"])
        if suspicious and not job["suspicious"]:
            newly.append(job["id"])
        updates.append((job["id"], json.dumps(flags), suspicious))
    db.set_flags(updates)
    return newly


def parse(flags_json: str | None) -> list[dict]:
    return json.loads(flags_json) if flags_json else []
