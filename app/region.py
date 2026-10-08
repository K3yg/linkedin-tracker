"""Where a remote job can be done from.

The user lives in Brazil, so "remote within the US" is as useless as on-site.
scope(title, location, description) -> ("open" | "restricted" | "unstated", evidence)

- open:       remote from a region in config remote_region.allowed (Brazil, LATAM, Americas…),
              or from anywhere / worldwide.
- restricted: remote only from somewhere else — "remote within the US", "must reside in Canada",
              "authorized to work in the United States", "Remote (Bulgaria)", "hiring in these states".
- unstated:   no hint either way.

A named allowed region ("Remote (US, Canada, Brazil)") beats a restriction; a vague
"work from anywhere" does not ("work from anywhere … must reside in an eligible U.S. state").
"""

import re

# Regions the user can't work from. Countries/states only matter inside the patterns below,
# so "Bank of America" or "trusted by enterprises in Europe" never count.
US_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
    "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|"
    "tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming"
)
STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
ELSEWHERE = (
    r"(?-i:U\.?S\.?(?:A\.?)?)|united states(?: of america)?|america|north america|lower 48|"
    r"(?:the )?continental (?:u\.?s\.?a?\.?|united states)|contiguous (?:u\.?s\.?|united states)|"
    r"canada|mexico|(?-i:UK|U\.K\.|EU|E\.U\.|EEA|EMEA|DACH|CEE|APAC)|united kingdom|great britain|britain|"
    r"england|scotland|ireland|europe|european union|european|nordics|scandinavia|benelux|"
    r"dach region|asia|india|pakistan|philippines|australia|new zealand|japan|singapore|israel|turkey|"
    r"south africa|africa|middle east|germany|deutschland|france|spain|españa|italy|italia|portugal|poland|"
    r"netherlands|belgium|sweden|norway|denmark|finland|switzerland|austria|czech republic|czechia|romania|"
    r"bulgaria|greece|hungary|croatia|serbia|ukraine|estonia|latvia|lithuania|slovakia|slovenia|luxembourg|"
    r"cyprus|malta|argentina|colombia|chile|peru|uruguay|costa rica|ecuador|guatemala|"
    + US_STATES
)
# "Remote (Global)", "work from anywhere": open, but weaker than naming the user's region.
GENERIC = ("anywhere in the world", "anywhere", "worldwide", "world-wide", "global", "globally", "international")

SEP = r"\s*(?:,|/|\||&|\+|\bor\b|\band\b)\s*(?:the\s+)?"
FILLER = (r"(?:(?:the|an?|one of the|any of the|our|any|eligible|approved|following|select(?:ed)?|certain|"
          r"these|listed|permitted)\s+){0,3}")
# Region words that describe something else: "US time zones", "European clients".
NOT_PLACE = (r"(?![\s-]*(?:time\s?zones?|timezones?|hours|business hours|working hours|clients?|customers?|"
             r"markets?|compan(?:y|ies)|headquarters|hq|teams?|workforce|brands?|network|community|"
             r"presence|footprint|reach|impact|economy|scale|leader)\b)")


def _region_re(allowed: list[str]) -> str:
    words = sorted({*(re.escape(a.lower()) for a in allowed if a.strip()), *map(re.escape, GENERIC)},
                   key=len, reverse=True)
    one = rf"(?<!\w)(?:{'|'.join(words)}|{ELSEWHERE})(?!\w)"
    return rf"(?P<regions>{one}(?:{SEP}{one})*){NOT_PLACE}"


def shown(cfg: dict) -> tuple[str, ...]:
    """Scopes that reach To Apply (and notifications)."""
    return ("open",) if (cfg.get("remote_region") or {}).get("hide_unstated") else ("open", "unstated")


