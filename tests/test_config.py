"""The loader that decides whether the product has a key at all.

WHY THIS FILE EXISTS. app/config.py had no test of any kind, and it is the
module that put an end to the worst-shaped bug this repository has had. The key
sat in .env, nothing copied .env into os.environ, and so every model-backed path
-- the assistant, the claim proposer, the mail transport -- announced honestly
that it was switched off while a valid key lay on disk two directories away.
Every component reported a limitation it did not have. Nothing looked broken.
No screen threw. There was nothing to see.

That is the failure these tests guard: not a crash, but a product that is quietly
less than it is. A test suite that never loads this module cannot tell the
difference between "the key is absent" and "the key is present and ignored",
which are the same shape from the outside and opposite in the world.

THE RULE MOST LIKELY TO BE BROKEN BY A LATER EDIT is that the real environment
beats the file, and it is the one with the sharpest edge: reverse it and the
developer's personal key in .env overrides the deployment's own key inside a
production container. Half the tests below exist for that single sentence.

NOTHING HERE TOUCHES THE REAL .env OR THE REAL os.environ. Every test passes an
explicit `environ` dictionary or uses monkeypatch, because this repository has a
real .env with a real key in it and a test that read it would both alter the
process it runs in and risk printing a secret into a failure message.
"""

import os
from pathlib import Path

import pytest

import app.config as config
from app.config import ENV_FILE, ROOT, load_env


def _write(tmp_path: Path, body: str, name: str = ".env") -> Path:
    """A .env file, written exactly as given. No trailing newline is added.

    A helper that tidied the text would test the helper. Several cases below
    turn on a character at the very end of the file.
    """
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The founding failure: a key on disk that never reached the process
# ---------------------------------------------------------------------------


def test_the_key_this_module_exists_for_arrives_under_its_real_name(tmp_path):
    """The whole reason app/config.py was written, stated as an assertion.

    Named ANTHROPIC_API_KEY rather than FOO on purpose: a rename in the .env
    format, or a loader that lower-cased names, would pass a generic test and
    still leave the assistant reporting itself unavailable on a live site.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "ANTHROPIC_API_KEY=sk-ant-value\n")

    added = load_env(path, environ=environ)

    assert environ["ANTHROPIC_API_KEY"] == "sk-ant-value"
    assert added == ["ANTHROPIC_API_KEY"]


def test_several_names_arrive_in_file_order(tmp_path):
    """The return value is what a startup line reports. Order makes it readable."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, "ONE=1\nTWO=2\nTHREE=3\n")

    assert load_env(path, environ=environ) == ["ONE", "TWO", "THREE"]
    assert environ == {"ONE": "1", "TWO": "2", "THREE": "3"}


# ---------------------------------------------------------------------------
# The real environment always wins
# ---------------------------------------------------------------------------


def test_the_environment_beats_the_file(tmp_path):
    """Reverse this and a developer's key overrides production's own key.

    A variable already exported, or set in deploy/compose.yml, is a deliberate
    act by whoever started the process. A file on disk is a default. Read it the
    other way round and a container cannot override the configuration it was
    built with -- and the surprise lands in production, where the wrong account
    is billed and the wrong key appears in a provider's logs.
    """
    environ = {"ANTHROPIC_API_KEY": "the-deployment-key"}
    path = _write(tmp_path, "ANTHROPIC_API_KEY=the-developers-key\n")

    added = load_env(path, environ=environ)

    assert environ["ANTHROPIC_API_KEY"] == "the-deployment-key"
    assert added == [], "reported setting a name it must not have touched"


def test_a_variable_exported_as_empty_still_beats_the_file(tmp_path):
    """"Already set" must mean present, not truthy.

    `export ANTHROPIC_API_KEY=` is how an operator switches the model off for one
    container without editing an image. Test presence with `in`, and this holds.
    Test it with `if not environ.get(name)` -- the edit a later reader is most
    likely to make, because it looks tidier -- and the file quietly switches the
    model back on in a container that deliberately disabled it.
    """
    environ = {"ANTHROPIC_API_KEY": ""}
    path = _write(tmp_path, "ANTHROPIC_API_KEY=sk-ant-value\n")

    assert load_env(path, environ=environ) == []
    assert environ["ANTHROPIC_API_KEY"] == ""


def test_it_sets_the_names_the_environment_has_not_claimed_and_skips_the_rest(tmp_path):
    """A mixed file is the normal case, not an edge one.

    The deployment sets one name, the file carries five. What must not happen is
    an all-or-nothing decision in either direction.
    """
    environ = {"SET_ALREADY": "from the environment"}
    path = _write(tmp_path, "SET_ALREADY=from the file\nNOT_SET=from the file\n")

    added = load_env(path, environ=environ)

    assert added == ["NOT_SET"]
    assert environ["SET_ALREADY"] == "from the environment"
    assert environ["NOT_SET"] == "from the file"


