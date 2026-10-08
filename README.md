# LinkedIn Job Tracker

Watches LinkedIn for newly posted **100% remote** jobs, pops a Windows notification when new ones
are verified, and tracks them in a local dashboard (To Apply → Applied / Dismissed).
**Top Picks** shows the 5 best matches: fewest applicants, freshly posted, not staff-level.

Uses LinkedIn's public logged-out endpoints — no account or credentials involved.

## Run

```
run.bat                      # server + dashboard at http://localhost:8765, scans every 15 min
uv run python -m app.poller  # one scan from the terminal, prints new jobs (--list, --notify)
```

Start automatically at logon (optional):

```
schtasks /create /tn "LinkedIn Job Tracker" /sc onlogon /tr "\"%CD%\run.bat\""
```

## How a scan works

1. Search each query (with "remote" appended) for jobs posted since the last scan.
   Titles must pass the software-dev filter (`role_keywords` / `exclude_title_words`).
2. For up to `detail_checks_per_scan` jobs, read the detail page: applicants, description,
   seniority, employment type, industry, company page, Easy Apply vs external, salary.
   Best bets go first: fresh, not senior, one copy per company + title, not from a blocked company.
   Company pages (size → "Startup") are optional: when LinkedIn refuses them (HTTP 999) they pause
   for 6h instead of failing the scan.
3. Classify remote / not remote from the description (`classify_workplace` in `app/scraper.py`).
   Then where it can be done from (`app/region.py`): "remote within the US", "must reside in Canada",
   "authorized to work in the United States" are hidden; Brazil / LATAM / worldwide get a **Brazil OK** tag
   and rank first; postings that don't say get a **Region?** tag and rank lower.
4. Score trust (`app/trust.py`): blocked companies, spam phrases, agencies, repeat postings…
   Suspicious jobs move to the **Flagged** tab with the reasons.
5. Drop jobs that are **no longer accepting applications** (LinkedIn's banner or the description
   saying so). A few To Apply jobs are re-read each scan (`rechecks_per_scan`) so closed ones drop out.
6. **AI check** (`app/ai.py`, Gemini): each job that passed every rule is read next to your CV
   (`cv/base.tex`) and judged apply / maybe / skip with a 0–100 score and reasons (hover the
   **AI 85** tag). Skips move to Flagged; the score feeds the ranking. Backfill now with
   `uv run python -m app.ai 50`.
7. Write `intel/latest.md`, `intel/scans/<time>.md` and `intel/jobs.jsonl`.

## Tailored CVs

Click **Tailor CV** on a job (To Apply or Applied). In ~20s it shows **CV PDF ↗**, the `.tex`, and
**What changed** (headline before/after, each bullet's new position and wording with the original
underneath, what was dropped and why). Upload the PDF in Easy Apply, then click Applied — the CV stays
on the job.

How it works (`app/tailor.py`): Gemini (`ai.tailor_model`) reorders and rewords the bullets of
`cv/base.tex` for the posting; the `.tex` is rebuilt from your file, so header, contact details,
companies, titles and dates never pass through the AI. Each bullet is checked: it must come from one of
your bullets in the same job, with no new numbers or technologies, no inflated role ("contributed to" →
"led"), and not much longer. Problems get one retry, then your original wording is kept. Portuguese
postings get a Portuguese CV. Compiled by `tools/tectonic.exe` (downloaded from its GitHub releases)
and kept to one page. Files: `cv/out/<job id>/`. To update your CV, replace `cv/base.tex`.

## Intelligence files (`intel/`)

- `latest.md` — what the last scan found + an overview of the whole dataset + your labels.
  Paste it into an AI (instructions at the bottom of the file) to learn what a good job looks
  like for you and get better keywords / filter rules.
- `jobs.jsonl` — one JSON object per job: structured fields, remote evidence, trust flags,
  a description excerpt and `my_label` (applied / dismissed / unreviewed).

## Scout run

```
uv run python -m app.scout            # broad, unfiltered search -> data/scout.db, intel/scout/
```

Use it to study what spam looks like before adding rules; your real data is untouched.

## Configure — `config.json`

- `searches`: keywords + optional location (also editable from the dashboard)
- `remote_only`: show/notify only jobs verified as remote
- `easy_apply_only`: only Easy Apply jobs (searches send LinkedIn's `f_AL=true`, the list hides the rest)
- `ai`: `enabled`, `model` (default `gemini-2.5-flash`), `per_scan`, `hide_skips`. Needs `GEMINI_API_KEY`
  in `.env` (not committed; copy `.env.example`), plus `PROFILE_ABOUT` (one-line summary of you for the AI)
  and `CV_NAME` (tailored PDF file name). Your CV goes in `cv/base.tex`, also not committed.
- `remote_region`: `allowed` regions you can work from (Brazil, LATAM…); `hide_unstated: true` also hides
  remote jobs that don't say where they hire
- `poll_minutes`, `max_pages_per_search` (10 jobs/page), `detail_checks_per_scan`, `lookback_seconds`
- `role_keywords` / `exclude_title_words`: the software-dev title filter
- `profile`: your skills (`skills` +points, `avoid` −points) and `min_fit`; jobs below it are hidden.
  Ranking ("Best match") = fit + open to Brazil + startup (company size from LinkedIn) + mid-level
  ("Engineer II", "Mid", "Pleno", asks 2–5 years) − senior (less if it only asks ≤ 5 years)
  + Easy Apply + few applicants + fresh. The same role posted in several cities shows once ("+N more").
- `top_picks_exclude_title_words`: keep these titles out of Top Picks (e.g. staff, principal)
- `trust`: `blocked_companies`, `trusted_companies`, `hide_phrases` (hide on sight),
  `warn_phrases` (soft), thresholds. The dashboard's Block / Trust company buttons edit these.

## Limits of the logged-out endpoints

- LinkedIn ignores its own remote filter when logged out, so remote-ness is inferred from the
  job description. Postings that only mark "Remote" in LinkedIn's UI badge without saying it in
  the text are classified "unknown" and hidden.
- Too many requests → HTTP 429. The poller backs off automatically (up to 8x the interval).

Data lives in `data/jobs.db` (SQLite).
