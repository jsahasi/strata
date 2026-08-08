"""The tool that writes every ratio this project publishes.

Ratios were estimated by hand once and the comment drifted from the colour.
tests/test_glass_contrast.py catches the drift; this script is what makes
fixing it a regeneration rather than an arithmetic exercise.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from contrast_report import pinned_table, read_schemes  # noqa: E402

CSS = """
:root { --bg: #ffffff; --ink: #000000; --ink-2: #414d68; --ink-3: #6b7791; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #000000; --ink: #ffffff; --ink-2: #adb7cd; --ink-3: #818da8; }
}
"""


def test_it_reads_a_token_out_of_the_light_block():
    assert read_schemes(CSS)["light"]["--bg"] == "#ffffff"


def test_it_reads_the_dark_block_separately():
    assert read_schemes(CSS)["dark"]["--bg"] == "#000000"


def test_black_on_white_is_the_known_21_to_1():
    table = pinned_table(read_schemes(CSS), [("light", "--ink", "--bg", "test")])
    assert '("light", "--ink", "--bg", 21.00, "test")' in table


def test_a_missing_token_raises_rather_than_defaulting():
    """A token that silently reads as None makes every ratio wrong and quiet."""
    try:
        pinned_table(read_schemes(CSS), [("light", "--nope", "--bg", "x")])
    except KeyError:
        return
    raise AssertionError("a missing token must raise, not default")
