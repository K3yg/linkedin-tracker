"""How well a job fits the user, and which ones to put first.

- fit:   points for skills from the user's profile (config "profile") found in the title
         and description, minus points for stacks they don't work with.
- match: fit + open to the user's region + startup-ness + level + Easy Apply + competition + freshness
         (higher = better). Level favours mid-level (the user has ~5 years): "Engineer II", "Mid",
         "Pleno", 2-5 years asked; a "Senior" title costs less when it only asks for <= 5 years.
"""

import re
from datetime import datetime, timezone

from .checks import years_required
from .roles import _pattern

MAX_AGE_HOURS = 72
SENIOR = re.compile(r"(?<![a-z])(s[eê]nior|sr\.?)(?![a-z])", re.I)
MID_SENIOR = re.compile(r"mid[- ]s[eê]nior", re.I)  # LinkedIn's "Mid-Senior" level isn't senior
MID = re.compile(
    r"(?<![a-z])(?:mid|mid[- ]?level|mid[- ]senior|intermediate|pleno|plena|semi[- ]?senior|ssr)(?![a-z])"
    r"|(?<![a-z])(?:engineer|developer|sde|swe|programmer)\s*(?:ii|2)(?![a-z0-9])",
    re.I,
)
STARTUP_SIGNALS = re.compile(
    r"\bstart-?up\b|\bseed\b|\bseries [abc]\b|\bearly[- ]stage\b|\bfounding (?:engineer|team)\b"
    r"|\by ?combinator\b|\byc[- ]backed\b|\bventure[- ]backed\b|\bsmall team\b",
    re.I,
)


def age_hours(job) -> float:
    if not job["posted_at"]:
        return MAX_AGE_HOURS
    posted = datetime.fromisoformat(job["posted_at"])
    return (datetime.now(timezone.utc) - posted).total_seconds() / 3600


def build_fit(cfg: dict):
    """-> fit(title, description) -> int, from config profile.skills / profile.avoid weights."""
    profile = cfg["profile"]
    # The first word as a plain substring check skips the regex for the many absent terms.
    terms = [(t.lower().split()[0], w, _pattern([t]))
             for t, w in {**profile["skills"], **profile["avoid"]}.items() if t.strip()]

    def fit(title: str | None, description: str | None) -> int:
        title, description = title or "", description or ""
        lower_title, lower_desc = title.lower(), description.lower()
        score = 0
        for first, weight, pattern in terms:
            if first in lower_title and pattern.search(title):
                score += weight * 2 if weight > 0 else weight
            elif first in lower_desc and pattern.search(description):
                score += weight
        return score

    return fit


def is_senior(job) -> bool:
    return bool(SENIOR.search(MID_SENIOR.sub("", job["title"] or "")))


def is_mid(job) -> bool:
    return bool(MID.search(job["title"] or ""))


def company_kind(company) -> str:
    """startup | midsize | big | '' (unknown), from the cached LinkedIn company page."""
    if not company or company["size_max"] is None:
        return ""
    founded_recently = (company["founded"] or 0) >= datetime.now().year - 8
    if company["size_max"] <= 200 or (founded_recently and company["size_max"] <= 500):
        return "startup"
    if company["size_min"] >= 5001 or (company["org_type"] == "Public Company" and company["size_min"] >= 1001):
        return "big"
    return "midsize"


def match_score(job, company, fit_score: int, scope: str = "unstated") -> float:
    """scope: where the job can be done from (region.py); open to Brazil/worldwide ranks first."""
    kind = company_kind(company)
    score = float(fit_score)
    score += {"open": 12, "unstated": -4}.get(scope, 0)
    score += {"startup": 10, "midsize": 3, "big": -8}.get(kind, 0)
    if not kind and STARTUP_SIGNALS.search(job["description"] or ""):
        score += 5
    years = years_required(job["description"])
    if is_senior(job):
        score -= 4 if years and years <= 5 else 10
    elif is_mid(job):
        score += 6
    if years:
        score += 4 if 2 <= years <= 5 else -3 if years >= 6 else 0
    if job["apply_type"] == "easy_apply":
        score += 6
    if job["ai_score"] is not None:  # Gemini's read of the whole posting (ai.py)
        score += (job["ai_score"] - 50) / 3
    n = job["applicants"]
    if n is not None:
        score += 8 if n < 25 else 3 if n < 100 else -5 if n > 200 else 0
    score -= min(age_hours(job), MAX_AGE_HOURS) * 0.15
    return score


def top_picks(jobs, exclude_title_words: list[str], n: int = 5) -> list:
    """jobs: annotated dicts with a 'match' key. Best matches that aren't too senior."""
    words = [w for w in exclude_title_words if w.strip()]
    excluded = re.compile(r"(?<![a-z])(" + "|".join(map(re.escape, words)) + r")(?![a-z])", re.I) if words else None
    candidates = [
        j for j in jobs
        if j["applicants"] is not None
        and age_hours(j) <= MAX_AGE_HOURS
        and not (excluded and excluded.search(j["title"]))
    ]
    return sorted(candidates, key=lambda j: j["match"], reverse=True)[:n]
