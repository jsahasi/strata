#!/usr/bin/env python3
"""Copy docs/ into deploy/site/docs/ so the documents are served with the site.

WHY THIS IS A BUILD STEP AND NOT A SECOND COPY IN THE REPOSITORY. The obvious
way to serve these is to keep a copy under deploy/site/. That gives every
document two homes, and this project has spent a whole day on what happens when
one description of a thing is edited and the other is not. A generated copy
cannot drift, because it is overwritten every time this runs and it is
gitignored so nobody edits it by hand.

WHY THE DOT IS DROPPED. Structured project memory lives in docs/.ai/ by
convention and that convention stays. But a path segment starting with a dot is
fragile everywhere it is served: the usual nginx hardening line
`location ~ /\\. { deny all; }` answers 403 for the whole directory, rsync and
several static hosts skip dotted directories by default, and a reader who
notices the path wonders whether they were meant to see it. So the published
copy uses docs/ai/ and the links are rewritten to match. The source keeps its
dot; only the artefact changes.

WHAT IT REFUSES TO DO. It does not rewrite prose, it does not filter documents by
what they admit, and it does not touch anything under docs/. If a document says
something the author would rather a reader did not see, the answer is to change
the document, not to have the publisher quietly drop it -- a publisher that
chooses what to show is a second, invisible editor.

Run:  .venv/bin/python scripts/publish_docs.py
      make publish-docs
"""

from __future__ import annotations

import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs"
NOTES = SOURCE / ".ai"
TARGET = ROOT / "deploy" / "site" / "docs"
NOTES_TARGET = TARGET / "ai"

#: Copied as-is beside the documents that link to them.
ASSETS = ("docs-nav.css", "diagrams.css")

#: KEPT LOCAL. Everything else in docs/.ai/ is working memory a reader benefits
#: from -- the decisions, the gaps, the findings, all of it describes the system.
#: interview-bank.html does not. It is rehearsal apparatus: for every question it
#: carries the tempting wrong answer and the follow-up that comes next, which
#: reads as somebody coaching themselves rather than as documentation of a
#: product. docs/tech-questions-faq.html carries the same 38 questions and the
#: same answers, with the coaching removed, and that is the one to publish.
#:
#: It was public for several hours before anybody noticed, because docs/.ai/
#: publishes as docs/ai/ and nothing here said otherwise. A file is published
#: unless this tuple says it is not, and that default is the right way round --
#: which is why the exclusion has to name itself out loud.
NOT_PUBLISHED = ("interview-bank.html",)

#: Not HTML, and linked from docs/.ai/index.html, so the link checker below
#: found it missing on the first run. state.json is the one file in that
#: directory nobody writes by hand -- ADR-42 makes it authoritative over the
#: prose, which is precisely why it has to travel with the prose. resume.md is
#: a handover note to whoever picks the work up next; it is published for the
#: same reason everything else here is, that a reader learns more from the
#: working state than from a tidied summary of it.
NOTE_FILES = ("state.json", "resume.md")


def _rewrite_top_level(html: str) -> str:
    """A document in docs/ links to its notes as `.ai/x.html`; drop the dot."""
    return html.replace('href=".ai/', 'href="ai/')


def _rewrite_notes(html: str) -> str:
    """A document in docs/.ai/ links to its siblings the long way round.

    The nav was generated with `../.ai/x.html` rather than a bare `x.html`,
    which resolves correctly and reads oddly. Published, it would also point at
    a dotted directory that does not exist in the artefact, so it has to change
    rather than merely wanting to.
    """
    return html.replace('href="../.ai/', 'href="')


def publish() -> int:
    if not SOURCE.is_dir():
        print(f"REFUSED: {SOURCE} is not a directory", file=sys.stderr)
        return 1

    # Rebuilt from empty every time. An incremental copy leaves a document
    # behind after it is renamed, and a stale page nobody links to is the
    # hardest kind to notice -- it is only ever found by somebody following an
    # old URL, which is to say by the worst possible reader.
    if TARGET.exists():
        shutil.rmtree(TARGET)
    NOTES_TARGET.mkdir(parents=True)

    written: list[str] = []

    for path in sorted(SOURCE.glob("*.html")):
        html = _rewrite_top_level(path.read_text(encoding="utf-8"))
        (TARGET / path.name).write_text(html, encoding="utf-8")
        written.append(f"docs/{path.name}")

    for path in sorted(NOTES.glob("*.html")):
        if path.name in NOT_PUBLISHED:
            continue
        html = _rewrite_notes(path.read_text(encoding="utf-8"))
        (NOTES_TARGET / path.name).write_text(html, encoding="utf-8")
        written.append(f"docs/ai/{path.name}")

    for name in NOTE_FILES:
        extra = NOTES / name
        if extra.exists():
            shutil.copy2(extra, NOTES_TARGET / name)
            written.append(f"docs/ai/{name}")

    for name in ASSETS:
        asset = SOURCE / name
        if asset.exists():
            shutil.copy2(asset, TARGET / name)
            written.append(f"docs/{name}")
        else:
            print(f"note: {name} is not in docs/, so nothing was copied for it")

    missing = _broken_links()
    for line in written:
        print(f"  {line}")
    print(f"\n{len(written)} files written to {TARGET.relative_to(ROOT)}")

    if missing:
        print("\nREFUSED: the published copy has links that go nowhere.")
        for line in missing:
            print(f"  {line}")
        print(
            "\nA document served with a broken link is worse than one not served,"
            "\nbecause the reader blames the product rather than the deploy."
        )
        return 1

    print("Every internal link in the published copy resolves.")
    return 0


def _broken_links() -> list[str]:
    """Follow every relative link in the artefact and name the ones that miss.

    Derived rather than listed. The nav is generated, the documents cross-link
    by hand, and the rewrite above changes paths -- three ways for a link to
    break, none of which a person would reliably catch by reading.
    """
    broken: list[str] = []
    href = re.compile(r'(?:href|src)="([^"#?:]+)"')
    for page in sorted(TARGET.rglob("*.html")):
        for link in href.findall(page.read_text(encoding="utf-8")):
            if link.startswith(("/", "#", "mailto:")):
                continue
            if not (page.parent / link).resolve().exists():
                broken.append(f"{page.relative_to(TARGET)} -> {link}")
    return broken


if __name__ == "__main__":
    raise SystemExit(publish())
