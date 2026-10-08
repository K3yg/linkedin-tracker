"""Background scanner. Also runnable as a CLI: `uv run python -m app.poller --once`."""

import argparse
import asyncio
import logging
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx

from . import ai, config, db, intel, ranking, roles, scraper, trust
from .notify import notify_new

log = logging.getLogger(__name__)
LOOKBACK_MARGIN_S = 600  # overlap between scans so nothing slips through
MAX_BACKOFF = 8
# Logged-out company pages answer HTTP 999 most of the time. That used to fail the whole scan and
# double the backoff, so scans ran every ~2h; now company pages just pause for a while.
COMPANY_COOLDOWN = timedelta(hours=6)
NOTIFY_MAX_AGE_HOURS = 12


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Poller:
    def __init__(self) -> None:
        self.last_scan_at: datetime | None = None  # last fully successful scan
        self.last_attempt_at: datetime | None = None
        self.next_scan_at: datetime | None = None
        self.last_new = 0
        self.last_error: str | None = None
        self.backoff = 1
        self.full_lookback = False  # set after a reset: next scan searches the whole lookback window
        self.companies_paused_until: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def scanning(self) -> bool:
        return self._lock.locked()

    def _lookback(self, cfg: dict) -> int:
        since = self.last_scan_at or db.last_seen_at()
        if since is None or self.full_lookback:
            return int(cfg["lookback_seconds"])
        elapsed = (utcnow() - since).total_seconds()
        return int(min(cfg["lookback_seconds"], elapsed + LOOKBACK_MARGIN_S))

    async def scan(self, notify: bool = True, search: bool = True) -> list[scraper.Job]:
        """search=False only reads pending detail pages (used by the scout run's later rounds)."""
        if self._lock.locked():
            return []
        async with self._lock:
            cfg = config.load()
            started = utcnow()
            self.last_attempt_at = started
            lookback = self._lookback(cfg)
            is_dev_role = roles.build(cfg)
            remote_only = bool(cfg["remote_only"])
            new_jobs: list[scraper.Job] = []
            stats = {"queries": 0, "cards": 0, "saved": 0, "title_filtered": 0, "checked": 0,
                     "remote": 0, "not_remote": 0, "unknown": 0, "remote_ids": [], "flagged_ids": []}
            first_request = True
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    for s in cfg["searches"] if search else []:
                        query = s["keywords"]
                        if remote_only and "remot" not in query.lower():  # remote / remoto
                            query += " remote"  # keyword search matches descriptions mentioning remote
                        stats["queries"] += 1
                        for page in range(int(cfg["max_pages_per_search"])):
                            if not first_request:
                                await asyncio.sleep(random.uniform(3, 6))
                            first_request = False
                            raw = await scraper.search(
                                client, query, s.get("location", ""), lookback, page * scraper.PAGE_SIZE,
                                easy_apply=bool(cfg["easy_apply_only"]),
                            )
                            jobs = [j for j in raw if is_dev_role(j.title)]
                            inserted = db.upsert_jobs(jobs)
                            new_jobs += inserted
                            stats["cards"] += len(raw)
                            stats["title_filtered"] += len(raw) - len(jobs)
                            log.info("%r page %d: %d cards, %d new", query, page, len(raw), len(inserted))
                            if len(raw) < scraper.PAGE_SIZE:
                                break
                    if search:
                        self.last_scan_at = started
                        self.full_lookback = False
                    stats["saved"] = len(new_jobs)
                    if notify and not remote_only and new_jobs:
                        await asyncio.to_thread(notify_new, [asdict(j) for j in new_jobs])

                    # Read detail pages (applicants, remote check, trust data), a few per scan.
                    todo = db.jobs_needing_details(int(cfg["detail_checks_per_scan"]))
                    for job in todo:
                        await asyncio.sleep(random.uniform(5, 9))  # detail pages rate-limit sooner
                        try:
                            details = await scraper.fetch_details(client, job["id"], job["title"], job["location"])
                        except httpx.TransportError as e:  # timeout etc.: skip, retried next scan
                            log.warning("detail page %s failed: %r", job["id"], e)
                            continue
                        db.set_details(job["id"], details)
                        stats["checked"] += 1
                        stats[details.workplace] += 1
                        if details.workplace == "remote":
                            stats["remote_ids"].append(job["id"])
                    log.info("checked %d job(s): %d remote", stats["checked"], stats["remote"])

                    # Re-read a few jobs already in To Apply: closed ones drop out, applicant counts refresh.
                    rechecked = closed = 0
                    for job in db.jobs_to_recheck(int(cfg["rechecks_per_scan"])):
                        await asyncio.sleep(random.uniform(5, 9))
                        try:
                            details = await scraper.fetch_details(client, job["id"], job["title"], job["location"])
                        except httpx.TransportError as e:
                            log.warning("re-check %s failed: %r", job["id"], e)
                            continue
                        db.set_details(job["id"], details)
                        rechecked += 1
                        closed += details.applicants_text == "closed"
                    if rechecked:
                        log.info("re-checked %d job(s): %d no longer accepting applications", rechecked, closed)

                    # Company size (startup vs big tech) for jobs that could reach To Apply. Optional:
                    # when LinkedIn refuses, skip it for a while instead of failing the scan.
                    paused = self.companies_paused_until and utcnow() < self.companies_paused_until
                    for url in [] if paused else db.companies_needed(int(cfg["company_checks_per_scan"])):
                        await asyncio.sleep(random.uniform(5, 9))
                        try:
                            db.save_company(url, await scraper.fetch_company(client, url))
                        except httpx.TransportError as e:
                            log.warning("company page %s failed: %r", url, e)
                        except scraper.RateLimited as e:
                            self.companies_paused_until = utcnow() + COMPANY_COOLDOWN
                            log.info("company pages refused (%s); pausing them for %s", e, COMPANY_COOLDOWN)
                            break
                self.last_error = None
                self.backoff = 1
            except scraper.RateLimited as e:
                self.backoff = min(self.backoff * 2, MAX_BACKOFF)
                self.last_error = f"LinkedIn rate-limited the scan ({e}); slowing down {self.backoff}x"
                log.warning(self.last_error)
            except httpx.HTTPError as e:
                self.last_error = f"Network error: {e!r}"
                log.warning(self.last_error)

            stats["error"] = self.last_error
            stats["flagged_ids"] = trust.refresh()
            # Gemini judges the jobs that passed every rule (Gemini, not LinkedIn: runs even after a 999).
            try:
                stats["ai_judged"] = await ai.judge_pending(int(cfg["ai"].get("per_scan", 0)))
            except Exception:
                log.exception("AI step failed")
            try:
                intel.write(stats)
            except Exception:
                log.exception("failed to write intel report")

            # Notify only for jobs that made it to To Apply (every filter, incl. region and the AI).
            shown = {r["id"] for r in db.list_jobs("new", remote_only=remote_only)}
            trusted_remote = [dict(r) for r in db.all_jobs() if r["id"] in set(stats["remote_ids"]) & shown]
            self.last_new = len(trusted_remote) if remote_only else len(new_jobs)
            if notify and remote_only:
                # Skip old backlog jobs that only just got verified.
                fresh = [j for j in trusted_remote if ranking.age_hours(j) <= NOTIFY_MAX_AGE_HOURS]
                if fresh:
                    await asyncio.to_thread(notify_new, fresh)
            return new_jobs

    async def run_forever(self) -> None:
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("scan failed")
            interval = float(config.load()["poll_minutes"]) * 60 * self.backoff
            self.next_scan_at = utcnow() + timedelta(seconds=interval)
            await asyncio.sleep(interval)


async def _cli() -> None:
    ap = argparse.ArgumentParser(description="Scan LinkedIn for new jobs")
    ap.add_argument("--once", action="store_true", help="run one scan and exit (default)")
    ap.add_argument("--notify", action="store_true", help="show a Windows toast for new jobs")
    ap.add_argument("--list", action="store_true", help="also list every job still to apply")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    poller = Poller()
    new = await poller.scan(notify=args.notify)
    if poller.last_error:
        print(f"\n! {poller.last_error}")

    print(f"\n{len(new)} new job(s) found, {poller.last_new} verified this scan")
    for j in new:
        print(f"  {j.title} — {j.company} ({j.location})\n    {j.url}")

    if args.list:
        pending = db.list_jobs("new", remote_only=bool(config.load()["remote_only"]))
        print(f"\n{len(pending)} job(s) to apply:")
        for r in pending:
            print(f"  {r['title']} — {r['company']} ({r['location']})\n    {r['url']}")


if __name__ == "__main__":
    asyncio.run(_cli())
