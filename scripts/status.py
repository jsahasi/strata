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
    """Run the suite and report what it said, or refuse to report a number.

    THE BUG THIS DOCSTRING EXISTS FOR. This used to regex for "N passed" and
    fall back to 0 when nothing matched. A pytest COLLECTION ERROR prints no
    summary line at all -- one half-written module that imports something not
    yet created takes the whole run down before a single test executes -- so the
    fallback turned "I could not measure" into "0 tests pass". That number then
    landed in state.json, which ADR-42 makes authoritative over prose, and it is
    indistinguishable from a suite that is empty or a suite where everything
    failed. Three very different facts, one number.

    Absence is denial applies to our own instruments. passed is None when the
    run produced no summary, `measured` says so, and `problem` carries the
    reason. A consumer that wants a number must handle the None; that is the
    point, because there genuinely was not one.
    """
    raw = _run(str(ROOT / ".venv/bin/python"), "-m", "pytest", "tests/", "-q", "--no-header")
    return {
        **parse_pytest_summary(raw),
        "files": sorted(p.name for p in (ROOT / "tests").glob("test_*.py")),
        "requires_network": False,
        "requires_api_key": False,
    }


def parse_pytest_summary(raw: str) -> dict:
    """Read a pytest run, or say it could not be read. Pure, so it is testable.

    Split out from tests() precisely so the collection-error case can be proved
    against real captured output instead of by running a broken suite on purpose.
    """
    passed = re.search(r"(\d+) passed", raw)
    failed = re.search(r"(\d+) failed", raw)
    errors = re.search(r"(\d+) error", raw)

    # A collection error aborts before any test runs, so neither counter appears.
    aborted = passed is None and failed is None
    problem = ""
    if aborted:
        # Two shapes pytest uses. Both stop at whitespace so the run of
        # underscores it prints as a separator is not read as part of the path.
        broken = re.findall(
            r"ImportError while importing test module '([^']+)'|ERROR collecting (\S+)", raw
        )
        names_found = {Path(a or b).name for a, b in broken if (a or b)}
        if names_found:
            names = ", ".join(sorted(names_found))
            problem = f"collection failed; could not import {names}"
        else:
            problem = "pytest produced no summary line: " + " ".join(raw.split())[-200:]

    return {
        "passed": int(passed.group(1)) if passed else None,
        "failed": int(failed.group(1)) if failed else None,
        "errors": int(errors.group(1)) if errors else 0,
        "measured": not aborted,
        "problem": problem,
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


CAPABILITIES = {
    # capability -> (module path, a symbol that must exist in it)
    "ingestion": ("app/ingestion/ingest.py", "def ingest_version"),
    "normalization": ("app/text/normalize.py", "def normalize"),
    "citation_verifier": ("app/verification/verifier.py", "def verify_citation"),
    "occurrence_disambiguation": ("app/verification/verifier.py", "def occurrence_index"),
    "deterministic_diff": ("app/diff/engine.py", "def diff"),
    "draft_final_split": ("app/interpretation/action.py", "def action_vocabulary"),
    "tenant_scoped_reads": ("app/state/queries.py", "def passages_for_company"),
    "audit_log_hash_chained": ("app/state/audit.py", "def verify_chain"),
    "audit_actor_attribution": ("app/state/audit.py", "actor_user_id"),
    "project_workspace": ("app/state/projects.py", "def create_project"),
    "review_centre": ("app/state/review.py", "def coverage_for_project"),
    "web_surface": ("app/web/views", ""),
    "authentication": ("app/auth/sessions.py", "def login"),
    "roles_and_permissions": ("app/state/identity.py", "def permissions_for_user"),
    "segregation_of_duties": ("app/auth/policy.py", "def can_approve"),
    "model_interpretation": ("app/interpretation", "anthropic"),
    "evals": ("app/evals/run.py", "manifest"),
}

# Where each capability is specified, so a false entry points at its design rather
# than just reporting an absence.
DESIGNED_IN = {
    "authentication": "docs/security.html - Authentication and authorization",
    "roles_and_permissions": "docs/security.html - roles: analyst, obligation owner, admin",
    "segregation_of_duties": "docs/security.html - the analyst never approves their own reading",
    "model_interpretation": "docs/tdd.html - the only stage permitted to call a model",
    "evals": "docs/.ai/tasks.html T17/T18 - labelled set, precision and recall",
    "review_centre": "docs/.ai/demo-notes.html - what AI Fund demoed",
    "project_workspace": "docs/.ai/demo-notes.html - project list with running changes",
    "web_surface": "docs/web-design.html - four screens",
}


def capabilities() -> dict:
    """Read each capability off the filesystem instead of asserting it.

    Everything else in state.json is derived, so a hand-written dict of what is
    built was the one claim in the file that could quietly go out of date -- the
    precise failure the file exists to prevent. A capability is present when its
    module exists and contains the symbol that implements it; a stub file with the
    right name is therefore not enough to claim the capability.
    """
    out = {}
    for name, (rel, symbol) in CAPABILITIES.items():
        target = ROOT / rel
        if not target.exists():
            out[name] = False
            continue
        if target.is_dir():
            files = [p for p in target.rglob("*.py") if p.name != "__init__.py"]
            # A directory with a symbol must still contain it. Checking only that the
            # folder is non-empty reported model_interpretation as built while no model
            # call existed anywhere -- the exact false claim this file exists to prevent.
            out[name] = bool(files) and (
                not symbol
                or any(symbol in p.read_text(encoding="utf-8") for p in files)
            )
            continue
        out[name] = (
            True if not symbol else symbol in target.read_text(encoding="utf-8")
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
        "built": {k: v for k, v in capabilities().items() if v},
        "designed_not_built": {
            k: DESIGNED_IN.get(k, "specified in docs/")
            for k, present in capabilities().items()
            if not present
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
    t = state["tests"]
    count = f"{t['passed']} tests pass" if t["measured"] else "TEST COUNT NOT MEASURED"
    print(
        f"  {count}, "
        f"{len(state['modules'])} modules, "
        f"{len(state['decisions'])} decisions, "
        f"{len(state['designed_not_built'])} things designed but not built"
    )
    if not t["measured"]:
        # Loud, and on stderr, because a silent zero is what this replaced.
        print(f"  WARNING: {t['problem']}", file=sys.stderr)
        print("  state.json records no test count rather than a false one.", file=sys.stderr)
    elif t["failed"]:
        print(f"  WARNING: {t['failed']} tests FAILING", file=sys.stderr)


if __name__ == "__main__":
    main()
