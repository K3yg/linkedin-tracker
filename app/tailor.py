"""Tailor the CV (cv/base.tex) to one job without inventing anything.

Gemini gets the CV as numbered bullets per job (E1.1, E1.2…) plus the posting, and returns a new
headline and, for every job in the CV, its bullets reordered / reworded / dropped, each pointing
at the original bullet it came from. The .tex is rebuilt from base.tex, so the header, contact
details, companies, titles and dates never go through the AI.

Never-invent checks on every bullet (one retry with the problems listed, then the original
wording is kept — or, in a Portuguese CV, the bullet is dropped):
- it comes from a bullet of the same job, used once;
- no number that isn't in that original bullet;
- no technology the same job's bullets don't mention (headline: anywhere in the CV);
- not much longer than the original (the CV must stay on one page).

Output in cv/out/<job id>/: cv.tex, cv.pdf (compiled by tools/tectonic.exe), changes.json.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from google import genai
from google.genai import types
from pydantic import BaseModel
from pypdf import PdfReader

from . import checks, config, cv, db
from .config import ROOT

log = logging.getLogger(__name__)
OUT_DIR = ROOT / "cv" / "out"
TECTONIC = ROOT / "tools" / "tectonic.exe"
MAX_PAGE_FIT_DROPS = 6
_compile_lock = asyncio.Lock()  # one Tectonic run at a time (shared package cache)

# ---- the base CV ----------------------------------------------------------------

ENTRY = re.compile(r"\\entry\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}")
ITEMIZE = re.compile(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", re.S)
HEADLINE = re.compile(r"(\\begin\{center\}\s*\\textbf\{)(.*?)(\}\s*\\end\{center\})", re.S)


@dataclass
class Entry:
    id: str          # "E1"
    company: str
    title: str
    dates: str
    bullets: list[str]  # plain text


def latex_to_text(s: str) -> str:
    s = re.sub(r"\\setlength\\\w+\{[^}]*\}", " ", s)
    for a, b in ((r"\textasciitilde{}", "~"), (r"\textasciitilde", "~"), (r"\textasciicircum{}", "^"),(r"\textbar\ ", "| "), (r"\textbar", "|"), (r"\&", "&"),
                 (r"\%", "%"), (r"\$", "$"), (r"\#", "#"), (r"\_", "_"), ("--", "–")):
        s = s.replace(a, b)
    s = re.sub(r"\\fa[A-Za-z]+|\\space\b", " ", s)
    s = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", s)
    s = re.sub(r"\\[a-zA-Z]+|[{}]", " ", s)
    return " ".join(s.split())


def text_to_latex(s: str) -> str:
    out = []
    for ch in " ".join(s.split()):
        out.append({"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
                    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}.get(ch, ch))
    return "".join(out)


def parse(tex: str) -> tuple[str, list[Entry]]:
    m = HEADLINE.search(tex)
    headline = latex_to_text(m.group(2)) if m else ""
    entries = []
    for i, e in enumerate(ENTRY.finditer(tex), 1):
        block = ITEMIZE.search(tex, e.end())
        items = re.split(r"\\item\b", block.group(1))[1:] if block else []
        entries.append(Entry(f"E{i}", latex_to_text(e.group(1)), latex_to_text(e.group(3)),
                             latex_to_text(e.group(2)), [latex_to_text(t) for t in items]))
    return headline, entries


# ---- never-invent checks ----------------------------------------------------------

TECH = (
    "python|typescript|javascript|java|golang|rust|c#|\\.net|c\\+\\+|php|ruby|rails|kotlin|swift|scala|elixir|"
    "react|react\\.js|react native|next\\.js|vue|angular|svelte|node|node\\.js|nodejs|nestjs|django|flask|fastapi|"
    "celery|huey|kafka|rabbitmq|redis|postgresql|postgres|mysql|sqlite|mongodb|dynamodb|elasticsearch|graphql|"
    "grpc|docker|kubernetes|terraform|aws|gcp|azure|lambda|sqs|sns|s3|ec2|ecs|ses|cloudwatch|llm|llms|rag|"
    "langchain|llamaindex|openai|anthropic|claude|gemini|mcp|pytorch|tensorflow|pandas|numpy|spark|airflow|"
    "selenium|playwright|pl/sql|sql|bash|tailwind|html|css|jwt|pymupdf|ocr|hipaa|fhir|hl7|ehr|emr|soc ?2|ci/cd|"
    "github actions|microservices|serverless|jira|scrum|agile|machine learning|deep learning|nlp|"
    "generative ai|genai|large language models?|vector databases?|embeddings|fine-tuning|restful|rest apis?|"
    "erp|etl|websockets?"
)
TECH_RE = re.compile(rf"(?<![A-Za-z0-9])(?:{TECH})(?![A-Za-z0-9])", re.I)
SYNONYMS = {
    "postgres": "postgresql", "node": "node.js", "nodejs": "node.js", "react.js": "react", "llms": "llm",
    "generative ai": "llm", "genai": "llm", "large language model": "llm", "large language models": "llm",
    "rest api": "restful", "rest apis": "restful", "emr": "ehr", "soc2": "soc 2", "websocket": "websockets",
    "vector database": "vector databases",
}
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def tech_terms(text: str) -> set[str]:
    return {SYNONYMS.get(t.lower(), t.lower()) for t in TECH_RE.findall(text)}


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").replace(".", "") for n in NUMBER.findall(text)}


# Upgrading the user's role in a piece of work: "contributed to" -> "led" / "architected".
HUMBLE = re.compile(r"\b(?:contribut\w*|help\w*|assist\w*|participat\w*|supported|gained experience)", re.I)
GRAND = re.compile(r"\b(?:led|lead|leading|architect\w*|spearhead\w*|own(?:ed|ing)?|head(?:ed)?|"
                   r"liderei|lider\w*|arquitet\w*|at scale|high[- ]traffic|mission[- ]critical)\b", re.I)


def bullet_problems(text: str, source: str, entry_text: str, translated: bool) -> list[str]:
    problems = []
    if (extra := {m.lower() for m in GRAND.findall(text)} - {m.lower() for m in GRAND.findall(source)}):
        problems.append(f"inflates your role/scope with {', '.join(sorted(extra))}")
    if not translated and HUMBLE.search(source) and not HUMBLE.search(text):
        problems.append(f"drops '{HUMBLE.search(source).group(0)}' — your part in this work must stay as stated")
    if extra := numbers(text) - numbers(source):
        problems.append(f"adds numbers not in the original bullet: {', '.join(sorted(extra))}")
    if extra := tech_terms(text) - tech_terms(entry_text):
        problems.append(f"mentions {', '.join(sorted(extra))}, which this job in the CV doesn't")
    limit = max(len(source) * (1.35 if translated else 1.2), len(source) + 40)
    if len(text) > limit:
        problems.append(f"too long ({len(text)} chars, original {len(source)})")
    return problems


# ---- the AI ---------------------------------------------------------------------

class Bullet(BaseModel):
    source: str  # id of the original bullet, e.g. "E1.3"
    text: str
    why: str


class JobBullets(BaseModel):
    entry: str  # "E1"
    bullets: list[Bullet]


class Tailored(BaseModel):
    headline: str
    headline_why: str
    entries: list[JobBullets]


INSTRUCTIONS = """You tailor a software engineer's CV to one job posting. You may only use facts that are
already in the CV: never add a technology, number, employer, responsibility or result that isn't there.

