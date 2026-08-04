"""Who the request is for. One tenant, named in one place.

There is no identity provider and no login (docs/security.html says so plainly),
so the tenant comes from the environment and falls back to the seeded demo
company. It is written as a FastAPI dependency rather than as a module constant
for one reason: it is the exact seam authentication will replace, and a
dependency can be overridden in a test. That override is what lets
tests/test_change_view.py ask a handler for another company's change id and
prove the answer is a 404 rather than a row. A constant read straight from the
handler could not be asked that question.

This module is the only place that answers the question. It used to answer it
one way -- a hardcoded "MEP" -- while app/web/views/proceedings.py answered it
another, by reading STRATA_COMPANY_ID. Apart, each was right about its own
screens. Mounted on one application, the pair leaked: a deployment set to a
second company got an empty list and an empty queue, and the change screen still
served the first company's source text and claims. Two answers to one question
is the failure a chokepoint exists to remove, so there is now one function and
the view imports it.

Read on every call and never cached, so a deployment can move the tenant without
a restart and a test can act as another company and watch the screens refuse.

Nothing here decides anything. It names a tenant and looks up a display name.
Every read that returns content still goes through app/state/queries.py.
"""

import json
import os
from functools import lru_cache

from app.seed import DATA_DIR

# The one tenant in data/company_context.json. Everything in the corpus is
# scoped to it, and it is what an unconfigured deployment gets.
DEMO_COMPANY_ID = "MEP"

# The tenant, and the display name beside it in the masthead.
COMPANY_ENV = "STRATA_COMPANY_ID"
COMPANY_NAME_ENV = "STRATA_COMPANY_NAME"


def current_company() -> str:
    """The company whose rows this request may read."""
    return os.environ.get(COMPANY_ENV, "").strip() or DEMO_COMPANY_ID


@lru_cache(maxsize=1)
def _names() -> dict[str, str]:
    """Company id to display name, read once from the corpus.

    There is no companies table -- the corpus is one tenant and the name lives
    in data/company_context.json, which app/seed.py already reads. Copying the
    string into this module would give one fact two homes and let them drift.

    A missing or unreadable file returns an empty map rather than raising. The
    consequence is a header showing the tenant id instead of the name, which is
    a smaller failure than a 500 on every screen, and it is still true.
    """
    try:
        context = json.loads(
            (DATA_DIR / "company_context.json").read_bytes().decode("utf-8")
        )
    except (OSError, ValueError):
        return {}

    company = context.get("company", {})
    short_name, name = company.get("short_name"), company.get("name")
    return {short_name: name} if short_name and name else {}


def company_name(company_id: str) -> str:
    """The company's name, or its id where nothing here knows the name.

    Never a guess and never a blank. An unknown tenant shows as its id, which is
    honest and still identifies the scope every read on the page ran under.
    """
    return _names().get(company_id, company_id)


__all__ = [
    "COMPANY_ENV",
    "COMPANY_NAME_ENV",
    "DEMO_COMPANY_ID",
    "company_name",
    "current_company",
]