# ---------------------------------------------------------------------------
# A missing file is not an error
# ---------------------------------------------------------------------------


def test_a_missing_file_is_not_an_error(tmp_path):
    """A reviewer's fresh clone has no .env, and `make run` must still start.

    Raising here would make the absence of an optional file fatal to the one
    command AI Fund runs first.
    """
    environ: dict[str, str] = {}

    assert load_env(tmp_path / "nothing-here", environ=environ) == []
    assert environ == {}


def test_an_empty_file_sets_nothing(tmp_path):
    environ: dict[str, str] = {}

    assert load_env(_write(tmp_path, ""), environ=environ) == []
    assert environ == {}


# ---------------------------------------------------------------------------
# The file format, one awkward line at a time
# ---------------------------------------------------------------------------


def test_comments_and_blank_lines_are_skipped(tmp_path):
    """.env.example is mostly commentary, and it is the file people copy.

    Reading a comment as a name would put a variable called "# the database"
    into the process and, worse, hide the real one below it in the noise.
    """
    environ: dict[str, str] = {}
    path = _write(
        tmp_path,
        "# the database\n"
        "\n"
        "   \n"
        "    # indented comment, still a comment\n"
        "REAL=value\n",
    )

    assert load_env(path, environ=environ) == ["REAL"]
    assert environ == {"REAL": "value"}


def test_whitespace_around_the_equals_belongs_to_neither_side(tmp_path):
    """A key with a leading space is a key nothing will ever look up.

    os.environ.get("ANTHROPIC_API_KEY") does not find " ANTHROPIC_API_KEY", so
    the value would be loaded, reported as loaded, and never read -- the exact
    silent shape this module was written to end.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "  SPACED   =   the value  \n")

    assert load_env(path, environ=environ) == ["SPACED"]
    assert environ == {"SPACED": "the value"}


def test_a_value_may_contain_an_equals_sign(tmp_path):
    """Base64 pads with '='. Split on the last one and every padded secret breaks.

    A refresh token, a client secret and an API key are all base64 in practice,
    and the corruption is invisible: the value is present, it is the wrong
    length, and the provider answers 401 with nothing on our side to point at.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "GMAIL_REFRESH_TOKEN=a=b==\n")

    assert load_env(path, environ=environ) == ["GMAIL_REFRESH_TOKEN"]
    assert environ["GMAIL_REFRESH_TOKEN"] == "a=b=="


def test_a_line_with_no_equals_is_skipped_rather_than_crashing(tmp_path):
    """One malformed line must not cost the reader the whole file.

    A half-typed line in a hand-edited file is common. Raising on it takes down
    `make run` and takes every other name in the file with it.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "THIS_IS_NOT_A_PAIR\nGOOD=value\n")

    assert load_env(path, environ=environ) == ["GOOD"]
    assert environ == {"GOOD": "value"}


def test_a_line_with_no_name_is_skipped(tmp_path):
    """An empty name would become an environment variable nobody can read back.

    `=value` and `   =value` both give a name of "". Setting it is not harmless:
    os.environ rejects some empty keys outright on some platforms, and where it
    does not, the entry is invisible junk in every child process.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "=orphan\n   =also orphan\nGOOD=value\n")

    assert load_env(path, environ=environ) == ["GOOD"]
    assert environ == {"GOOD": "value"}


def test_the_first_of_two_identical_names_wins(tmp_path):
    """A duplicated name resolves once and stays resolved.

    Hand-edited .env files grow duplicates when somebody pastes a new key above
    an old one. The rule that already governs the environment -- first writer
    wins, nothing is overwritten -- governs the file too, so there is one rule to
    remember rather than two.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "ANTHROPIC_API_KEY=first\nANTHROPIC_API_KEY=second\n")

    assert load_env(path, environ=environ) == ["ANTHROPIC_API_KEY"]
    assert environ["ANTHROPIC_API_KEY"] == "first"


def test_a_file_with_no_trailing_newline_still_yields_its_last_name(tmp_path):
    """People edit .env in editors that do not add one. The last line is usually
    the key they just pasted."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, "FIRST=1\nANTHROPIC_API_KEY=pasted-last")

    assert load_env(path, environ=environ) == ["FIRST", "ANTHROPIC_API_KEY"]
    assert environ["ANTHROPIC_API_KEY"] == "pasted-last"


