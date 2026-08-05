"""The web layer: FastAPI routes, Jinja templates, one stylesheet.

Thin by design, per docs/architecture.html. This layer reads projections and
renders them. Nothing here decides anything, and nothing here may reach past
app.state.queries for a tenant-scoped read.

This module holds paths only. It imports nothing beyond the standard library on
purpose: routes, tests and the seed script all need to name the template and
static directories, and a path constant that cannot fail to import is the
cheapest way to keep them agreeing with each other.

Contract for whoever mounts the app:

    from fastapi.staticfiles import StaticFiles
    from app.web import STATIC_DIR, STATIC_URL_PATH
    from app.web.templating import build_templates

    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    templates = build_templates()

NOT `Jinja2Templates(directory=TEMPLATES_DIR)`, which is what this file used to
say. base.html reads two things from the environment rather than from a view's
context, and a bare object carries neither -- so the screen renders with its
administrative menu silently missing while every one of its own tests passes.
Sixteen view modules each built their own object, which is how six screens ended
up mounted and linked from nowhere. app/web/templating.py holds the factory and
the reasoning; tests/test_admin_index.py fails if a view module builds one of its
own again. TEMPLATES_DIR stays exported for the handful of callers that name the
directory for some other purpose.

templates/base.html links the stylesheet at a fixed absolute path,
STATIC_URL_PATH + "/strata.css", so the template does not depend on a request
object being present in every render context. Mount it anywhere else and the
page loads unstyled.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
STATIC_URL_PATH = "/static"

# The stylesheet is one file by decision, not by accident (ADR-012). Named here
# so a rename breaks an import rather than silently serving an unstyled page.
STYLESHEET = "strata.css"

__all__ = [
    "PACKAGE_DIR",
    "STATIC_DIR",
    "STATIC_URL_PATH",
    "STYLESHEET",
    "TEMPLATES_DIR",
]
