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
    from fastapi.templating import Jinja2Templates
    from app.web import STATIC_DIR, STATIC_URL_PATH, TEMPLATES_DIR

    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

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
