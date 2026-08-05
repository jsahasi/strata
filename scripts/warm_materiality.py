"""Earn the materiality verdict for every change a demo project can reach.

ADR-86 names this under "what would reverse it": a materiality pass in the
pipeline that judges on ingest. This is that pass, run by hand rather than on
ingest. It calls exactly what the change screen calls, so nothing is staged --
a verdict earned here is the same verdict, by the same path, with the same
audit row. It exists because a reviewer landing on a project list should not
have to click thirty changes to make the product show its work.
"""
import sys
from app.state.db import session_scope
from app.web.views.changes import transport_factory
from app.interpretation.propose import materiality_for_company
from app.web.views.projects import _change_rows
from app.state.models import Project
from sqlalchemy import select

done = skipped = failed = 0
with session_scope() as s:
    seen = set()
    for p in s.execute(select(Project)).scalars().all():
        try:
            rows = _change_rows(s, p.company_id, p.id)
        except Exception as e:
            print(f"{p.id}: cannot list changes: {type(e).__name__}"); continue
        for r in rows:
            cid = getattr(r, "change_id", None) or getattr(r, "id", None)
            if not cid or (p.company_id, cid) in seen:
                continue
            seen.add((p.company_id, cid))
            try:
                run = materiality_for_company(
                    s, p.company_id, cid, transport=transport_factory()
                )
                if run.judgement is not None:
                    done += 1
                else:
                    skipped += 1
                    print(f"  no verdict {cid}: {getattr(run,'reason_code',None) or getattr(run,'code',None)}")
            except Exception as e:
                failed += 1
                print(f"  FAILED {cid}: {type(e).__name__}: {e}")
print(f"\njudged={done} no-verdict={skipped} failed={failed}")
