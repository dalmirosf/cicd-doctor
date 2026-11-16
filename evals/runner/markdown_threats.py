"""A threat scanner for rendered markdown: what could act on a reader (SPEC §11.2, §13 P1b, D-64).

It is the oracle of the render and injection tests. It removes what is inert (fenced code blocks
and inline code spans) and reports what is left that could be a clickable link, an image, raw
HTML, a live mention, an issue reference, an HTML entity or markup that changes the page. The
only things it lets through are the ones the renderer writes on purpose: autolinks to allowlisted
hosts, the link to the failed step on github.com, and the hidden marker of the sticky comment.
"""

import re

from cicd_doctor.core.render import ALLOWED_HOSTS, ZWJ

_HOSTS = "|".join(re.escape(host) for host in sorted(ALLOWED_HOSTS))
_ALLOWED_AUTOLINK = re.compile(rf"<https://(?:{_HOSTS})(?:[/?#][^\s<>]*)?>")
_STEP_LINK = re.compile(
    r"\[open the step\]\(https://github\.com/[\w.\-]+/[\w.\-]+/actions/runs/\d+/job/\d+#step:\d+:1\)"
)
_MARKER = re.compile(r"<!-- cicd-doctor:v1 pr=\d+ workflow=\d+ -->")
_FENCE = re.compile(r"^\s{0,3}(`{3,})")
_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def strip_code(markdown: str) -> str:
    """The markdown without fenced blocks and inline code spans (they cannot act on a reader)."""
    kept: list[str] = []
    fence: str | None = None
    for line in markdown.split("\n"):
        match = _FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)
            continue
        if fence is not None:
            if match and len(match.group(1)) >= len(fence) and line.strip() == match.group(1):
                fence = None
            continue
        kept.append(_strip_inline_code(line))
    return "\n".join(kept)


def _strip_inline_code(line: str) -> str:
    """Remove code spans from a line, scanning left to right as CommonMark does.

    A backslash before ASCII punctuation is an escape and consumes both characters, so ``\\```
    never opens a code span. A run of N backticks opens a span that ends at the next run of
    exactly N; inside a span a backslash is literal. A run with no closer is plain text. Escapes
    become a placeholder so that ``\\[x\\](y)`` is not read as a link either.
    """
    out: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line) and line[index + 1] in _PUNCTUATION:
            # An escaped "_" is still a word character (so no word boundary appears around it)
            # but it is not a thematic break; any other escaped mark is a neutral placeholder.
            out.append("a" if line[index + 1] == "_" else "\u0001")
            index += 2
        elif char == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = line[index:end]
            close = re.search(rf"(?<!`){re.escape(run)}(?!`)", line[end:])
            if close:
                out.append("\u0002")  # a span leaves a mark: text after it is not at line start
                index = end + close.end()
            else:
                out.append(run)
                index = end
        else:
            out.append(char)
            index += 1
    return "".join(out)


_HEADING = re.compile(
    r"^### CICD Doctor: (?:Likely cause: [a-z_]+ \((?:high|medium|low) confidence\)"
    r"(?: \(\+\d+ more\))?|Could not diagnose)$",
    re.MULTILINE,
)
_RULE_LINE = re.compile(r"^---$", re.MULTILINE)
_SIMILAR_PREFIX = re.compile(r"^- `[a-z_]+` \(score \d\.\d\d\): ", re.MULTILINE)


def threats(markdown: str, *, document: bool = False) -> list[str]:
    """Findings that make the markdown unsafe to show; an empty list means it is safe.

    With ``document=True`` the scanner lets through the structure the renderer writes itself: the
    comment heading, the separator before the footer and the prefix of a similar-failure bullet.
    The text after the prefix is still scanned.
    """
    text = markdown
    if document:  # on the raw markdown: the bullet prefix contains a code span
        text = _HEADING.sub("", text)
        text = _RULE_LINE.sub("", text)
        text = _SIMILAR_PREFIX.sub("", text)
    text = strip_code(text)  # also turns every backslash escape into a placeholder
    text = _MARKER.sub("", text)
    text = _ALLOWED_AUTOLINK.sub("", text)
    text = _STEP_LINK.sub("", text)
    found: list[str] = []
    checks = {
        "raw html or comment": r"<[A-Za-z/!?]",
        "link or image destination": r"\]\(",
        "link reference definition": r"^\s{0,3}\[[^\]]+\]:",
        "image": r"!\[",
        "bare url": r"(?i)(?:\b[a-z][a-z0-9+.\-]*://|\bwww\.)",
        "live mention": rf"@(?!{ZWJ})[A-Za-z0-9_\-]",
        "html entity": r"&(?!amp;|lt;|gt;)#?\w+;",
        "issue reference": r"(?<![\w/])(?:[\w.\-]+/[\w.\-]+)?#\d+\b|\bGH-\d+\b",
        "heading": r"^\s{0,3}#{1,6}(?:\s|$)",
        "blockquote": r"^\s{0,3}>",
        "table row": r"^\s{0,3}\|",
        "thematic break or setext": r"^\s{0,3}(?:[-*_]\s*){3,}$|^\s{0,3}=+\s*$",
        "list item": r"^\s{0,3}(?:[-+*]|\d+[.)])\s",
    }
    for name, pattern in checks.items():
        if re.search(pattern, text, re.MULTILINE):
            found.append(name)
    return found
