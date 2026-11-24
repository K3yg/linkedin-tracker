"""Study: turn the jobs you marked into a study plan.

1. Marking a job (the bookmark on its card) has Gemini list that job's requirements once
   (`extract`); the result is stored on the job (jobs.reqs) and never asked again.
2. The Study page counts those requirements across the marked jobs (`aggregate`, no AI): what keeps
   coming up, and whether your CV already shows it.
3. "Build study plan" sends the counted requirements and your CV to Gemini once (`build_plan`) and
   stores the plan under the set of jobs + model, so the same set returns the saved plan.

Needs GEMINI_API_KEY in .env. Model: "ai.study_model" in config.json, else "ai.tailor_model".
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

from . import ai, config, cv, db

log = logging.getLogger(__name__)

Category = Literal["language", "framework", "ai", "data", "cloud", "practice", "domain", "soft"]


class Requirement(BaseModel):
    name: str
    category: Category
    level: Literal["required", "nice"]
    in_cv: bool
    evidence: str


class Requirements(BaseModel):
    requirements: list[Requirement]


class Topic(BaseModel):
    name: str
    why: str
    learn: list[str]
    resources: list[str]
    exercise: str
    hours: int
    requirements: list[str]


class Project(BaseModel):
    name: str
    pitch: str
    covers: list[str]
    stack: list[str]
    milestones: list[str]
    show_it: str


class Strength(BaseModel):
    name: str
    tip: str


class Plan(BaseModel):
    summary: str
    topics: list[Topic]
    projects: list[Project]
    already_have: list[Strength]


EXTRACT = """You read one job posting and list what a candidate must know to do the job, so they can study it.

List 5-15 concrete, learnable requirements: languages, frameworks, tools, AI/LLM techniques (e.g.
"Prompt evaluation", "RAG", "Fine-tuning"), cloud services, engineering practices (e.g. "CI/CD",
"System design"), and domain knowledge (e.g. "Healthcare compliance"). Skip generic items: years of
experience, degrees, English level, "team player", time zones, benefits.

name: a short, common name (2-4 words, Title Case), the way most job posts would say it. When a name
from this list means the same thing, use it exactly, so the same skill is counted together:
{known}
level: "required" if the posting demands it, "nice" if it's a plus / nice to have.
in_cv: true only if the candidate's CV below clearly shows it (same tool or technique, not just related).
evidence: the posting's own words for it, under 15 words.

The candidate's CV:
<cv>
{cv}
</cv>"""

PLAN = """You are a senior engineer mentoring a mid-level developer (about 5 years, Python / TypeScript /
Node / React / LLM work, lives in Brazil) who wants jobs like the ones below. From the requirements
those jobs share, write a practical study plan, in English.

Their CV:
<cv>
{cv}
</cv>

Rules:
- Prioritise requirements that appear in many of the jobs and that the CV doesn't show. Skip ones
  they already have, except in "already_have".
- topics: 4-8, in the order to study them. name: the topic. why: one sentence with how many of the
  jobs ask for it and why it matters. learn: 3-6 key concepts. resources: 2-4 kinds of material or
  well-known official sources by name (docs, a known course or book); no URLs. exercise: one small
  hands-on task to prove it. hours: a realistic estimate. requirements: the requirement names it covers.
- projects: 1-2 portfolio projects that combine several topics, close to what these companies build,
  doable in 1-3 weeks of evenings, in their stack. covers: topic names. milestones: 3-6 steps.
  show_it: how to present it (README, demo, CV bullet).
