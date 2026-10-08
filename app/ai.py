"""Gemini reads a job (that already passed every rule-based filter) next to the user's CV and
says whether it's worth applying: apply / maybe / skip, a 0-100 score and short reasons.

Needs GEMINI_API_KEY in .env (PROFILE_ABOUT there too: a one-line summary of you). Settings under "ai" in config.json (model, per_scan, hide_skips).
"""

import logging
import os
from typing import Literal

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from . import config, cv, db

log = logging.getLogger(__name__)
MAX_DESCRIPTION_CHARS = 9000


class Verdict(BaseModel):
    verdict: Literal["apply", "maybe", "skip"]
    score: int
    summary: str
    reasons: list[str]


INSTRUCTIONS = """You screen job postings for one candidate and decide if applying is worth their time.

Candidate: {about}
They are mid-level (about 5 years of experience) and live in Brazil.

Their CV:
<cv>
{cv}
</cv>

Judge the posting below. Use "skip" when any of these is true:
- it can't be done 100% remotely from Brazil (requires living in, or work authorization for, another
  country, or office presence);
- it isn't really a software engineering role (data annotation / AI training gigs, sales, support,
  pure DevOps, QA…), or it's fake, vague or a reposting aggregator with no real employer;
- it's clearly above their level (staff, principal, lead, manager, or 7+ years required) or an internship;
- their stack barely overlaps (e.g. a Java/.NET/Go/mobile-only role).
Use "apply" when the stack and level match well and nothing blocks them; "maybe" for in-between cases
(partial stack overlap, unclear remote policy, contract with unclear terms…). Missing a few
nice-to-haves is not a reason to skip. Contract / contractor roles are fine for someone in Brazil.

score: 0-100, how good an application this is for them (fit, level, chances).
summary: one sentence, in English.
reasons: 2-4 short reasons (under 12 words each), the decisive ones first."""


def enabled(cfg: dict) -> bool:
    return bool(cfg["ai"].get("enabled")) and bool(os.environ.get("GEMINI_API_KEY"))


def _job_text(job) -> str:
    fields = [
        ("Title", job["title"]), ("Company", job["company"]), ("Location", job["location"]),
        ("Seniority", job["seniority"]), ("Employment type", job["employment_type"]),
        ("Industries", job["industries"]), ("Salary", job["salary"]), ("Applicants", job["applicants_text"]),
    ]
    head = "\n".join(f"{k}: {v}" for k, v in fields if v)
    return f"{head}\n\nDescription:\n{(job['description'] or '')[:MAX_DESCRIPTION_CHARS]}"


async def judge(client: genai.Client, cfg: dict, job) -> Verdict:
    resp = await client.aio.models.generate_content(
        model=cfg["ai"]["model"],
        contents=_job_text(job),
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCTIONS.format(about=os.environ.get("PROFILE_ABOUT", ""), cv=cv.plain_text()),
            response_mime_type="application/json",
            response_schema=Verdict,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    v = resp.parsed if isinstance(resp.parsed, Verdict) else Verdict.model_validate_json(resp.text)
    v.score = max(0, min(100, v.score))
    return v


async def judge_pending(limit: int) -> int:
    """Judge up to `limit` jobs that pass the rules but have no verdict yet. Returns how many."""
    cfg = config.load()
    if not enabled(cfg) or limit <= 0:
        return 0
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    done = 0
    for job in db.jobs_for_ai(limit):
        try:
            v = await judge(client, cfg, job)
        except errors.APIError as e:
            if e.code == 429:  # quota: try again next scan
                log.warning("Gemini quota reached after %d job(s): %s", done, e.message)
                break
            log.warning("Gemini failed on %s: %s", job["id"], e)
            continue
        except Exception:
            log.exception("Gemini answer for %s unusable", job["id"])
            continue
        db.set_ai(job["id"], v.verdict, v.score, v.summary, v.reasons, cfg["ai"]["model"])
        done += 1
        log.info("AI %-5s %3d  %s — %s", v.verdict, v.score, job["title"], job["company"])
    return done


if __name__ == "__main__":  # uv run python -m app.ai [N]: judge up to N waiting jobs now
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    print(f"judged {asyncio.run(judge_pending(int(sys.argv[1]) if len(sys.argv) > 1 else 50))} job(s)")
