"""Background scanner. Also runnable as a CLI: `uv run python -m app.poller --once`."""

import argparse
import asyncio
import json
import logging
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx

from . import ai, config, db, intel, ranking, roles, scraper, sources, trust
from .notify import notify_new

log = logging.getLogger(__name__)
LOOKBACK_MARGIN_S = 600  # overlap between scans so nothing slips through
MAX_BACKOFF = 8
# Logged-out company pages answer HTTP 999 most of the time. That used to fail the whole scan and
# double the backoff, so scans ran every ~2h; now company pages just pause for a while.
COMPANY_COOLDOWN = timedelta(hours=6)
NOTIFY_MAX_AGE_HOURS = 12
# Survives restarts: restarting the server used to scan at once and reset the backoff, which hit
# LinkedIn again while it was still refusing us.
STATE_PATH = config.ROOT / "data" / "poller_state.json"


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
        self.sources_last_run: dict[str, str] = {}  # source -> ISO time of its last fetch
        self.sources_errors: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.backoff = int(state.get("backoff", 1))
            self.sources_last_run = state.get("sources_last_run", {})
            if state.get("next_scan_at"):
                self.next_scan_at = datetime.fromisoformat(state["next_scan_at"])
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        STATE_PATH.write_text(json.dumps({
            "backoff": self.backoff,
            "next_scan_at": self.next_scan_at.isoformat() if self.next_scan_at else None,
            "sources_last_run": self.sources_last_run,
        }), encoding="utf-8")

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
                                await asyncio.sleep(random.uniform(8, 15))
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
                        await asyncio.sleep(random.uniform(10, 20))  # detail pages rate-limit sooner
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
                        await asyncio.sleep(random.uniform(10, 20))
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
                        await asyncio.sleep(random.uniform(10, 20))
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
            stats["pruned"] = db.prune_stale()
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

            shown = self._shown(stats["remote_ids"]) if remote_only else []
            self.last_new = len(shown) if remote_only else len(new_jobs)
            if notify and remote_only:
                await self._notify(shown)
            return new_jobs

    def _shown(self, ids) -> list[dict]:
        """The given jobs that made it to To Apply (every filter, incl. region and the AI)."""
        cfg = config.load()
        wanted = set(ids) & {r["id"] for r in db.list_jobs("new", remote_only=cfg["remote_only"])}
        return [dict(r) for r in db.all_jobs() if r["id"] in wanted]

    async def _notify(self, jobs: list[dict]) -> None:
        """Fresh jobs only (not old backlog that just got verified); with notify_only_open, only the
        ones open to Brazil / worldwide — the rest still show on the dashboard."""
        cfg = config.load()
        fn = db.functions(cfg)
        jobs = [j for j in jobs if ranking.age_hours(j) <= NOTIFY_MAX_AGE_HOURS]
        if cfg.get("notify_only_open", True):
            jobs = [j for j in jobs if fn.remote_scope(j["title"], j["location"], j["description"]) == "open"]
        if jobs:
            await asyncio.to_thread(notify_new, jobs)

    async def fetch_sources(self, force: bool = False) -> int:
        """Other job boards (sources.py), each on its own interval. Returns how many new jobs reached To Apply."""
        cfg = config.load()
        names = list(sources.FETCHERS) if force else sources.due(cfg, self.sources_last_run)
        names = [n for n in names if (cfg.get("sources") or {}).get(n)]
        if not names:
            return 0
        fn = db.functions(cfg)
        jobs, errors = await sources.fetch(cfg, names)
        now = utcnow().isoformat(timespec="seconds")
        for name in names:
            self.sources_last_run[name] = now
        self.sources_errors = errors
        new_ids = await asyncio.to_thread(db.upsert_external, [j for j in jobs if fn.is_dev_role(j["title"])])
        await asyncio.to_thread(trust.refresh)
        shown = self._shown(new_ids)
        log.info("sources %s: %d new job(s), %d in To Apply", ", ".join(names), len(new_ids), len(shown))
        await self._notify(shown)
        self._save_state()
        return len(shown)

    async def run_sources_forever(self) -> None:
        """Separate from the LinkedIn loop so its rate-limit backoff doesn't hold the other boards up."""
        while True:
            try:
                await self.fetch_sources()
            except Exception:
                log.exception("sources failed")
            await asyncio.sleep(600)

    async def run_forever(self) -> None:
        # After a restart, wait out a scan that was already scheduled (e.g. a rate-limit backoff).
        if self.next_scan_at and self.next_scan_at > utcnow():
            await asyncio.sleep((self.next_scan_at - utcnow()).total_seconds())
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("scan failed")
            interval = float(config.load()["poll_minutes"]) * 60 * self.backoff
            self.next_scan_at = utcnow() + timedelta(seconds=interval)
            self._save_state()
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
