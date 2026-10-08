"""Windows toast notifications (no-op on other platforms)."""

import logging

try:
    from winotify import Notification, audio
except ImportError:  # not on Windows
    Notification = None

log = logging.getLogger(__name__)
APP_ID = "LinkedIn Tracker"
DASHBOARD_URL = "http://localhost:8765"


def notify_new(jobs: list[dict]) -> None:
    """jobs: dicts with title, company, location, url."""
    if not jobs or Notification is None:
        return
    try:
        if len(jobs) == 1:
            j = jobs[0]
            toast = Notification(
                app_id=APP_ID,
                title=j["title"],
                msg=" · ".join(p for p in (j["company"], j["location"]) if p),
                launch=j["url"],
            )
            toast.add_actions(label="Open job", launch=j["url"])
        else:
            lines = [f"{j['title']} — {j['company']}" for j in jobs[:3]]
            if len(jobs) > 3:
                lines.append(f"+{len(jobs) - 3} more")
            toast = Notification(
                app_id=APP_ID,
                title=f"{len(jobs)} new remote jobs",
                msg="\n".join(lines),
                launch=DASHBOARD_URL,
            )
        toast.add_actions(label="Dashboard", launch=DASHBOARD_URL)
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        log.exception("failed to show notification")
