"""Reasons to hide a job that only show up in its description.

- seniority: the posting is really a director / principal / staff / manager role, or asks
  for more years of experience than profile.max_years_required.
- language:  the posting isn't in English, or requires a language other than English
  (or Portuguese, which the user speaks natively).

job_issue(title, description) returns a short reason, or "" when the job is fine.
"""

import re
from collections import Counter

LEADERSHIP = re.compile(
    r"\b(?:associate |senior |executive |managing )?director\b|\bhead of\b|\bvice president\b"
    r"|\b(?:principal|staff|distinguished) (?:software |ai |ml |machine learning |data )?(?:engineer|developer)\b"
    r"|\bengineering manager\b|\bmanager of engineering\b",
    re.I,
)
ROLE_INTRO = re.compile(r"\b(?:seeking|hiring|looking for|recruiting|searching for)\s+an?\s+([^.]{0,90})", re.I)
REPORTS_TO = re.compile(r"report(?:s|ing)?\s+(?:directly\s+)?(?:in)?to\s+(?:the|our|a)?\s*$", re.I)
YEARS = re.compile(
    r"(?:minimum of|at least|min\.?)?\s*(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:\+\s*)?(?:or more\s+)?(?:years?|anos)['’]?"
    r"(?:\s+\w+){0,4}?\s+(?:experience|experiência)",  # "5+ years of experience", "3 anos de experiência"
    re.I,
)

LANGS = (
    "german|french|dutch|italian|spanish|polish|swedish|danish|norwegian|finnish|czech|slovak|romanian|"
    "hungarian|greek|hebrew|japanese|mandarin|chinese|korean|arabic|russian|turkish|ukrainian|flemish"
)
LANG_REQUIRED = re.compile(
    rf"\b(?:fluent|fluency|native|proficient|proficiency|business[- ]level|professional|excellent|strong|good)\b"
    rf"[^.]{{0,30}}?\b({LANGS})\b"
    rf"|\b({LANGS})\b[^.]{{0,25}}?\b(?:required|mandatory|a must|essential|fluency|fluent|native|speaker|c1|c2|b2)\b"
    rf"|\bwith ({LANGS})\b|\b({LANGS})[- ]speaking\b",
    re.I,
)
OPTIONAL = re.compile(r"\b(?:plus|nice to have|bonus|advantage|advantageous|preferred|desirable|beneficial)\b", re.I)

# Frequent function words per language; a posting is "not English" if another language wins.
STOPWORDS = {
    "en": "the and with you we for our will your are this that have from experience team",
    "de": "und der die das mit für wir sie ist eine bei auf ihre deine oder nicht",
    "fr": "les des pour vous nous une avec dans est sur votre notre vos aux",
    "it": "della per con che una sono nel delle alla nostro nostra degli",
    "es": "los las para con una del que nuestro nuestra somos como",
    "nl": "het een voor met wij jij van zijn onze bent",
    "pl": "oraz jest się dla przez które nasz",
    "pt": "e em do da dos das com uma você não são nossa nosso ou também experiência vaga",
}
OK_LANGS = {"en", "pt"}  # the user is a native Portuguese speaker
STOPSETS = {lang: set(words.split()) for lang, words in STOPWORDS.items()}


def _leadership(text: str) -> str:
    for m in LEADERSHIP.finditer(text):
        if not REPORTS_TO.search(text[max(0, m.start() - 30):m.start()]):
            return m.group(0)
    return ""


def seniority_issue(description: str, max_years: int) -> str:
    if hit := _leadership(description[:250]):
        return f"really a {hit.lower()} role"
    for intro in ROLE_INTRO.finditer(description):
        if hit := _leadership(intro.group(1)):
            return f"really a {hit.lower()} role"
    years = years_required(description)
    if years and years > max_years:
        return f"asks for {years}+ years of experience"
    return ""


def years_required(description: str | None) -> int | None:
    """Most years of experience the posting asks for ("5+ years of Python, 2+ with AWS" -> 5)."""
    years = [int(y) for y in YEARS.findall(description or "") if 0 < int(y) <= 15]
    return max(years) if years else None


def _language_scores(text: str) -> dict[str, int]:
    words = Counter(re.findall(r"[a-zà-öø-ÿ]+", text.lower()))
    return {lang: sum(words[w] for w in stop) for lang, stop in STOPSETS.items()}


def language(text: str) -> str:
    """Main language of a posting: 'en', 'pt', 'de'… (English when unclear)."""
    scores = _language_scores(text)
    top = max(scores, key=scores.get)
    return top if scores[top] > scores["en"] else "en"


def language_issue(title: str, description: str) -> str:
    scores = _language_scores(description)
    top = max(scores, key=scores.get)
    if top not in OK_LANGS and scores[top] > scores["en"]:
        return f"posting not in English ({top})"
    for m in LANG_REQUIRED.finditer(f"{title}. {description}"):
        window = f"{title}. {description}"[max(0, m.start() - 40):m.end() + 40]
        if not OPTIONAL.search(window):
            lang = next(g for g in m.groups() if g)
            return f"requires {lang.capitalize()}"
    return ""


def build(cfg: dict):
    max_years = int(cfg["profile"]["max_years_required"])

    def job_issue(title: str | None, description: str | None) -> str:
        if not description:
            return ""
        return seniority_issue(description, max_years) or language_issue(title or "", description)

    return job_issue
