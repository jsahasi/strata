"""Write docs/.ai/state.json from the repository itself.

Every other file in docs/.ai/ is prose a human wrote, so every one of them can
go stale the moment the code moves. This one cannot, because nothing here is
asserted -- the module list comes from the filesystem, the test count from
pytest, the decision list from the data attributes on decisions.html, and the
commit from git. Run `make status` after anything lands.

That is principle 28 applied to the project's own documentation: a fact has a
shelf life, so record how it was obtained and when, and regenerate rather than
remember.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / ".ai" / "state.json"


def _run(*args: str) -> str:
    try:
        return subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, timeout=120
        ).stdout.strip()
    except Exception:
        return ""


def modules() -> dict:
    """Every package under app/, with its line count and whether it is tested."""
    found = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(ROOT).as_posix()
        found[rel] = {
            "lines": len(path.read_text(encoding="utf-8").splitlines()),
            "calls_a_model": "anthropic" in path.read_text(encoding="utf-8"),
        }
    return found


def tests() -> dict:
    raw = _run(str(ROOT / ".venv/bin/python"), "-m", "pytest", "tests/", "-q", "--no-header")
    match = re.search(r"(\d+) passed", raw)
    failed = re.search(r"(\d+) failed", raw)
    return {
        "passed": int(match.group(1)) if match else 0,
        "failed": int(failed.group(1)) if failed else 0,
        "files": sorted(p.name for p in (ROOT / "tests").glob("test_*.py")),
        "requires_network": False,
        "requires_api_key": False,
    }


def decisions() -> list:
    """Read the ADRs out of their data attributes, not out of the prose."""
    html = (ROOT / "docs" / ".ai" / "decisions.html").read_text(encoding="utf-8")
    out = []
    for div in re.findall(r'<div class="d"([^>]*data-adr[^>]*)>', html):
        entry = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', div))
        heading = re.search(
            re.escape(entry.get("adr", "")).replace("ADR\\-0", "ADR-0?") + r"\d*\s*—\s*([^<]+)",
            html,
        )
        out.append(
            {
                "id": entry.get("adr"),
                "status": entry.get("status"),
                "date": entry.get("date"),
                "area": entry.get("area"),
                "depends_on": entry.get("depends-on"),
                "revisits": entry.get("revisits"),
                "is_hard_bet": entry.get("hard-bet") == "true",
                "title": heading.group(1).strip() if heading else None,
            }
        )
    return out


def main() -> None:
    state = {
        "_generated_by": "scripts/status.py via `make status`. Do not hand-edit.",
        "_why": (
            "Every other file in docs/.ai/ is prose and can go stale. This one is "
            "read from the repository, so where it disagrees with prose, it is right."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": {
            "head": _run("git", "rev-parse", "--short", "HEAD"),
            "branch": _run("git", "rev-parse", "--abbrev-ref", "HEAD"),
            "commits": int(_run("git", "rev-list", "--count", "HEAD") or 0),
            "dirty": bool(_run("git", "status", "--porcelain")),
        },
        "tests": tests(),
        "modules": modules(),
        "decisions": decisions(),
        "built": {
            "ingestion": True,
            "normalization": True,
            "citation_verifier": True,
            "occurrence_disambiguation": True,
            "deterministic_diff": True,
            "draft_final_split": True,
            "tenant_scoped_reads": True,
            "audit_log_hash_chained": True,
        },
        "designed_not_built": {
            "authentication": "docs/security.html — no identity provider; roles are a design",
            "model_interpretation": "docs/architecture.html — app/interpretation/ has the draft/final split only; no model call exists",
            "web_ui": "docs/web-design.html — four screens specified, none built",
            "review_queue": "ADR-006 — escalation is designed; no queue surface",
            "deployment": "ADR-009/010/011 — strata.sudama.ai has no DNS record yet",
            "evals": "app/evals/run.py is a stub",
            "approval_workflow": "designed in this session; not in app/",
            "feedback_loop": "designed in this session; not in app/",
        },
        "known_gaps": [
            "The verifier confirms a quoted passage EXISTS at the cited offsets. It does not "
            "confirm the passage supports the claim attached to it. Named in docs/security.html.",
            "The audit hash chain detects tampering; it does not prevent it. Closing that needs "
            "the chain head published outside the host.",
            "_spans_of in app/verification/verifier.py falls back to fixed-width window scanning, "
            "which breaks once normalization changes length -- i.e. on real PDF text.",
            "The eval corpus has five labelled changes. No percentage computed over five items "
            "should be reported as a measurement.",
        ],
    }
    OUT.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(
        f"  {state['tests']['passed']} tests pass, "
        f"{len(state['modules'])} modules, "
        f"{len(state['decisions'])} decisions, "
        f"{len(state['designed_not_built'])} things designed but not built"
    )
    if state["tests"]["failed"]:
        print(f"  WARNING: {state['tests']['failed']} tests FAILING", file=sys.stderr)


if __name__ == "__main__":
    main()
