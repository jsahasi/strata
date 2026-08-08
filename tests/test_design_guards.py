"""Guards for the visual system: the font is ours, the record stays flat.

Both rules are cheap to break by accident and expensive to notice late. A CDN
link is one paste away, and this project has already had to correct a
sub-processor page that had gone untrue. A gradient behind evidence is the
same class of error in a different medium.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_FONT = ROOT / "app/web/static/fonts/manrope-latin.woff2"
SITE_FONT = ROOT / "deploy/site/fonts/manrope-latin.woff2"

CDN_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "use.typekit.net",
    "cdn.jsdelivr.net",
    "unpkg.com",
)

SURFACES = list((ROOT / "app/web/templates").glob("*.html")) + list(
    (ROOT / "deploy/site").glob("*.html")
) + [ROOT / "app/web/static/strata.css", ROOT / "deploy/site/site.css"]


@pytest.mark.parametrize("font", [APP_FONT, SITE_FONT])
def test_the_display_face_is_in_the_repository(font):
    assert font.exists(), f"{font} is missing; the design depends on it"
    assert font.read_bytes()[:4] == b"wOF2", f"{font} is not a woff2"


@pytest.mark.parametrize("host", CDN_HOSTS)
def test_no_surface_reaches_a_font_cdn_at_runtime(host):
    """A reviewer with no network must get the whole design, not a fallback."""
    offenders = [
        path.relative_to(ROOT)
        for path in SURFACES
        if path.exists() and host in path.read_text()
    ]
    assert not offenders, (
        f"{host} is referenced by {offenders}. The font ships in the "
        "repository; a CDN adds a third party to every page load and breaks "
        "an offline run."
    )
