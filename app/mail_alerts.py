"""LinkedIn job-alert emails as a job source: no request to LinkedIn to find jobs.

Create job alerts on LinkedIn (logged in, so its Remote filter works) and this reads the alert
emails from your mailbox over IMAP, read-only (nothing is marked read, moved or deleted). Each job
in an alert becomes a LinkedIn job like the ones the search finds, and its detail page is read
later like theirs.

Setup in .env: IMAP_USER, IMAP_PASSWORD (Gmail: an app password), optional IMAP_HOST
(default imap.gmail.com). In config.json: "email_alerts": {"enabled": true, ...}.

Check what it reads without saving anything: `uv run python -m app.mail_alerts`.
"""

import email
import imaplib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime

from selectolax.parser import HTMLParser

from .scraper import Job

log = logging.getLogger(__name__)
SENDER = "jobalerts-noreply@linkedin.com"  # jobs-noreply@ is application status, not alerts
MAX_MESSAGES = 50
JOB_LINK = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\s\"']*?-)?(\d{6,})", re.I)
VIEW_JOB = re.compile(r"^view job:\s*(\S+)", re.I)
SEPARATOR = re.compile(r"^[-_=]{5,}$")
# Lines in a job block that aren't the title, company or location.
NOISE = re.compile(
    r"^(easy apply|promoted|actively recruiting|actively hiring|be an early applicant|new|"
    r"apply with (?:resume|your profile).*|\d+\s+(?:school )?(?:connections?|alumni|applicants?).*|"
    r"\d+\s+(?:minutes?|hours?|days?|weeks?) ago|fast[- ]growing.*|top applicant|"
    r"[$€£R]\$?\s?\d.*|.*\b(?:/yr|/hr|/mo|per year|per hour|per month)\b.*|\d+ benefits?|"
    r"(?:\d+ )?company alumni.*|responses managed off linkedin|in your network)$",
    re.I,
)
# The alert's own header / footer text, which can share a block with the first or last job.
HEADER = re.compile(r"job alert|jobs? match(?:es)? your|new jobs?\b|see all jobs|unsubscribe|linkedin|"
                    r"your preferences|manage (?:your )?alerts|\bjobs? (?:for|in)\b.*\balert", re.I)
EASY_APPLY = re.compile(r"^easy apply$", re.I | re.M)


class AlertsError(Exception):
    pass


def credentials() -> tuple[str, str, str]:
    user, password = os.environ.get("IMAP_USER", ""), os.environ.get("IMAP_PASSWORD", "")
    if not user or not password:
        raise AlertsError("IMAP_USER / IMAP_PASSWORD missing in .env")
    return os.environ.get("IMAP_HOST", "imap.gmail.com"), user, password


def _job(job_id: str, lines: list[str], subject: str, sent_at: str | None, easy_apply: bool) -> Job | None:
    fields: list[str] = []
    for line in lines:
        if NOISE.match(line) or HEADER.search(line):
            continue
        # "Company · Location" on one line
        fields += [p.strip() for p in re.split(r"\s+[·•|]\s+", line) if p.strip()]
    if not fields:
        return None
    return Job(
        id=job_id,
        title=fields[0],
        company=fields[1] if len(fields) > 1 else "",
        location=fields[2] if len(fields) > 2 else "",
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        posted_at=sent_at,  # the alert's send time: the job was posted at or before it
        search_keywords=f"email alert: {subject}"[:200],
        easy_apply=easy_apply,
    )


def parse_text(text: str, subject: str = "", sent_at: str | None = None) -> list[Job]:
    """Plain-text part: blocks of title / company / location / extras, then 'View job: <url>'."""
    jobs, block = [], []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if SEPARATOR.match(line):
            block = []
            continue
        m = VIEW_JOB.match(line)
        if not m:
            block.append(line)
            continue
        link = JOB_LINK.search(m.group(1))
        if link:
            lines = block[-6:]  # header text can sit above the first job in the same block
            if job := _job(link.group(1), lines, subject, sent_at, any(EASY_APPLY.match(x) for x in lines)):
                jobs.append(job)
        block = []
    return jobs