def build(cfg: dict):
    """-> scope(title, location, description) -> 'open' | 'restricted' | 'unstated' (plus evidence)."""
    rc = cfg.get("remote_region") or {}
    allowed = [a.lower() for a in rc.get("allowed", [])]
    regions = _region_re(allowed)
    allowed_re = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, allowed)) + r")(?!\w)", re.I) if allowed else None
    generic_re = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, GENERIC)) + r")(?!\w)", re.I)
    region_only = re.compile(rf"\s*(?:{regions})\s*", re.I)

    contexts = [re.compile(p, re.I) for p in (
        # "fully remote within the US", "remote position anywhere in the US", "remotely from Brazil"
        rf"\bremot(?:e|ely|o)\b[^.;:!?\n]{{0,30}}?\b(?:in|within|from|across|throughout|inside)\s+"
        rf"(?:anywhere\s+in\s+)?{FILLER}{regions}",
        # "Remote (US, Canada, Brazil)", "Location: Remote, USA", "Remote - Continental United States"
        rf"\bremot(?:e|ely|o)\b\s*(?:[-–—:,|/(\[]\s*)+(?:only\s+|eligible\s+|based\s+)?"
        rf"(?:in\s+|from\s+|within\s+)?{FILLER}{regions}",
        # "United States — Remote", "US (Remote)", "LATAM remote"
        rf"{regions}\s*(?:[-–—:,|/(\[]\s*)*(?:fully\s+|100%\s+)?remot(?:e|o)\b",
        # "must reside within the United States", "candidates based in the US", "hiring in Brazil"
        rf"\b(?:resid\w*|live|lives|living|domiciled|hir(?:e|es|ing)|employ(?:s|ing)?|"
        rf"(?:be|are|currently|you're|you are|candidates?|applicants?|engineers?|developers?|talent|"
        rf"individuals|those)\s+(?:based|located)|(?-i:Based|Located))\b"
        rf"[^.;:!?\n]{{0,25}}?\b(?:in|within|inside|from)\s+(?:anywhere\s+in\s+)?{FILLER}{regions}",
        # "authorized to work for any employer in the U.S.", "right to work in the UK"
        rf"\b(?:authori[sz](?:ed|ation)|eligib(?:le|ility)|right|permitted|legally able|able|permission)"
        rf"\s+to\s+work\b[^.;:!?\n]{{0,30}}?\b(?:in|within)\s+{FILLER}{regions}",
        # "US citizenship", "U.S. residency requirement", "EU work permit"
        rf"{regions}\s+(?:work\s+authori[sz]ation|work\s+permit|right\s+to\s+work|citizens?(?:hip)?|"
        rf"nationals?|residency|persons|green\s+card|permanent\s+residen\w*)\b",
        # "work from anywhere in the US"
        rf"\banywhere\s+(?:in|within|across|throughout)\s+{FILLER}{regions}",
        # "Job location: United States", "**Location:** Brazil"
        rf"\blocation\b[\s:*\-–—]{{0,8}}{regions}",
        # "Must be U.S.-based", "this role is US-based only"
        rf"\b(?:be|is|are|you're|you are|candidates?|applicants?|engineers?|developers?)\s+{regions}[- ]based\b",
    )]
    us_states_list = re.compile(
        r"\b(?:following|eligible|approved|select(?:ed)?|these|listed|certain|permitted)\s+(?:U\.?S\.?\s+)?states\b"
        r"|\bgreen card\b|\bGC holders?\b",
        re.I,
    )
    generic_open = re.compile(
        r"\b(?:work|working|remote(?:ly)?|hire|hiring|based|located|live|anyone|candidates?|applicants?|talent|"
        r"executed|done|employees?|engineers?|join)\b[^.;!?\n]{0,40}?"
        r"\b(?:from anywhere(?! (?:in|within|across)\b)(?!\s+for\s+(?:up\s+to\s+)?\d)|anywhere in the world|"
        r"globally|worldwide|world-wide|from any country|in any country|any location|"
        r"regardless of (?:your )?location|location[- ](?:independent|agnostic))\b",
        re.I,
    )
    title_part = re.compile(r"\(([^()]*)\)|[-–—|:]\s*([^-–—|:()]+)$")
    title_noise = re.compile(r"\b(?:remot[eo]|only|solo|somente|apenas|eligible|from|in|within|based|fully|"
                             r"100%|position|role|opportunity)\b|[,;]", re.I)

    def kind(text: str) -> str:
        """Captured region list -> 'explicit' (names an allowed region) | 'generic' | 'elsewhere'."""
        if allowed_re and allowed_re.search(text):
            return "explicit"
        if generic_re.search(text):
            return "generic"
        return "elsewhere"

    def scope(title: str | None, location: str | None, description: str | None) -> tuple[str, str]:
        title, location, desc = title or "", location or "", description or ""
        hits: dict[str, str] = {}

        def hit(k: str, evidence: str) -> None:
            hits.setdefault(k, evidence)

        if allowed_re and (m := allowed_re.search(location)):
            hit("explicit", f"posted in {location}")

        for m in title_part.finditer(title):
            part = title_noise.sub(" ", m.group(1) or m.group(2) or "").strip()
            if part in STATE_CODES:
                hit("elsewhere", f"title: {title}")
            elif part and (rm := region_only.fullmatch(part)):
                hit(kind(rm.group("regions")), f"title: {title}")

        for text, label in ((title, "title"), (desc, "")):
            for pattern in contexts:
                for m in pattern.finditer(text):
                    k = kind(m.group("regions"))
                    # "Our global remote team": a generic word right before "remote" isn't a location.
                    if k == "generic" and pattern is contexts[2]:
                        continue
                    hit(k, f"title: {title}" if label else _snippet(desc, m))
        if m := us_states_list.search(desc):
            hit("elsewhere", _snippet(desc, m))
        if m := generic_open.search(desc):
            hit("generic", _snippet(desc, m))
        if allowed_re and "explicit" not in hits and (m := allowed_re.search(desc)):
            hit("mention", _snippet(desc, m))  # "teams across North America, Latin America…"

        if "explicit" in hits:
            return "open", hits["explicit"]
        if "elsewhere" in hits:
            return "restricted", hits["elsewhere"]
        for k in ("generic", "mention"):
            if k in hits:
                return "open", hits[k]
        return "unstated", ""

    return scope


def _snippet(text: str, m: re.Match, pad: int = 60) -> str:
    start, end = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