You get the CV as jobs (E1, E2…) with numbered bullets (E1.1, E1.2…). Return:
- headline: the short line under the name (now "{headline}"), at most 8 words, aimed at this posting
  but true to the CV (e.g. "Python | LLM & Full-Stack Engineer");
- for EVERY job E1…E{n}: its bullets, most relevant to the posting first. Each bullet has "source" (the
  id of the original bullet it rewrites, each used at most once, only from that same job), "text" and
  "why" (under 12 words: why it's placed or worded this way for this posting).
Rewording rules — stay faithful:
- Use the posting's vocabulary only where it truthfully names the same work; lead with what the
  posting cares about; not longer than the original.
- Never inflate scope or role: "contributed to" stays "contributed to" (not engineered / architected /
  led / owned); a feature stays a feature; don't add claims like "at scale", "high-traffic", "core".
- Keep every number exactly, and keep what it measures ("download time" stays download time).
- Don't merge bullets or move work between jobs.
Selection: the CV already fits one page, so keep it about as full as it is. Drop a bullet only when
it's clearly irrelevant to this posting (at most 1 per job), and never drop the strongest achievements
of the most recent job (concrete results and numbers). Keep at least 2 bullets per job.
Write the headline and bullets in {language}."""

LANGUAGE_NAMES = {"en": "English", "pt": "Brazilian Portuguese (natural and professional, verbs in the first person "
                                         "past tense — Desenvolvi, Construí; keep technology names as they are)"}


def _cv_for_prompt(entries: list[Entry]) -> str:
    lines = []
    for e in entries:
        lines.append(f"{e.id} — {e.company} — {e.title} — {e.dates}")
        lines += [f"  {e.id}.{i}: {b}" for i, b in enumerate(e.bullets, 1)]
    return "\n".join(lines)


async def _ask(client: genai.Client, model: str, system: str, contents: list) -> Tailored:
    resp = await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=Tailored,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    return resp.parsed if isinstance(resp.parsed, Tailored) else Tailored.model_validate_json(resp.text)


def _review(answer: Tailored, entries: list[Entry], headline_terms: set[str], translated: bool) -> list[str]:
    """Every rule the answer breaks, as sentences the model can act on."""
    problems = []
    if extra := tech_terms(answer.headline) - headline_terms:
        problems.append(f"headline mentions {', '.join(sorted(extra))}, which the CV doesn't")
    by_id = {e.id: e for e in entries}
    for j in answer.entries:
        e = by_id.get(j.entry)
        if not e:
            problems.append(f"{j.entry} is not a job in the CV")
            continue
        entry_text = " ".join([e.title, *e.bullets])
        used = set()
        for b in j.bullets:
            src = _source(e, b.source)
            if src is None:
                problems.append(f"{b.source} is not a bullet of {e.id}")
            elif b.source in used:
                problems.append(f"{b.source} used twice")
            else:
                problems += [f"{b.source}: {p}" for p in bullet_problems(b.text, src, entry_text, translated)]
            used.add(b.source)
    return problems


def _source(e: Entry, source_id: str) -> str | None:
    m = re.fullmatch(rf"{e.id}\.(\d+)", source_id.strip())
    return e.bullets[int(m.group(1)) - 1] if m and 0 < int(m.group(1)) <= len(e.bullets) else None


# ---- building the .tex ------------------------------------------------------------

PT = {
    "January": "Janeiro", "February": "Fevereiro", "March": "Março", "April": "Abril", "May": "Maio",
    "June": "Junho", "July": "Julho", "August": "Agosto", "September": "Setembro", "October": "Outubro",
    "November": "Novembro", "December": "Dezembro", "Present": "Atual", "(Remote)": "(Remoto)",
    "(On-Site)": "(Presencial)", "(Hybrid)": "(Híbrido)", r"\section*{Experience}": r"\section*{Experiência}",
}


def render(tex: str, headline: str, bullets_by_entry: list[list[str]], language: str) -> str:
    """base.tex with a new headline and new bullet lists; everything else untouched (translated
    word-for-word for a Portuguese CV: month names, Present, Remote / On-Site, section title)."""
    entry_matches = list(ENTRY.finditer(tex))
    for e, bullets in reversed(list(zip(entry_matches, bullets_by_entry))):
        block = ITEMIZE.search(tex, e.end())
        body = block.group(1)
        first = re.search(r"\\item\b", body)  # not the \item in \itemsep
        prefix = body[:first.start()] if first else body
        items = "".join(f"\\item {text_to_latex(b)}\n" for b in bullets)
        tex = tex[:block.start(1)] + prefix + items + tex[block.end(1):]
        if language == "pt":
            line = e.group(0)
            for a, b in PT.items():
                line = line.replace(a, b)
            tex = tex[:e.start()] + line + tex[e.end():]
    tex = HEADLINE.sub(lambda m: m.group(1) + text_to_latex(headline) + m.group(3), tex, count=1)
    if language == "pt":
        tex = tex.replace(r"\section*{Experience}", PT[r"\section*{Experience}"])
    return tex


async def compile_pdf(tex_path) -> int:
    """Compile with Tectonic; returns the page count."""
    async with _compile_lock:
        proc = await asyncio.create_subprocess_exec(
            str(TECTONIC), "-X", "compile", str(tex_path), "--outdir", str(tex_path.parent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    if proc.returncode != 0:
        tail = "\n".join(out.decode("utf-8", "replace").splitlines()[-8:])
        raise RuntimeError(f"LaTeX failed:\n{tail}")
    return len(PdfReader(tex_path.with_suffix(".pdf")).pages)


# ---- the whole thing ------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w%~]+", " ", s.lower()).split())


async def tailor(job_id: str) -> None:
    """Build cv/out/<job_id>/ for one job and record the result on the job (cv_* columns)."""
    cfg = config.load()
    model = cfg["ai"].get("tailor_model") or cfg["ai"]["model"]
    try:
        with db.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise RuntimeError("job not found")
        if not job["description"]:
            raise RuntimeError("the job's description hasn't been read yet")
        changes = await _tailor(job, model)
        db.set_cv(job_id, "done", "", json.dumps(changes, ensure_ascii=False), changes["language"], model)
        log.info("tailored CV for %s — %s", job["title"], job["company"])
    except Exception as e:
        log.exception("CV tailoring failed for %s", job_id)
        db.set_cv(job_id, "error", str(e)[:500], None, None, model)


async def _tailor(job, model: str) -> dict:
    tex = cv.BASE_TEX.read_text(encoding="utf-8")
    old_headline, entries = parse(tex)
    language = "pt" if checks.language(job["description"]) == "pt" else "en"
    translated = language != "en"
    headline_terms = tech_terms(cv.plain_text())

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    system = INSTRUCTIONS.format(headline=old_headline, n=len(entries), language=LANGUAGE_NAMES[language])
    posting = (f"Job posting — {job['title']} at {job['company']} ({job['location']})\n\n"
               f"{job['description'][:9000]}\n\nCV:\n{_cv_for_prompt(entries)}")
    contents: list = [posting]
    answer = await _ask(client, model, system, contents)
    problems = _review(answer, entries, headline_terms, translated)
    retried = bool(problems)
    if problems:  # one retry with the problems spelled out
        contents += [types.Content(role="model", parts=[types.Part(text=answer.model_dump_json())]),
                     "These parts break the rules; fix them and return the whole answer again:\n- "
                     + "\n- ".join(problems)]
        answer = await _ask(client, model, system, contents)

    # Apply the answer, keeping only what passes the checks.
    fallbacks: list[str] = []
    headline = answer.headline.strip() or old_headline
    if extra := tech_terms(headline) - headline_terms:
        fallbacks.append(f"kept your headline: the AI's mentioned {', '.join(sorted(extra))}")
        headline = old_headline
    by_entry = {j.entry: j for j in answer.entries}
    plan: list[list[dict]] = []  # per entry: [{text, source, original, why}]
    for e in entries:
        entry_text = " ".join([e.title, *e.bullets])
        chosen, used = [], set()
        for b in by_entry[e.id].bullets if e.id in by_entry else []:
            src = _source(e, b.source)
            if src is None or b.source in used:
                continue
            used.add(b.source)
            text = b.text.strip()
            if problems_here := bullet_problems(text, src, entry_text, translated):
                if translated:
                    fallbacks.append(f"dropped {b.source} ({e.company}): {'; '.join(problems_here)}")
                    continue
                fallbacks.append(f"kept your wording for {b.source} ({e.company}): {'; '.join(problems_here)}")
                text = src
            chosen.append({"text": text, "source": b.source, "original": src, "why": b.why.strip()})
        if not chosen:  # never lose a job: fall back to its first original bullets
            fallbacks.append(f"kept {e.company}'s original bullets (the AI's version didn't pass the checks)")
            chosen = [{"text": t, "source": f"{e.id}.{i}", "original": t, "why": ""}
                      for i, t in enumerate(e.bullets[:2], 1)]
        plan.append(chosen)

    # Built next to the previous version and swapped in at the end, so a failed re-run keeps the old CV.
    out, work = OUT_DIR / str(job["id"]), OUT_DIR / f"{job['id']}.new"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tex_path = work / "cv.tex"
    page_fit_drops: list[str] = []
    while True:
        tex_path.write_text(render(tex, headline, [[b["text"] for b in p] for p in plan], language), encoding="utf-8")
        pages = await compile_pdf(tex_path)
        if pages <= 1 or len(page_fit_drops) >= MAX_PAGE_FIT_DROPS:
            break
        # Too long: drop the least relevant bullet (last) of the job with the most bullets, oldest first.
        idx = max(range(len(plan)), key=lambda i: (len(plan[i]), i))
        if len(plan[idx]) <= 1:
            break
        page_fit_drops.append(plan[idx].pop()["source"])

    changes = {
        "job": {"id": job["id"], "title": job["title"], "company": job["company"]},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "language": language, "pages": pages, "retried": retried,
        "headline": {"before": old_headline, "after": headline, "why": answer.headline_why.strip()},
        "entries": [], "fallbacks": fallbacks, "page_fit_drops": page_fit_drops,
    }
    for e, chosen in zip(entries, plan):
        kept_sources = {b["source"] for b in chosen}
        bullets = []
        for pos, b in enumerate(chosen, 1):
            original_pos = int(b["source"].split(".")[1])
            same = _norm(b["text"]) == _norm(b["original"])
            status = ("translated" if translated else "reworded") if not same else \
                ("kept" if original_pos == pos else "moved")
            bullets.append({**b, "status": status, "from_position": original_pos})
        dropped = [{"source": f"{e.id}.{i}", "original": t} for i, t in enumerate(e.bullets, 1)
                   if f"{e.id}.{i}" not in kept_sources]
        changes["entries"].append({"company": e.company, "title": e.title, "bullets": bullets, "dropped": dropped})
    (work / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(out, ignore_errors=True)
    work.rename(out)
    return changes
