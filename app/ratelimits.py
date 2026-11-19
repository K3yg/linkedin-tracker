"""Log of every LinkedIn scan's request counts and the block that ended it, if any.

One JSON line per scan in data/ratelimit_log.jsonl. Summary: `uv run python -m app.ratelimits`.
Shows which kind of request gets refused (search / detail / recheck / company), with which status
(429 = too many requests, 999 = bot detection), how many requests in, and at what hour.
"""

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .config import ROOT

LOG_PATH = ROOT / "data" / "ratelimit_log.jsonl"
KINDS = ("search", "detail", "recheck", "company")


class ScanLog:
    """Counts one scan's LinkedIn requests; `blocked()` records the request that got refused."""

    def __init__(self) -> None:
        self.started = datetime.now(timezone.utc)
        self.requests = dict.fromkeys(KINDS, 0)
        self.block: dict | None = None
        self.company_refused: int | None = None  # company pages refuse often; they don't end the scan

    def request(self, kind: str) -> None:
        self.requests[kind] += 1

    def _event(self, kind: str, status: int) -> dict:
        return {"kind": kind, "status": status,
                "after_requests": sum(self.requests.values()) - 1,  # successful ones before this
                "seconds_in": round((datetime.now(timezone.utc) - self.started).total_seconds())}

    def blocked(self, kind: str, status: int) -> None:
        self.block = self._event(kind, status)

    def write(self, previous_scan: datetime | None) -> None:
        if not any(self.requests.values()):
            return
        record = {
            "at": self.started.isoformat(timespec="seconds"),
            "minutes_since_previous": round((self.started - previous_scan).total_seconds() / 60)
            if previous_scan else None,
            "requests": self.requests,
            "blocked": self.block,
            "company_refused": self.company_refused,
        }
        LOG_PATH.parent.mkdir(exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def load() -> list[dict]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def summary(records: list[dict]) -> str:
    if not records:
        return f"No scans logged yet ({LOG_PATH})."
    blocked = [r for r in records if r["blocked"]]
    out = [f"{len(records)} scans since {records[0]['at']}, {len(blocked)} ended by a block "
           f"({len(blocked) / len(records):.0%})", ""]

    by = Counter((r["blocked"]["kind"], r["blocked"]["status"]) for r in blocked)
    out.append("Blocks by request kind / status:")
    out += [f"  {kind:8} HTTP {status}: {n}" for (kind, status), n in by.most_common()] or ["  none"]

    after = defaultdict(list)
    for r in blocked:
        after[r["blocked"]["kind"]].append(r["blocked"]["after_requests"])
    if after:
        out += ["", "Successful requests in the scan before the block (min / median / max):"]
        for kind, ns in after.items():
            ns.sort()
            out.append(f"  {kind:8} {ns[0]} / {ns[len(ns) // 2]} / {ns[-1]}")

    totals = Counter()
    for r in records:
        totals.update(r["requests"])
    out += ["", "Requests made: " + ", ".join(f"{k} {totals[k]}" for k in KINDS)]

    hours = defaultdict(lambda: [0, 0])  # local hour -> [scans, blocked]
    for r in records:
        h = datetime.fromisoformat(r["at"]).astimezone().hour
        hours[h][0] += 1
        hours[h][1] += bool(r["blocked"])
    out += ["", "By local hour (scans / blocked):"]
    out += [f"  {h:02d}h  {s:3} / {b}" for h, (s, b) in sorted(hours.items())]

    gaps = defaultdict(lambda: [0, 0])  # time since previous scan -> [scans, blocked]
    for r in records:
        m = r.get("minutes_since_previous")
        if m is None:
            continue
        bucket = "<30 min" if m < 30 else "30-60 min" if m < 60 else "1-3 h" if m < 180 else "3 h+"
        gaps[bucket][0] += 1
        gaps[bucket][1] += bool(r["blocked"])
    if gaps:
        out += ["", "By time since the previous scan (scans / blocked):"]
        out += [f"  {k:9}  {s:3} / {b}" for k in ("<30 min", "30-60 min", "1-3 h", "3 h+")
                if k in gaps for s, b in [gaps[k]]]
    return "\n".join(out)


if __name__ == "__main__":
    print(summary(load()))
