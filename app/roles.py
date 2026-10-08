"""Title filter: keep only software-development roles.

A title passes if it contains one of `role_keywords` and none of `exclude_title_words`.
Keywords match whole words, case-insensitively; a space also matches "-", "_", "/" or
nothing, so "full stack" matches "Full-Stack" and "Fullstack".
"""

import re
from collections.abc import Callable


def _pattern(words: list[str]) -> re.Pattern | None:
    parts = []
    for w in words:
        tokens = w.lower().split()
        if tokens:
            parts.append(r"[\s\-_/]*".join(re.escape(t) for t in tokens))
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.I)


def build(cfg: dict) -> Callable[[str], bool]:
    include = _pattern(cfg["role_keywords"])
    exclude = _pattern(cfg["exclude_title_words"])

    def is_dev_role(title: str | None) -> bool:
        if not title:
            return False
        if exclude and exclude.search(title):
            return False
        return include.search(title) is not None if include else True

    return is_dev_role