def parse_html(html: str, subject: str = "", sent_at: str | None = None) -> list[Job]:
    """HTML part (fallback): for each job link, the smallest enclosing element that holds only that
    job; its text lines from the title on are title / company / location."""
    tree = HTMLParser(html)
    anchors: dict[str, list] = {}
    for a in tree.css("a[href]"):
        if m := JOB_LINK.search(a.attributes.get("href") or ""):
            anchors.setdefault(m.group(1), []).append(a)
    jobs = []
    for job_id, links in anchors.items():
        titled = [a for a in links if " ".join(a.text().split()) and not NOISE.match(" ".join(a.text().split()))]
        if not titled:
            continue
        title_el = titled[0]
        title = " ".join(title_el.text().split())
        box, lines = title_el, [title]
        while box.parent is not None and box.parent.tag not in ("body", "html"):
            parent = box.parent
            inner = {m.group(1) for a in parent.css("a[href]")
                     if (m := JOB_LINK.search(a.attributes.get("href") or ""))}
            if len(inner) > 1:
                break
            box = parent
            text = [" ".join(x.split()) for x in box.text(separator="\n").splitlines()]
            text = [x for x in text if x]
            if title in text:
                lines = text[text.index(title):]
            if len([x for x in lines if not NOISE.match(x)]) >= 3:
                break
        if job := _job(job_id, lines[:8], subject, sent_at, any(EASY_APPLY.match(x) for x in lines[:8])):
            jobs.append(job)
    return jobs


def parse_message(msg: Message) -> list[Job]:
    subject = " ".join(str(msg.get("Subject", "")).split())
    try:
        sent_at = parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        sent_at = None
    jobs: list[Job] = []
    if (part := msg.get_body(preferencelist=("plain",))) is not None:
        jobs = parse_text(part.get_content(), subject, sent_at)
    if not jobs and (part := msg.get_body(preferencelist=("html",))) is not None:
        jobs = parse_html(part.get_content(), subject, sent_at)
    return jobs


def fetch(mailbox: str = "INBOX", days: int = 2) -> tuple[list[Job], int]:
    """Jobs from the alert emails of the last `days` days (blocking; run it in a thread).
    Returns (jobs, emails read). The same job in several alerts comes back once."""
    host, user, password = credentials()
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        with imaplib.IMAP4_SSL(host, timeout=30) as imap:
            imap.login(user, password)
            status, _ = imap.select(f'"{mailbox}"', readonly=True)
            if status != "OK":
                raise AlertsError(f"mailbox {mailbox!r} not found")
            status, data = imap.search(None, "FROM", f'"{SENDER}"', "SINCE", since)
            ids = data[0].split()[-MAX_MESSAGES:] if status == "OK" and data and data[0] else []
            jobs: dict[str, Job] = {}
            for num in reversed(ids):  # newest first, so a job keeps its latest alert
                status, parts = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                for item in parts:
                    if isinstance(item, tuple):
                        msg = email.message_from_bytes(item[1], policy=default_policy)
                        for job in parse_message(msg):
                            jobs.setdefault(job.id, job)
            return list(jobs.values()), len(ids)
    except imaplib.IMAP4.error as e:
        raise AlertsError(f"IMAP: {e}") from e


if __name__ == "__main__":
    from . import config

    opts = config.load()["email_alerts"]
    found, read = fetch(opts.get("mailbox", "INBOX"), int(opts.get("days", 2)))
    print(f"{read} alert email(s), {len(found)} job(s):")
    for j in found:
        print(f"  {j.id}  {j.title} — {j.company} ({j.location}){'  [Easy Apply]' if j.easy_apply else ''}")
