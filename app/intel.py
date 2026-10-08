"""Write what each scan learned to intel/ — a Markdown report plus a JSONL dataset.

intel/latest.md          summary of the last scan + the whole dataset (paste this into an AI)
intel/scans/<time>.md    one report per scan
intel/jobs.jsonl         one JSON object per checked job, incl. your applied/dismissed labels
"""

import json
import re
from collections import Counter
from datetime import datetime

from . import config, db, trust
from .config import ROOT

OUT_DIR = ROOT / "intel"  # the scout run points this at intel/scout
EXCERPT_CHARS = 700

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
EUROPE = re.compile(
    r"\b(europe|european union|emea|united kingdom|uk|england|scotland|ireland|germany|france|spain|portugal|"
    r"italy|netherlands|belgium|switzerland|austria|poland|czech|sweden|norway|denmark|finland|romania|"
    r"hungary|greece|bulgaria|croatia|serbia|ukraine|estonia|latvia|lithuania|luxembourg|slovakia|slovenia|"
    r"london|berlin|paris|madrid|lisbon|amsterdam|dublin|warsaw|munich|barcelona|stockholm|zurich)\b",
    re.I,
)
LABELS = {"new": "unreviewed", "applied": "applied (liked)", "dismissed": "dismissed (not interested)"}


def region(location: str | None) -> str:
    loc = location or ""
    if not loc:
        return "unknown"
    state = re.search(r",\s*([A-Z]{2})\b", loc)
    if "united states" in loc.lower() or (state and state.group(1) in US_STATES):
        return "US"
    if EUROPE.search(loc):
        return "Europe"
    if re.search(r"\b(greater .* area|metropolitan|bay area|metroplex)\b", loc, re.I):
        return "US"  # LinkedIn metro names are almost always US
    return "other"


def job_record(r, scope=None) -> dict:
    desc = r["description"] or ""
    return {
        "id": r["id"],
        "url": r["url"],
        "title": r["title"],
        "company": r["company"],
        "company_page": r["company_url"],
        "location": r["location"],
        "region": region(r["location"]),
        "posted_at": r["posted_at"],
        "applicants": r["applicants_text"],
        "apply_type": r["apply_type"],
        "workplace": r["workplace"],
        "workplace_evidence": r["workplace_evidence"],
        "remote_from": scope(r["title"], r["location"], r["description"])[0] if scope else None,
        "seniority": r["seniority"],
        "employment_type": r["employment_type"],
        "job_function": r["job_function"],
        "industries": r["industries"],
        "salary": r["salary"],
        "trust_flags": [f["reason"] for f in trust.parse(r["flags"])],
        "suspicious": bool(r["suspicious"]),
        "my_label": LABELS.get(r["status"], r["status"]),
        "description_excerpt": desc[:EXCERPT_CHARS] + ("…" if len(desc) > EXCERPT_CHARS else ""),
    }


def _table(rows: list[list], head: list[str]) -> str:
    def cell(v) -> str:
        return str(v if v not in (None, "") else "—").replace("|", "/").replace("\n", " ")
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _dist(counter: Counter, total: int, top: int = 8) -> str:
    if not total:
        return "—"
    return ", ".join(f"{k or 'n/a'} {v} ({v * 100 // total}%)" for k, v in counter.most_common(top))


