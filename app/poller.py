"""Background scanner. Also runnable as a CLI: `uv run python -m app.poller --once`."""

import argparse
import asyncio
import json
import logging
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx

from . import ai, apify, config, db, intel, mail_alerts, ranking, ratelimits, roles, scraper, sources, trust
from .notify import notify_new

log = logging.getLogger(__name__)
LOOKBACK_MARGIN_S = 600  # overlap between scans so nothing slips through
# Any refusal (429 or 999) pauses LinkedIn entirely (search, details, Scan Now) for 24h, then one
# try. Retrying every few hours (the old doubling backoff) kept a 429 going for 28h+ (Nov 2026).
BLOCK_COOLDOWN = timedelta(hours=24)
# Email alerts count as working (so the search slows to search_minutes) for this long after a read.
ALERTS_FRESH = timedelta(hours=2)
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
        self.last_scan_at: datetime | None = None  # last fully successful search
        self.last_attempt_at: datetime | None = None
        self.next_scan_at: datetime | None = None
        self.last_new = 0
        self.last_error: str | None = None
        self.backoff = 1
        self.paused_until: datetime | None = None  # after HTTP 999: no LinkedIn request until then
        self.blocks = 0  # 999s in a row
        self.alerts_last_run: datetime | None = None
        self.alerts_ok_at: datetime | None = None
        self.alerts_error: str | None = None
        self.full_lookback = False  # set after a reset: next scan searches the whole lookback window
        self.companies_paused_until: datetime | None = None
        self.sources_last_run: dict[str, str] = {}  # source -> ISO time of its last fetch
        self.sources_errors: dict[str, str] = {}
        self.apify_last_run: datetime | None = None  # last attempt (also budget skips)
        self.apify_ok_at: datetime | None = None
        self.apify_error: str | None = None
        self.apify_running = False
        self._lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.backoff = int(state.get("backoff", 1))
            self.blocks = int(state.get("blocks", 0))
            self.sources_last_run = state.get("sources_last_run", {})
            for name in ("next_scan_at", "paused_until", "last_scan_at", "alerts_ok_at", "apify_last_run",
                         "apify_ok_at"):
                if state.get(name):
                    setattr(self, name, datetime.fromisoformat(state[name]))
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        def iso(t: datetime | None) -> str | None:
            return t.isoformat() if t else None
        STATE_PATH.write_text(json.dumps({
            "backoff": self.backoff,
            "blocks": self.blocks,
            "next_scan_at": iso(self.next_scan_at),
            "paused_until": iso(self.paused_until),
            "last_scan_at": iso(self.last_scan_at),  # last search: alert jobs also bump last_seen_at
            "alerts_ok_at": iso(self.alerts_ok_at),
            "sources_last_run": self.sources_last_run,
            "apify_last_run": iso(self.apify_last_run),
            "apify_ok_at": iso(self.apify_ok_at),
        }), encoding="utf-8")

    @property
    def scanning(self) -> bool:
        return self._lock.locked()

    @property
    def paused(self) -> bool:
        return bool(self.paused_until and utcnow() < self.paused_until)

    def _search_due(self, cfg: dict) -> bool:
        """With email alerts working, the search is only a backstop: every search_minutes."""
        alerts_on = (cfg.get("email_alerts") or {}).get("enabled")
        if not alerts_on or not self.alerts_ok_at or utcnow() - self.alerts_ok_at > ALERTS_FRESH:
            return True
        if self.full_lookback or not self.last_scan_at:
            return True
        return utcnow() - self.last_scan_at >= timedelta(minutes=float(cfg.get("search_minutes", 180)))

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
            previous_attempt, self.last_attempt_at = self.last_attempt_at, started
            search = search and self._search_due(cfg)
            remote_only = bool(cfg["remote_only"])
            new_jobs: list[scraper.Job] = []
            stats = {"queries": 0, "cards": 0, "saved": 0, "title_filtered": 0, "checked": 0,
                     "remote": 0, "not_remote": 0, "unknown": 0, "remote_ids": [], "flagged_ids": []}
            if cfg["apify"]["enabled"]:
                # Apify finds LinkedIn jobs (fetch_apify); asking LinkedIn ourselves only gets us blocked.
                self.last_error = None
            elif self.paused:
                self.last_error = (f"LinkedIn refused us; no requests to it until "
                                   f"{self.paused_until.astimezone():%d/%m %H:%M}")
                log.info(self.last_error)
            else:
                scan_log = ratelimits.ScanLog()
                kind = "search"
                try:
                    async for kind in self._linkedin(cfg, search, notify, started, stats, new_jobs, scan_log):
                        pass
                    self.last_error = None
                    self.backoff = 1
                    self.blocks = 0
                except scraper.RateLimited as e:
                    scan_log.blocked(kind, e.status)
                    self.blocks += 1
                    self.paused_until = utcnow() + BLOCK_COOLDOWN
                    self.last_error = (f"LinkedIn refused the scan ({e} on a {kind} request); "
                                       f"no requests to it for 24h, then one try")
                    log.warning(self.last_error)
                except httpx.HTTPError as e:
                    self.last_error = f"Network error: {e!r}"
                    log.warning(self.last_error)
                try:
                    scan_log.write(previous_attempt)
                except OSError:
                    log.exception("failed to write the rate-limit log")
                self._save_state()

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

    async def _linkedin(self, cfg, search, notify, started, stats, new_jobs, scan_log):
        """Every LinkedIn request of a scan. Yields the kind of each request just before making it,
        so a RateLimited raised here is pinned on the kind of request that got refused."""
        lookback = self._lookback(cfg)
        is_dev_role = roles.build(cfg)
        remote_only = bool(cfg["remote_only"])
        first_request = True
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
                    yield "search"
                    scan_log.request("search")
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
            elif cfg["searches"]:
                log.info("search skipped this scan (email alerts are finding jobs); detail pages only")
            stats["saved"] = len(new_jobs)
            if notify and not remote_only and new_jobs:
                await asyncio.to_thread(notify_new, [asdict(j) for j in new_jobs])

            # Read detail pages (applicants, remote check, trust data), a few per scan.
            for job in db.jobs_needing_details(int(cfg["detail_checks_per_scan"])):
                if not first_request:
                    await asyncio.sleep(random.uniform(15, 30))  # detail pages rate-limit sooner
                first_request = False
                yield "detail"
                scan_log.request("detail")
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
                yield "recheck"
                scan_log.request("recheck")
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
                yield "company"
                scan_log.request("company")
                try:
                    db.save_company(url, await scraper.fetch_company(client, url))
                except httpx.TransportError as e:
                    log.warning("company page %s failed: %r", url, e)
                except scraper.RateLimited as e:
                    scan_log.company_refused = e.status
                    self.companies_paused_until = utcnow() + COMPANY_COOLDOWN
                    log.info("company pages refused (%s); pausing them for %s", e, COMPANY_COOLDOWN)
                    break

    def _shown(self, ids) -> list[dict]:
        """The given jobs that made it to To Apply (every filter, incl. region and the AI)."""
        cfg = config.load()
        wanted = set(ids) & {r["id"] for view in ("new", "boards")
                             for r in db.list_jobs(view, remote_only=cfg["remote_only"])}
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

    async def fetch_alerts(self, force: bool = False) -> int:
        """LinkedIn job-alert emails (mail_alerts.py). New jobs join the detail-page queue like
        search results; they reach To Apply (and notify) once their page is read. Returns how many."""
        cfg = config.load()
        opts = cfg.get("email_alerts") or {}
        if not opts.get("enabled"):
            return 0
        due = timedelta(minutes=float(opts.get("every_minutes", 15)))
        if not force and self.alerts_last_run and utcnow() - self.alerts_last_run < due:
            return 0
        self.alerts_last_run = utcnow()
        try:
            jobs, emails = await asyncio.to_thread(
                mail_alerts.fetch, opts.get("mailbox", "INBOX"), int(opts.get("days", 2)))
        except (mail_alerts.AlertsError, OSError) as e:
            self.alerts_error = str(e)
            log.warning("email alerts: %s", e)
            return 0
        self.alerts_error = None
        self.alerts_ok_at = utcnow()
        is_dev_role = roles.build(cfg)
        inserted = await asyncio.to_thread(db.upsert_jobs, [j for j in jobs if is_dev_role(j.title)])
        log.info("email alerts: %d email(s), %d job(s), %d new", emails, len(jobs), len(inserted))
        self._save_state()
        return len(inserted)

    def apify_next_at(self, cfg: dict) -> datetime | None:
        if not self.apify_last_run:
            return None
        return self.apify_last_run + timedelta(minutes=float(cfg["apify"]["every_minutes"]))

    async def fetch_apify(self, force: bool = False) -> int:
        """LinkedIn jobs via the paid Apify actor (apify.py), every apify.every_minutes. Never more than
        monthly_budget_usd a month: a run that could cross it is skipped. Returns jobs that reached To Apply."""
        cfg = config.load()
        o = cfg["apify"]
        if not o["enabled"] or self.apify_running:
            return 0
        next_at = self.apify_next_at(cfg)
        # The very first run (each one costs money) waits for Scan Now; after that, every interval.
        if not force and (not next_at or utcnow() < next_at):
            return 0
        self.apify_running = True  # at once, so the status bar shows it before the first await
        try:
            return await self._apify_run(cfg, o)
        finally:
            self.apify_running = False

    async def _apify_run(self, cfg: dict, o: dict) -> int:
        started = utcnow()
        self.apify_last_run = started
        self._save_state()
        started_iso = started.isoformat(timespec="seconds")
        # The account's own figure also counts runs started on Apify's website.
        spent = max(await asyncio.to_thread(db.apify_month_spent), (await apify.account_usage()).get("used", 0.0))
        if spent + float(o["max_usd_per_run"]) > float(o["monthly_budget_usd"]):
            self.apify_error = (f"Monthly budget reached (US${spent:.2f} of US${float(o['monthly_budget_usd']):.2f})"
                                " — no more Apify runs until the 1st; raise apify.monthly_budget_usd to continue")
            log.warning(self.apify_error)
            await asyncio.to_thread(db.log_apify_run, started_iso, "skipped", error="monthly budget reached")
            return 0
        try:
            result = await apify.run(cfg)
        except (apify.ApifyError, httpx.HTTPError, ValueError, KeyError) as e:
            self.apify_error = f"Apify run failed: {e}"
            log.warning(self.apify_error)
            await asyncio.to_thread(db.log_apify_run, started_iso, "error", error=str(e)[:500])
            return 0
        fn = db.functions(cfg)
        rows = [r for r in apify.to_rows(result.items, started) if fn.is_dev_role(r["title"])]
        new_ids = await asyncio.to_thread(db.upsert_apify, rows)
        await asyncio.to_thread(trust.refresh)
        try:
            await ai.judge_pending(int(cfg["ai"].get("per_scan", 0)))
        except Exception:
            log.exception("AI step failed")
        shown = self._shown(new_ids)
        ok = result.status == "SUCCEEDED"
        self.apify_error = None if ok else f"Apify run ended {result.status} (got {len(result.items)} jobs)"
        if ok:
            self.apify_ok_at = utcnow()
        await asyncio.to_thread(db.log_apify_run, started_iso, result.status, result.run_id, len(result.items),
                                len(new_ids), len(shown), result.usd, "" if ok else self.apify_error)
        log.info("apify: %d job(s), %d new, %d in To Apply, US$%.4f", len(result.items), len(new_ids),
                 len(shown), result.usd)
        await self._notify(shown)
        self._save_state()
        return len(shown)

    async def run_sources_forever(self) -> None:
        """Separate from the LinkedIn loop so its rate-limit backoff doesn't hold the other boards up."""
        while True:
            for step in (self.fetch_apify, self.fetch_alerts, self.fetch_sources):
                try:
                    await step()
                except Exception:
                    log.exception("%s failed", step.__name__)
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
            if self.paused:
                interval = max(interval, (self.paused_until - utcnow()).total_seconds() + 60)
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
    if await poller.fetch_alerts(force=True):
        print("(new jobs from email alerts queued for detail reads)")
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
