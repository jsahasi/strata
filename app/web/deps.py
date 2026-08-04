"""Who the request is for. One tenant, named in one place.

There is no identity provider and no login (docs/security.html says so plainly),
so `current_company` returns the seeded demo tenant. It is written as a FastAPI
dependency rather than as a module constant for one reason: it is the exact seam
authentication will replace, and a dependency can be overridden in a test. That
override is what lets tests/test_change_view.py ask a handler for another
company's change id and prove the answer is a 404 rather than a row. A constant
read straight from the handler could not be asked that question.

Nothing here decides anything. It names a tenant and looks up a display name.
Every read that returns content still goes through app/state/queries.py.
"""

import json
from functools import lru_cache

from app.seed import DATA_DIR

# The one tenant in data/company_context.json. Everything in the corpus is
# scoped to it.
DEMO_COMPANY_ID = "MEP"


def current_company() -> str:
    """The company whose rows this request may read."""
    return DEMO_COMPANY_ID


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


__all__ = ["DEMO_COMPANY_ID", "company_name", "current_company"]