def write(stats: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scans").mkdir(exist_ok=True)
    rows = db.all_jobs()
    checked = [r for r in rows if r["applicants_checked_at"]]
    by_id = {r["id"]: r for r in rows}
    scope = db.functions(config.load()).scope

    with open(OUT_DIR / "jobs.jsonl", "w", encoding="utf-8") as f:
        for r in checked:
            f.write(json.dumps(job_record(r, scope), ensure_ascii=False) + "\n")

    now = datetime.now().astimezone()
    md = [f"# Job scan report — {now:%Y-%m-%d %H:%M}", ""]

    md += ["## This scan", ""]
    md += [f"- Search queries: {stats.get('queries', 0)} · job cards seen: {stats.get('cards', 0)}",
           f"- New jobs saved: {stats.get('saved', 0)} · dropped by title filter: {stats.get('title_filtered', 0)}",
           f"- Detail pages read: {stats.get('checked', 0)} → remote {stats.get('remote', 0)}, "
           f"not remote {stats.get('not_remote', 0)}, unclear {stats.get('unknown', 0)}",
           f"- Newly flagged as suspicious: {len(stats.get('flagged_ids', []))}"]
    if stats.get("error"):
        md.append(f"- ⚠ Stopped early: {stats['error']}")
    md.append("")

    new_remote = [by_id[i] for i in stats.get("remote_ids", []) if i in by_id and not by_id[i]["suspicious"]]
    if new_remote:
        md += ["### New verified-remote jobs", "", _table(
            [[r["title"], r["company"], r["location"], r["applicants_text"],
              "Easy Apply" if r["apply_type"] == "easy_apply" else "external",
              r["salary"], r["seniority"], (r["workplace_evidence"] or "")[:120]] for r in new_remote],
            ["Title", "Company", "Location", "Applicants", "Apply", "Salary", "Seniority", "Remote evidence"]), ""]

    flagged = [by_id[i] for i in stats.get("flagged_ids", []) if i in by_id]
    if flagged:
        md += ["### Newly flagged", "", _table(
            [[r["title"], r["company"], "; ".join(f["reason"] for f in trust.parse(r["flags"]))] for r in flagged],
            ["Title", "Company", "Why"]), ""]

    total = len(checked)
    remote = [r for r in checked if r["workplace"] == "remote"]
    md += ["## Dataset overview", "",
           f"{total} jobs with details ({len(rows)} saved in total).", "",
           f"- Workplace: {_dist(Counter(r['workplace'] for r in checked), total)}",
           f"- Region: {_dist(Counter(region(r['location']) for r in checked), total)}",
           f"- Remote jobs workable from Brazil: "
           f"{_dist(Counter(scope(r['title'], r['location'], r['description'])[0] for r in remote), len(remote))}",
           f"- Seniority: {_dist(Counter(r['seniority'] for r in checked), total)}",
           f"- Employment type: {_dist(Counter(r['employment_type'] for r in checked), total)}",
           f"- Apply type: {_dist(Counter(r['apply_type'] for r in checked), total)}",
           f"- Applicants: {_dist(Counter(r['applicants_text'] for r in checked), total, 6)}",
           f"- Industries: {_dist(Counter(r['industries'] for r in checked), total, 6)}",
           f"- Suspicious: {sum(1 for r in checked if r['suspicious'])} of {total}",
           ""]

    companies = Counter(r["company"] for r in rows)
    flagged_by_company = Counter(r["company"] for r in rows if r["suspicious"])
    md += ["### Most active companies", "", _table(
        [[c, n, flagged_by_company[c]] for c, n in companies.most_common(15)],
        ["Company", "Postings", "Flagged"]), ""]

    reasons = Counter(re.sub(r"\d+", "N", f["reason"]) for r in rows for f in trust.parse(r["flags"]))
    if reasons:
        md += ["### Most common trust flags", "",
               _table([[k, v] for k, v in reasons.most_common(12)], ["Flag", "Jobs"]), ""]

    liked = [r for r in rows if r["status"] == "applied"]
    disliked = [r for r in rows if r["status"] == "dismissed"]
    md += ["## Your labels", "",
           f"Applied: {len(liked)} · Dismissed: {len(disliked)}", ""]
    for label, group in (("Applied", liked), ("Dismissed", disliked)):
        if group:
            md += [f"**{label} (latest 20):**", ""]
            md += [f"- {r['title']} — {r['company']} ({r['location']}; {r['applicants_text'] or '?'} applicants; "
                   f"{r['seniority'] or 'n/a'})" for r in group[:20]]
            md.append("")

    md += ["## Using this with an AI", "",
           "Paste this report (and, for detail, `intel/jobs.jsonl`) into an AI with a prompt like:", "",
           "> Here is data from my LinkedIn job tracker. `my_label` shows what I applied to or dismissed.",
           "> 1) Describe what a good opportunity looks like for me. 2) Suggest better search keywords and",
           "> locations. 3) Point out companies or patterns that look fake, spammy or low quality, and",
           "> propose rules (title keywords, blocked companies, description phrases) to filter them.", ""]

    text = "\n".join(md)
    (OUT_DIR / "latest.md").write_text(text, encoding="utf-8")
    (OUT_DIR / "scans" / f"{now:%Y-%m-%d_%H%M}.md").write_text(text, encoding="utf-8")