- already_have: up to 5 requirements the CV already shows, each with one tip to make it more visible.
- summary: two sentences on what these jobs have in common and the main gap.
Be concrete; no filler."""


def model_name(cfg: dict) -> str:
    return cfg["ai"].get("study_model") or cfg["ai"].get("tailor_model") or cfg["ai"]["model"]


def enabled() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def _ask(model: str, system: str, contents: str, schema: type[BaseModel]) -> tuple[BaseModel, dict]:
    resp = await _client().aio.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    u = resp.usage_metadata
    usage = {"in": (u.prompt_token_count or 0) if u else 0,
             "out": ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0}
    answer = resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text)
    return answer, usage


# ---- 1. per job -------------------------------------------------------------------

_working: set[str] = set()


def is_working(job_id: str) -> bool:
    return job_id in _working


def _key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#.]+", " ", name.lower()).split())


def known_names() -> list[str]:
    """Requirement names already used on marked jobs, most common first."""
    counts: dict[str, list] = {}
    for job in db.study_jobs():
        for r in json.loads(job["reqs"] or "[]"):
            counts.setdefault(_key(r["name"]), [r["name"], 0])[1] += 1
    return [n for n, _ in sorted(counts.values(), key=lambda x: -x[1])][:80]


async def extract(job_id: str) -> None:
    """List one job's requirements with Gemini and store them (once per job)."""
    if job_id in _working or not enabled():
        return
    _working.add(job_id)
    try:
        with db.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or job["reqs"] or not job["description"]:
            return
        cfg = config.load()
        model = model_name(cfg)
        known = known_names()
        system = EXTRACT.format(known=", ".join(known) if known else "(none yet)", cv=cv.plain_text())
        answer, usage = await _ask(model, system, ai._job_text(job), Requirements)
        reqs = [r.model_dump() for r in answer.requirements if r.name.strip()]
        db.set_reqs(job_id, reqs, model)
        log.info("study: %d requirement(s) for %s — %s (tokens in %d, out %d)",
                 len(reqs), job["title"], job["company"], usage["in"], usage["out"])
    except Exception:
        log.exception("study: extracting requirements failed for %s", job_id)
    finally:
        _working.discard(job_id)


async def extract_missing() -> None:
    """Marked jobs that have a description but no requirements yet (e.g. marked before a restart)."""
    for job in db.study_jobs():
        if not job["reqs"] and job["description"] and job["id"] not in _working:
            await extract(job["id"])


# ---- 2. patterns (no AI) ----------------------------------------------------------

def aggregate(jobs) -> list[dict]:
    """Requirements across `jobs`, most common first: name, count, jobs asking for it, required
    count, whether the CV shows it, category and one evidence quote."""
    out: dict[str, dict] = {}
    for job in jobs:
        for r in json.loads(job["reqs"] or "[]"):
            a = out.setdefault(_key(r["name"]), {"name": r["name"], "count": 0, "required": 0, "in_cv": 0,
                                                 "category": r["category"], "jobs": [], "evidence": r["evidence"]})
            if job["id"] in {j["id"] for j in a["jobs"]}:
                continue
            a["count"] += 1
            a["required"] += r["level"] == "required"
            a["in_cv"] += bool(r["in_cv"])
            a["jobs"].append({"id": job["id"], "title": job["title"], "company": job["company"]})
    for a in out.values():
        a["in_cv"] = a["in_cv"] * 2 >= a["count"]  # most jobs' reading of the CV agrees
    return sorted(out.values(), key=lambda a: (-a["count"], a["in_cv"], -a["required"], a["name"]))


# ---- 3. the plan --------------------------------------------------------------------

def plan_key(job_ids: list[str], model: str) -> str:
    return hashlib.sha1(json.dumps([sorted(job_ids), model]).encode()).hexdigest()[:16]


_plan_lock = asyncio.Lock()


async def build_plan(job_ids: list[str], rebuild: bool = False) -> int:
    """The saved plan for exactly these jobs, or a new one (one Gemini call). Returns its id."""
    cfg = config.load()
    model = model_name(cfg)
    async with _plan_lock:
        # Keyed on the jobs actually used: one still being read joins once its requirements are in.
        jobs = [j for j in db.study_jobs() if j["id"] in set(job_ids) and j["reqs"]]
        if not jobs:
            raise ValueError("none of these jobs has its requirements yet")
        key = plan_key([j["id"] for j in jobs], model)
        if not rebuild and (saved := db.get_plan(key)):
            return saved["id"]
        reqs = aggregate(jobs)
        lines = [f"Jobs ({len(jobs)}):"] + [f"- {j['title']} — {j['company']}" for j in jobs]
        lines += ["", "Requirements (how many of the jobs ask for it · required in how many · in the CV?):"]
        lines += [f"- {r['name']} [{r['category']}]: {r['count']}/{len(jobs)} · required {r['required']}"
                  f" · {'in CV' if r['in_cv'] else 'NOT in CV'} — e.g. \"{r['evidence']}\"" for r in reqs]
        answer, usage = await _ask(model, PLAN.format(cv=cv.plain_text()), "\n".join(lines), Plan)
        plan = {**answer.model_dump(), "usage": usage, "jobs": [{"id": j["id"], "title": j["title"],
                                                                 "company": j["company"]} for j in jobs]}
        log.info("study: plan for %d job(s) (tokens in %d, out %d)", len(jobs), usage["in"], usage["out"])
        return db.save_plan(key, [j["id"] for j in jobs], model, plan)
