import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"  # secrets (GEMINI_API_KEY); never in config.json

DEFAULTS = {
    "poll_minutes": 10,
    "lookback_seconds": 86400,
    "max_pages_per_search": 2,
    "detail_checks_per_scan": 20,
    "company_checks_per_scan": 8,
    "remote_only": True,
    # Only Easy Apply jobs (apply on LinkedIn, no account on another site). Also sent to the search.
    "easy_apply_only": True,
    "rechecks_per_scan": 5,  # re-read jobs shown in To Apply so closed ones drop out
    # Gemini reads each job that passes the filters and says whether it's worth applying (ai.py).
    "ai": {"enabled": True, "model": "gemini-2.5-flash", "tailor_model": "gemini-3.8-flash",
           "per_scan": 15, "hide_skips": True},
    # Where the user can work from; "remote within the US" etc. is hidden (see region.py).
    "remote_region": {"allowed": ["brazil", "latam", "latin america", "south america", "americas"],
                      "hide_unstated": False},
    "profile": {"min_fit": 0, "max_years_required": 7, "skills": {}, "avoid": {}},
    "searches": [],
    "role_keywords": [],
    "exclude_title_words": [],
    "top_picks_exclude_title_words": [],
    "trust": {
        "soft_flags_to_hide": 2,
        "max_same_title_per_company": 3,
        "max_jobs_per_company_week": 10,
        "min_description_chars": 500,
        "blocked_companies": [],
        "trusted_companies": [],
        "hide_phrases": [],
        "warn_phrases": [],
    },
}

# Temporary in-process overrides (the scout run uses this); never written to config.json.
OVERRIDE: dict = {}


def load() -> dict:
    """Read config.json on every call so edits apply without a restart."""
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if k != "trust"})
        cfg["trust"].update(user.get("trust", {}))
    cfg.update(OVERRIDE)
    return cfg


def load_env() -> None:
    """KEY=value lines from .env into the environment (existing variables win)."""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