def test_a_windows_line_ending_is_not_part_of_the_value(tmp_path):
    """A trailing carriage return inside an API key is unreadable in every log.

    The header goes out with a stray \\r, the provider answers 401, and the value
    printed anywhere for debugging looks correct to the eye.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "ANTHROPIC_API_KEY=sk-ant-value\r\nOTHER=2\r\n")

    assert load_env(path, environ=environ) == ["ANTHROPIC_API_KEY", "OTHER"]
    assert environ["ANTHROPIC_API_KEY"] == "sk-ant-value"


# ---------------------------------------------------------------------------
# Quoting: one pair, and nothing cleverer
# ---------------------------------------------------------------------------


def test_matching_surrounding_quotes_are_stripped(tmp_path):
    """Quotes are a habit carried over from shell files. Keep them and the value
    that reaches the provider begins with a double quote."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, 'DOUBLE="value"\nSINGLE=\'value\'\n')

    load_env(path, environ=environ)

    assert environ == {"DOUBLE": "value", "SINGLE": "value"}


def test_only_one_pair_of_quotes_comes_off(tmp_path):
    """Stripping repeatedly would eat a value that legitimately begins and ends
    with a quote character."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, 'NESTED=""quoted""\n')

    load_env(path, environ=environ)

    assert environ["NESTED"] == '"quoted"'


def test_mismatched_quotes_are_left_alone(tmp_path):
    """A value that starts with a quote and ends with an apostrophe is not quoted,
    it is a value with punctuation in it, and rewriting it would be a guess."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, "ODD=\"not closed\nALSO='half\n")

    load_env(path, environ=environ)

    assert environ["ODD"] == '"not closed'
    assert environ["ALSO"] == "'half"


def test_a_single_quote_character_on_its_own_survives(tmp_path):
    """The length check is what stops a one-character value being read as an
    empty pair of quotes and thrown away."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, 'ONE_CHAR="\n')

    load_env(path, environ=environ)

    assert environ["ONE_CHAR"] == '"'


def test_a_quoted_value_keeps_the_spaces_inside_the_quotes(tmp_path):
    """Quoting is the only way to write a value with meaningful trailing space,
    and a display name is the case that has one."""
    environ: dict[str, str] = {}
    path = _write(tmp_path, 'STRATA_MAIL_FROM="Strata Notices "\n')

    load_env(path, environ=environ)

    assert environ["STRATA_MAIL_FROM"] == "Strata Notices "


def test_a_shell_export_prefix_is_not_understood(tmp_path):
    """A limit worth pinning: this reader is not a shell and does not pretend.

    `export ANTHROPIC_API_KEY=...` is copied from shell notes often enough that a
    reader should know what happens. The name does not arrive, and every
    model-backed path goes on announcing itself off with the key sitting in the
    file. The assertion that matters is the first one; the second records that
    the line is currently absorbed rather than rejected, which is why nothing
    warns the person who wrote it.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "export ANTHROPIC_API_KEY=sk-ant-value\n")

    load_env(path, environ=environ)

    assert "ANTHROPIC_API_KEY" not in environ
    assert "export ANTHROPIC_API_KEY" in environ, "the line went somewhere; say where"


