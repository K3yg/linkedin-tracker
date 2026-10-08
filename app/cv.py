"""The user's CV: cv/base.tex (LaTeX, exported from Overleaf)."""

import re

from .config import ROOT

BASE_TEX = ROOT / "cv" / "base.tex"


def plain_text() -> str:
    """The CV body (no contact header) as plain text, for prompts."""
    tex = BASE_TEX.read_text(encoding="utf-8")
    body = tex[tex.find(r"\section*{"):tex.rfind(r"\end{document}")]
    body = re.sub(r"(?m)%.*$", "", body)
    body = re.sub(r"\\setlength\\\w+\{[^}]*\}", " ", body)
    body = re.sub(r"\\entry\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}", r"\n\1 — \3 (\2)", body)
    body = re.sub(r"\\section\*?\{([^}]*)\}", r"\n## \1\n", body)
    body = body.replace(r"\item", "\n-").replace(r"\textasciitilde", "~").replace(r"\&", "&")
    body = re.sub(r"\\fa[A-Za-z]+|\\space|\\vspace\{[^}]*\}|\\setlength\\\w+\{[^}]*\}|\\(?:begin|end)\{[^}]*\}", " ", body)
    body = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", body)
    body = re.sub(r"\\[a-zA-Z]+|[{}]", " ", body)
    return "\n".join(" ".join(line.split()) for line in body.splitlines() if line.strip())
