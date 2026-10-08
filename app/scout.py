"""Scout run: a broad, unfiltered search into a separate database, to learn what fake,
spammy or off-target postings look like before turning that into filter rules.

    uv run python -m app.scout [--rounds 6] [--pause 180]

Writes data/scout.db and intel/scout/ (latest.md, jobs.jsonl). Your real data is untouched.
"""

import argparse
import asyncio
import logging

from . import config, db, intel
from .config import ROOT
from .poller import Poller

KEYWORDS = ["software engineer", "software developer", "full stack developer", "ai engineer"]
LOCATIONS = ["United States", "European Union"]

SCOUT_CONFIG = {
    "searches": [{"keywords": k, "location": loc} for loc in LOCATIONS for k in KEYWORDS],
    "remote_only": True,              # keeps "remote" in the queries, like the real scans
    "lookback_seconds": 3 * 86400,    # wider window = more companies to compare
    "max_pages_per_search": 2,
    "detail_checks_per_scan": 25,
    # Vague on purpose: anything engineer/developer-ish, no exclusions, no trust rules.
    "role_keywords": ["engineer", "developer", "programmer", "engineering", "swe", "sde"],
    "exclude_title_words": [],
}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=6, help="detail-reading rounds (25 jobs each)")
    ap.add_argument("--pause", type=int, default=180, help="seconds to wait between rounds")
    ap.add_argument("--details-only", action="store_true", help="skip searching; only read pending detail pages")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config.OVERRIDE.update(SCOUT_CONFIG)
    db.DB_PATH = ROOT / "data" / "scout.db"
    intel.OUT_DIR = ROOT / "intel" / "scout"
    db.init()

    poller = Poller()
    for round_no in range(1, args.rounds + 1):
        searched = args.details_only or poller.last_scan_at is not None
        logging.info("round %d/%d (%s)", round_no, args.rounds, "details only" if searched else "search + details")
        await poller.scan(notify=False, search=not searched)
        pending = db.pending_details()
        logging.info("pending detail pages: %d%s", pending, f" · {poller.last_error}" if poller.last_error else "")
        if pending == 0 and searched:
            break
        if round_no < args.rounds:
            await asyncio.sleep(args.pause * (2 if poller.last_error else 1))

    print(f"\nScout report: {intel.OUT_DIR / 'latest.md'}")


if __name__ == "__main__":
    asyncio.run(main())