def test_an_inline_comment_is_part_of_the_value(tmp_path):
    """Another limit, and the reason .env.example puts every comment on its own line.

    A shell would cut at the '#'. This does not, deliberately -- a '#' is a legal
    character in a password and guessing which one is a comment is how a loader
    starts corrupting secrets.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "STRATA_SMTP_PASSWORD=p#ssword # the relay password\n")

    load_env(path, environ=environ)

    assert environ["STRATA_SMTP_PASSWORD"] == "p#ssword # the relay password"


# ---------------------------------------------------------------------------
# A name left blank in the file
# ---------------------------------------------------------------------------


def test_a_name_left_blank_in_the_file_behaves_as_unset(tmp_path):
    """Fixed 2026-08-04. The strict xfail this carried is deleted, not flipped.

    It was written against a real defect and it worked exactly as intended:
    the fix landed in app/config.py and this went red the same minute. A blank
    value in the file now means UNSET rather than the empty string, so copying
    .env.example verbatim -- which the file itself instructs -- no longer kills
    the process on import. The real environment is untouched: a variable
    EXPORTED as empty is still empty, because that is a deliberate act and a
    blank line in a template is not.

    The reviewer's first five minutes, and the only fatal bug in this repo.

    .env.example line 27 says, directly above `STRATA_DATABASE_URL=`, that unset
    means sqlite:///strata.db in the current directory. The head of the file says
    "Copy to .env and fill in what you need". Do exactly that -- copy it, paste an
    Anthropic key, leave the rest -- and `make run` dies with

        ArgumentError: Could not parse SQLAlchemy URL from given URL string

    because the blank line put "" into os.environ and "" is not absent. The
    Makefile calls `make run` breaking the one bug here that is fatal rather than
    embarrassing, and this is the shortest path to it.
    """
    environ: dict[str, str] = {}
    path = _write(tmp_path, "STRATA_DATABASE_URL=\nANTHROPIC_API_KEY=sk-ant-value\n")

    added = load_env(path, environ=environ)

    assert "STRATA_DATABASE_URL" not in environ, (
        "a blank line must leave the name unset, so the caller's default survives"
    )
    assert added == ["ANTHROPIC_API_KEY"]


# ---------------------------------------------------------------------------
# It must never say what it loaded
# ---------------------------------------------------------------------------


def test_it_prints_nothing_at_all(tmp_path, capsys):
    """A config loader that echoes its work puts an API key in every log.

    This file holds an API key, an OAuth refresh token and a client secret. One
    helpful startup line -- "loaded ANTHROPIC_API_KEY=sk-ant-..." -- ships all
    three to whatever the host aggregates logs into, where they outlive the
    process and the person who wrote the line.
    """
    path = _write(tmp_path, "ANTHROPIC_API_KEY=sk-ant-secret-value\n")

    load_env(path, environ={})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_it_reports_names_and_never_values(tmp_path):
    """The return value is meant to be printed. That is precisely why it must
    carry no secret: the caller is trusted to say what it says."""
    path = _write(tmp_path, "ANTHROPIC_API_KEY=sk-ant-secret-value\nOTHER=another-secret\n")

    added = load_env(path, environ={})

    assert added == ["ANTHROPIC_API_KEY", "OTHER"]
    for name in added:
        assert "secret" not in name
    assert "sk-ant-secret-value" not in "".join(added)


def test_a_failure_message_cannot_carry_a_value(tmp_path, capsys):
    """Nothing raises here, and that is the point being pinned.

    A traceback out of this function would print the line it was reading, and the
    line it was reading is a key. Every malformed shape in one file, and the call
    still returns rather than throwing.
    """
    path = _write(
        tmp_path,
        "NOT_A_PAIR\n"
        "=orphan\n"
        "ANTHROPIC_API_KEY=sk-ant-secret-value\n"
        "   \n"
        "# comment\n"
        "TRAILING=",
    )

    added = load_env(path, environ={})

    assert "ANTHROPIC_API_KEY" in added
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# The defaults: the real file, the real environment
# ---------------------------------------------------------------------------


def test_the_default_file_is_env_at_the_repository_root():
    """The README tells a reviewer to put .env in the root. This is that promise.

    Derived from __file__ rather than the working directory on purpose: `make
    run`, `python scripts/migrate.py` and a systemd unit all start from different
    places, and a relative path would find the file in one of them.
    """
    assert ENV_FILE == ROOT / ".env"
    assert (ROOT / "app" / "config.py").exists(), "ROOT is not the repository root"
    assert (ROOT / "Makefile").exists()


def test_the_real_env_file_is_ignored_by_git():
    """A loader that reads a file people commit is a loader that leaks keys.

    This module's whole value is that secrets live in one file outside version
    control. If .env ever left .gitignore, every test above would still pass and
    the next commit would publish the key.
    """
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert "!.env.example" in ignored, "the example must stay committed"


def test_with_no_arguments_it_reads_the_default_file_into_the_process(tmp_path, monkeypatch):
    """The only call shape production uses: load_env(), no arguments.

    Every caller -- app/main.py and six scripts -- calls it bare. A signature
    change that kept the two-argument form working and broke this one would pass
    every other test in this file and switch the product off everywhere.

    monkeypatch, not assignment: it restores ENV_FILE and removes the name from
    the real os.environ afterwards, so this test cannot leak into another.
    """
    path = _write(tmp_path, "STRATA_TEST_ONLY_MARKER=arrived\n")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.delenv("STRATA_TEST_ONLY_MARKER", raising=False)

    added = load_env()

    assert added == ["STRATA_TEST_ONLY_MARKER"]
    assert os.environ["STRATA_TEST_ONLY_MARKER"] == "arrived"


def test_the_process_environment_beats_the_default_file_too(tmp_path, monkeypatch):
    """The rule holds on the path production actually takes, not only on the
    injectable one. A test that only ever passes its own dictionary proves the
    rule about dictionaries."""
    path = _write(tmp_path, "STRATA_TEST_ONLY_MARKER=from the file\n")
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.setenv("STRATA_TEST_ONLY_MARKER", "from the environment")

    assert load_env() == []
    assert os.environ["STRATA_TEST_ONLY_MARKER"] == "from the environment"
