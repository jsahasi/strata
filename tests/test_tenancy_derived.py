"""Tenant isolation, derived from the code rather than listed by hand.

WHY THIS FILE EXISTS. An audit deleted eighteen tenant filters from app/, one at
a time, and ran the suite after each. The two isolation files caught FOUR -- the
four functions they name by hand. Everything else that went red went red in some
per-feature test that happened to read a row and happened to check whose it was,
and one deletion turned nothing red at all:
app/state/queries.py::passage_counts_by_version. That is 1,984 tests answering
"is the guard there?" with a shrug.

A LIST A PERSON MAINTAINS CANNOT CATCH THE FUNCTION THAT PERSON FORGOT. This
repository has already learnt that once, in tests/test_app_wiring.py: the screen
list was hand-written, so it did not notice that two modules and nine routes were
mounted nowhere. Deriving the answer from app.routes caught three unmounted
routers on the first run. The same move, applied to tenancy, is these three
guards:

  THE READ SWEEP -- inspect.signature over app.state.*, every public function
  taking company_id, called with a foreign scope, asserting that nothing of the
  other tenant's comes back.

  THE AST PASS -- every function under app/state/ that takes company_id must
  route it through the scope guard, and every function anywhere under app/ that
  takes one and opens a query must name it in a filter or a where clause. This
  is the one that reads the source rather than the answer, so it catches a
  filter whose deletion no test can currently feel.

  THE HTTP SWEEP -- app.routes on the ASSEMBLED application, signed in as one
  company, asserting that no route hands back another's rows.

WHAT EACH ONE COSTS. The read sweep and the HTTP sweep are slow: they seed the
corpus and then call every scoped function or open every screen. That is the
right cost for the control this product is sold on, and it is paid once per run
rather than once per feature.

EVERY EXEMPTION IS NAMED WITH A REASON. A skip list of counts, or a silent
filter, would be the hand-written list again wearing a different hat. If a
function or a route does not belong in a sweep, it is written below with the
sentence that says why, so the exemption is a decision somebody made rather than
a gap nobody saw.
"""

import ast
import dataclasses
import importlib
import inspect
import pathlib
import pkgutil
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.state as state_package
from app.main import app
from app.seed import (
    DEMO_PASSWORD,
    demo_account_list,
    ensure_accounts,
    ensure_demo_actions,
    load,
)
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles
from app.state.models import Base, ROLE_ADMIN, ROLE_ANALYST
from app.web import deps

COMPANY = "MEP"
RIVAL = "RIVAL"

RIVAL_EMAIL = "sweep-analyst@rival.example"
RIVAL_PASSWORD = "rival-sweep-password"

APP_DIR = pathlib.Path(state_package.__file__).resolve().parent.parent
STATE_DIR = APP_DIR / "state"
SCOPE = "company_id"


# ===========================================================================
# (a) THE DERIVED READ SWEEP
#
# Every public function in app.state.* that takes a company_id, called as a
# company that owns nothing, with the OTHER company's ids in its hands. The
# answer must be empty, None, or a refusal. It must never be a row belonging to
# the company that did not ask.
#
# WHY THE OTHER COMPANY'S IDS. A sweep that passed made-up ids would prove only
# that the database has no row called "x". Ids in this product are unique across
# tenants and the corpus supplies them -- a version id is printed on a filing,
# an escalation id appears in an email -- so the interesting call is the one
# where the id is real and the scope is wrong. That is the shape that let
# app/state/routing.py::ensure_obligation hand back somebody else's obligation.
# ===========================================================================


#: Functions the sweep does not call, each with the reason it does not.
#:
#: NAMED, NEVER COUNTED, AND NEVER A PATTERN. "everything ending in _admin" would
#: quietly grow to cover a function nobody argued about. Adding a line here is
#: adding a sentence somebody has to defend at review.
SWEEP_EXEMPT = {
    # The guard itself and the chokepoint it feeds. tests/test_isolation.py is
    # where these two are tested, value by value, and a sweep that also called
    # them would be asserting the same thing in a weaker way.
    ("queries", "row_for_company"): (
        "the chokepoint under test elsewhere; it takes a model class rather "
        "than data and tests/test_isolation.py exercises it directly"
    ),
    # A destructive sweep over the retention module would delete the corpus the
    # rest of the sweep is probing, and the savepoint below cannot protect the
    # calls that run after it inside the same probe.
    ("retention", "purge_all"): (
        "purges every table for a company; tests/test_retention.py drives it "
        "with its own fixtures rather than against the shared corpus"
    ),
}


def _populate(session) -> None:
    """Fill the tables app/seed.py leaves empty, so the sweep has rows to probe.

    A FRESH SEED LEAVES SIXTEEN TABLES EMPTY, and the sweep is only as good as
    what it can see: a probe against a module with no rows returns [] for the
    right reason and the wrong one at once, and cannot tell them apart. Four
    modules -- permissions, rollback, sources and workflow -- had no live probe
    at all until this ran, which is the same "an empty answer proved the guard"
    mistake tests/test_isolation.py was rewritten to stop.

    Written through the product's own writers rather than by building rows, so
    what the sweep reads back is shaped the way the product shapes it. Each part
    is wrapped, because a writer that refuses on a corpus somebody has since
    changed must not take the whole sweep down with it -- the control above
    reports the loss instead.

    scripts/seed_demo_gaps.py, scripts/seed_roles.py and scripts/seed_route.py do
    the same job for a live demo, at more length and with more of an eye to what
    it looks like. This is the short version, and it exists here rather than as
    an import because a test that depends on a script in scripts/ depends on a
    file nobody runs in CI.

    THE CLOCK IS THE REAL ONE HERE, ON PURPOSE. Three writers below take a
    moment -- create_share_link twice and provision_login once -- and all three
    are listed in ALLOWED in tests/test_clock_pinned.py rather than pinned.

    What this function builds is a corpus, and the corpus is read back two ways
    that no test can pin: the function sweep calls each scoped reader with
    arguments built from its signature, and the HTTP sweep signs in over the
    real stack and fetches every screen. Both run on the real now. A share link
    or an invitation written at a fixed 2026-08-04 and read by those would be a
    live row today, an expired one next week, and the difference would show up
    as screens quietly losing rows -- which is precisely what
    test_the_signed_in_owner_still_sees_their_own_rows asserts against.

    Agreement between the write and the read is what stops a test rotting.
    Pinning one end of a pair whose other end cannot be pinned breaks that
    agreement instead of enforcing it.
    """
    from app.state import audit, feedback, invites, models, permissions
    from app.state import rollback, routing, sharing, sources
    from app.state import workflow as wf

    actor = "system:tenancy-sweep"
    # THE ADMIN, NOT WHOEVER SORTS FIRST. Composing a role and registering a
    # source are both behind user.manage, and an analyst is refused -- which is
    # the permission model working and would leave four modules with nothing for
    # the sweep to look at.
    admin_email = next(
        account.email
        for account in demo_account_list()
        if account.role == ROLE_ADMIN
    )
    user = (
        session.query(models.User)
        .filter(models.User.company_id == COMPANY)
        .filter(models.User.email == admin_email)
        .one_or_none()
    )
    change = (
        session.query(models.Change)
        .filter(models.Change.company_id == COMPANY)
        .order_by(models.Change.id)
        .first()
    )
    escalation = (
        session.query(models.Escalation)
        .filter(models.Escalation.company_id == COMPANY)
        .order_by(models.Escalation.id)
        .first()
    )
    claim = (
        session.query(models.Claim)
        .filter(models.Claim.company_id == COMPANY)
        .order_by(models.Claim.id)
        .first()
    )
    assert user is not None and change is not None and escalation is not None

    steps = [
        {
            "id": "SWEEP-STP-1",
            "label": "Obligation owner reads it",
            "assignee_rule": "obligation_owner",
            "approval_hours": 24,
            "on_timeout": "remind",
            "remind_every_hours": 4,
            "escalate_to": None,
            "x": 40,
            "y": 150,
        },
        {
            "id": "SWEEP-STP-2",
            "label": "Officer signs the filing",
            "assignee_rule": f"role:{ROLE_ADMIN}",
            "approval_hours": 48,
            "on_timeout": "escalate",
            "escalate_to": f"role:{ROLE_ADMIN}",
            "remind_every_hours": None,
            "x": 250,
            "y": 150,
        },
    ]
    graph = {
        "workflow_id": "SWEEP-WF-1",
        "name": "Sweep route",
        "steps": steps,
        "edges": [{"from": "SWEEP-STP-1", "to": "SWEEP-STP-2"}],
    }

    parts = (
        lambda: permissions.create_role(
            session,
            COMPANY,
            actor=user.id,
            name="Sweep Reviewer",
            codes=("claim.read",),
            reason="a row for the tenancy sweep to probe",
        ),
        lambda: routing.ensure_obligation(
            session,
            COMPANY,
            obligation_id="SWEEP-OBL-1",
            title="Sweep obligation",
            actor=actor,
            owner_user_id=user.id,
        ),
        lambda: routing.map_change_to_obligation(
            session,
            COMPANY,
            change_id=change.id,
            obligation_id="SWEEP-OBL-1",
            mapped_by=actor,
        ),
        lambda: sources.register_source(
            session,
            company_id=COMPANY,
            user_id=user.id,
            name="Sweep source",
            kind="public_docket",
            config={"endpoint": "https://example.invalid/docket"},
        ),
        lambda: sharing.create_share_link(
            session,
            company_id=COMPANY,
            user_id=user.id,
            subject_type=sharing.SHARE_SUBJECT_CHANGE,
            subject_id=change.id,
        ),
        lambda: wf.create_workflow(
            session, COMPANY, workflow_id="SWEEP-WF-1", name="Sweep route", actor=actor
        ),
        lambda: wf.save_graph(session, COMPANY, "SWEEP-WF-1", graph, actor=actor),
        lambda: wf.activate_workflow(session, COMPANY, "SWEEP-WF-1", actor=actor),
        lambda: wf.start_run(
            session, COMPANY, escalation_id=escalation.id, actor=actor
        ),
        lambda: invites.provision_login(
            session,
            company_id=COMPANY,
            actor=user.id,
            email="sweep-invitee@mep.example",
            display_name="A sweep invitee",
            role=ROLE_ADMIN,
        ),
    )
    for part in parts:
        savepoint = session.begin_nested()
        try:
            part()
            savepoint.commit()
        except Exception:
            savepoint.rollback()

    # The chat transcript, and the feedback that hangs off it. Built as rows
    # because app/web/views/chat.py is the only writer and it wants a request.
    session.add(
        models.ChatSession(id="SWEEP-CHAT-1", company_id=COMPANY, user_id=user.id)
    )
    session.add(
        models.ChatMessage(
            id="SWEEP-MSG-1",
            session_id="SWEEP-CHAT-1",
            company_id=COMPANY,
            ordinal=1,
            role="person",
            text="what changed in the load forecast?",
        )
    )
    session.flush()

    for part in (
        lambda: feedback.record_thumb(
            session,
            company_id=COMPANY,
            user_id=user.id,
            message_id="SWEEP-MSG-1",
            rating="down",
        ),
        lambda: feedback.record_bug_report(
            session,
            company_id=COMPANY,
            user_id=user.id,
            title="Sweep bug report",
            detail="a row for the tenancy sweep to probe",
        ),
    ):
        savepoint = session.begin_nested()
        try:
            part()
            savepoint.commit()
        except Exception:
            savepoint.rollback()

    report = (
        session.query(models.Feedback)
        .filter(models.Feedback.company_id == COMPANY)
        .order_by(models.Feedback.id)
        .first()
    )
    if report is not None:
        savepoint = session.begin_nested()
        try:
            feedback.triage(
                session,
                company_id=COMPANY,
                feedback_id=report.id,
                actor=actor,
                actor_user_id=user.id,
                title="Sweep improvement",
            )
            savepoint.commit()
        except Exception:
            savepoint.rollback()

    if claim is not None:
        savepoint = session.begin_nested()
        try:
            sharing.create_share_link(
                session,
                company_id=COMPANY,
                user_id=user.id,
                subject_type=sharing.SHARE_SUBJECT_CLAIM,
                subject_id=claim.id,
            )
            savepoint.commit()
        except Exception:
            savepoint.rollback()

    # A decision, and the decision taken back. Without both, every read in
    # app/state/rollback.py answers None on a corpus where nothing was ever
    # reverted -- and None is the same answer a deleted scope filter gives, which
    # is the confusion this whole file exists to remove.
    savepoint = session.begin_nested()
    try:
        escalation.resolved_at = datetime.now(timezone.utc)
        escalation.resolved_by = user.display_name
        session.flush()
        closed = audit.record_event(
            session,
            company_id=COMPANY,
            actor=user.display_name,
            action=audit.ACTION_ESCALATION_APPROVED,
            subject_type="escalation",
            subject_id=escalation.id,
            reason="a decision for the tenancy sweep to take back",
            actor_kind=audit.ACTOR_USER,
            actor_user_id=user.id,
        )
        rollback.revert_event(
            session,
            company_id=COMPANY,
            event_id=closed.id,
            actor=user.display_name,
            actor_kind=audit.ACTOR_USER,
            actor_user_id=user.id,
            reason="taking it back, so the sweep has a reversal to read",
        )
        savepoint.commit()
    except Exception:
        savepoint.rollback()


def _model_ids(session) -> dict[type, list[str]]:
    """One id per model, for each company that holds rows of it."""
    found: dict[type, dict[str, str]] = {}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if not hasattr(model, "id") or not hasattr(model, "company_id"):
            continue
        for company in (COMPANY, RIVAL):
            row = (
                session.query(model)
                .filter(model.company_id == company)
                .order_by(model.id)
                .first()
            )
            if row is not None:
                found.setdefault(model, {})[company] = row.id
    return found


#: Parameter name to the model whose id it wants. Derived where it can be --
#: every name below is the id of a row -- and written out because Python
#: signatures carry no such relation.
def _argument_values(session) -> tuple[dict[str, object], set[str]]:
    """Real MEP values for the parameters the scoped functions ask for.

    Returns the map and the set of strings that identify MEP and nobody else.
    Anything in that second set coming back out of a RIVAL-scoped call is the
    leak this sweep exists to find.
    """
    from app.state import models as m

    by_model = _model_ids(session)

    def mep(model) -> str | None:
        return by_model.get(model, {}).get(COMPANY)

    values: dict[str, object] = {
        "version_id": mep(m.DocumentVersion),
        "change_id": mep(m.Change),
        "claim_id": mep(m.Claim),
        "escalation_id": mep(m.Escalation),
        "project_id": mep(m.Project),
        "proceeding_id": mep(m.Proceeding),
        "obligation_id": mep(m.Obligation),
        "workflow_id": mep(m.ApprovalWorkflow),
        "run_id": mep(m.WorkflowRun),
        # A WorkPlanStep, not a WorkflowStep. Two tables spell the parameter the
        # same way and only one of them is reached by a function taking a
        # company_id -- app/state/projects.py::set_step_state. WorkflowStep
        # carries no company_id of its own; its tenancy is on the workflow.
        "step_id": mep(m.WorkPlanStep),
        "plan_id": mep(m.WorkPlan),
        "thread_id": mep(m.ResearchThread),
        "message_id": mep(m.ChatMessage),
        "chat_session_id": mep(m.ChatSession),
        "feedback_id": mep(m.Feedback),
        "item_id": mep(m.ImprovementItem),
        "invitation_id": mep(m.Invitation),
        "source_id": mep(m.SourceRegistration),
        "share_id": mep(m.ShareLink),
        "directive_id": mep(m.SteerDirective),
        "role_id": mep(m.Role),
        # The seeded demonstration's proposal. app/state/actions.py::
        # record_decision takes an id and re-reads the row precisely so the
        # scope is enforced there rather than trusted from a caller, which is
        # what makes it a probe worth making.
        "action_id": mep(m.ProposedAction),
    }

    user = (
        session.query(m.User)
        .filter(m.User.company_id == COMPANY)
        .order_by(m.User.id)
        .first()
    )
    version = session.get(m.DocumentVersion, values["version_id"])
    # THE EVENT SOMEBODY TOOK BACK, not simply the first one. Every read in
    # app/state/rollback.py answers None about an event with no reversal against
    # it, and None is the answer a deleted scope filter gives too. Probing the
    # one event the corpus has a reversal for is what makes those reads say
    # something the sweep can judge.
    reversal = (
        session.query(m.AuditEvent)
        .filter(m.AuditEvent.company_id == COMPANY)
        .filter(m.AuditEvent.reverts_event_id.isnot(None))
        .order_by(m.AuditEvent.id)
        .first()
    )
    event = (
        session.query(m.AuditEvent)
        .filter(m.AuditEvent.company_id == COMPANY)
        .order_by(m.AuditEvent.id)
        .first()
    )

    for name in (
        "user_id",
        "actor_user_id",
        "owner_user_id",
        "approver_user_id",
        "revoked_by_user_id",
        "invited_by_user_id",
        "acting_user_id",
        "opened_by",
        "owner",
        "mapped_by",
    ):
        values[name] = None if user is None else user.id

    values.update(
        {
            "event_id": (
                reversal.reverts_event_id
                if reversal is not None
                else (None if event is None else event.id)
            ),
            "docket": None if version is None else version.docket,
            "email": None if user is None else user.email,
            "actor": "system:tenancy-sweep",
            "reason": "the derived tenancy sweep",
            "name": "Legal Reviewer",
            "role_name": "analyst",
            "role": "analyst",
            "source_role": "analyst",
            "display_name": "A sweep probe",
            "title": "a sweep probe",
            "body": "a sweep probe",
            "new_body": "a sweep probe",
            "note": "a sweep probe",
            "question": "a sweep probe",
            "instruction": "a sweep probe",
            "description": "a sweep probe",
            "resolution": "a sweep probe",
            "rationale": "a sweep probe",
            # A decision has to be one thing or the other, and the sweep only
            # asks whether the call crosses a tenant boundary. Approving is the
            # side with the gate in front of it, so it is the side probed.
            "approved": True,
            "password": "a-sweep-probe-password-1",
            "query": "load forecast",
            "expression": '"load"',
            "code": "claim.read",
            "codes": ("claim.read",),
            "subject_type": "escalation",
            "subject_id": values["escalation_id"],
            "action": "escalation.approved",
            "actor_kind": "system",
            "author_kind": "user",
            "kind": "question",
            "status": "open",
            "state": "open",
            "outcome": "approve",
            "result": "ok",
            "rule": "manual",
            "cadence": "weekly",
            "jurisdiction": "MPUC",
            "scheme": "v1",
            "rating": "down",
            "limit": 5,
            "enabled": True,
            "payload": {},
            "model": "claude-test",
            "row_id": values["version_id"],
            "moment": datetime.now(timezone.utc),
            "now": datetime.now(timezone.utc),
            "ran_at": datetime.now(timezone.utc),
            "next_run_at": datetime.now(timezone.utc) + timedelta(days=1),
        }
    )

    # What "MEP's" looks like as a bare string. Every id above, plus the two
    # pieces of text only this tenant's corpus carries.
    sentinels = {
        str(value)
        for name, value in values.items()
        if value and name.endswith(("_id", "docket", "email"))
    }
    sentinels.add(COMPANY)
    return values, sentinels


def _call_arguments(fn, company: str, values: dict[str, object], session):
    """Build a call for one scoped function, or None if a value is missing.

    A missing value is a probe that cannot be made rather than one that passed.
    The count of them is asserted on below, so the sweep cannot quietly shrink
    to nothing.
    """
    arguments: dict[str, object] = {}
    for name, parameter in inspect.signature(fn).parameters.items():
        if name == "session":
            arguments[name] = session
            continue
        if name == SCOPE:
            arguments[name] = company
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if name not in values or values[name] is None:
            return None
        arguments[name] = values[name]
    return arguments


def _scoped_functions():
    """Every public function in app.state.* that takes a company_id.

    Derived by walking the package and reading signatures, so a module added
    next month is swept because it exists rather than because somebody added a
    line here.
    """
    for info in pkgutil.iter_modules(state_package.__path__):
        module = importlib.import_module(f"app.state.{info.name}")
        for name, fn in vars(module).items():
            if not inspect.isfunction(fn) or fn.__module__ != module.__name__:
                continue
            if name.startswith("_"):
                continue
            if SCOPE not in inspect.signature(fn).parameters:
                continue
            if (info.name, name) in SWEEP_EXEMPT:
                continue
            yield info.name, name, fn


def _leaks(value, sentinels: set[str], seen=None) -> list[str]:
    """Anything in this answer that belongs to the company that did not ask.

    Walks rows, lists, dicts, dataclasses and named tuples. An ORM row is judged
    by its own company_id; a bare string by whether it is one of the ids or
    labels only MEP holds. Both matter: passage_counts_by_version returns a dict
    keyed by version id and carries no rows at all, which is exactly why the
    deleted filter in it turned nothing red.
    """
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))

    scope = getattr(value, "company_id", None)
    if isinstance(scope, str) and scope in sentinels and scope != RIVAL:
        return [f"{type(value).__name__} scoped to {scope}"]

    if isinstance(value, str):
        return [f"the string {value!r}"] if value in sentinels else []
    if isinstance(value, (bytes, int, float, bool)) or value is None:
        return []
    if isinstance(value, dict):
        found = []
        for key, item in value.items():
            found += _leaks(key, sentinels, seen) + _leaks(item, sentinels, seen)
        return found
    if isinstance(value, (list, tuple, set, frozenset)):
        found = []
        for item in value:
            found += _leaks(item, sentinels, seen)
        return found
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        found = []
        for field in dataclasses.fields(value):
            found += _leaks(getattr(value, field.name), sentinels, seen)
        return found
    if hasattr(value, "_fields"):  # a NamedTuple
        found = []
        for field in value._fields:
            found += _leaks(getattr(value, field), sentinels, seen)
        return found
    return []


@pytest.fixture(scope="module")
def swept():
    """Seed both tenants once, then probe every scoped function twice.

    Once as MEP, to learn which probes can see anything at all -- a sweep whose
    calls all return empty proves nothing, and this is how that is detected
    rather than assumed. Once as RIVAL, which is the assertion.

    Every call runs inside a SAVEPOINT that is rolled back afterwards. The sweep
    reaches writes as well as reads, because a write is a scoped operation too
    and refusing to sweep it would leave exactly the half that changes data
    untested. Nothing a probe writes survives its own probe.
    """
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        # The proposed actions too, so app/state/actions.py has rows to probe.
        # Without them the sweep cannot build a call for record_decision, and a
        # probe it cannot build is a probe that silently stopped running.
        ensure_demo_actions(session)
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        _populate(session)

    results = []
    with session_scope() as outer:
        values, sentinels = _argument_values(outer)
        for module_name, fn_name, fn in _scoped_functions():
            record = {
                "module": module_name,
                "name": fn_name,
                "callable": False,
                "live": False,
                "leaked": [],
            }
            for company, key in ((COMPANY, "live"), (RIVAL, "leaked")):
                arguments = _call_arguments(fn, company, values, outer)
                if arguments is None:
                    break
                record["callable"] = True
                savepoint = outer.begin_nested()
                try:
                    answer = fn(**arguments)
                    found = _leaks(answer, sentinels)
                    record[key] = bool(found) if key == "live" else found
                except Exception:
                    # A refusal is the right answer to a foreign scope, and an
                    # argument this sweep guessed wrong is not a tenant failure.
                    # Either way nothing crossed.
                    pass
                finally:
                    if savepoint.is_active:
                        savepoint.rollback()
            results.append(record)
        outer.rollback()
    return results


def test_no_scoped_function_hands_a_row_to_the_company_that_did_not_ask(swept):
    """The sweep itself. One line per function that answered with MEP's data."""
    leaked = {
        f"{r['module']}.{r['name']}": r["leaked"] for r in swept if r["leaked"]
    }
    assert not leaked, (
        "these were called with company_id='RIVAL', holding MEP's ids, and "
        f"answered with MEP's data: {leaked}"
    )


def test_the_read_sweep_actually_reaches_the_functions_it_claims_to(swept):
    """The control, and it is not decoration.

    A sweep where every probe returns [] passes the test above and proves
    nothing -- it is the same mistake tests/test_isolation.py was rewritten for,
    where an empty list was accepted as evidence of a guard. So the same calls
    are made as MEP first, and at least one function in every module that has
    scoped reads must come back holding MEP's rows. A module whose probes all go
    quiet is a module this sweep is no longer testing, and it says so here rather
    than passing.
    """
    modules = {r["module"] for r in swept}
    live = {r["module"] for r in swept if r["live"]}
    silent = sorted(modules - live)
    assert not silent, (
        f"no probe in {silent} saw a single MEP row, so the sweep is not "
        "testing those modules. Either the corpus no longer seeds them or the "
        "argument map above has gone stale."
    )


def test_every_scoped_function_can_be_called_by_the_sweep(swept):
    """A function the sweep cannot build arguments for is a function it skips.

    Skipping is not passing. This fails with the names, so a new required
    parameter lands as a red test rather than as a probe that silently stopped
    running.
    """
    uncallable = sorted(
        f"{r['module']}.{r['name']}" for r in swept if not r["callable"]
    )
    assert not uncallable, (
        f"the sweep could not build a call for {uncallable}. Add the parameter "
        "to _argument_values above, or name the function in SWEEP_EXEMPT with "
        "the reason it does not belong in the sweep."
    )


# ===========================================================================
# (b) THE AST PASS
#
# The other two guards read answers. This one reads the source, and that is the
# whole point: a tenant filter can be correct, load-bearing, and covered by no
# test at all, because every caller happens to discard the rows it would let
# through. app/state/queries.py::passage_counts_by_version was exactly that --
# deleting its filter left the suite byte for byte identical, 1,880 passed, not
# one test moving. Its only caller reads the dict with .get(version_id, 0)
# against its own version list, so the foreign keys fall on the floor. The day a
# second caller iterates the dict instead, that filter is the only thing between
# one tenant and another's page counts, and nothing would have noticed it go.
#
# TWO RULES, AND THEY CATCH THE TWO DEFECTS SEPARATELY.
#
#   THE GUARD RULE. A public function taking company_id must pass it to
#   _require_scope -- directly, or through a function it hands the scope to.
#   app/state/audit.py failed this on all four of its entry points: it wrote
#   `if not company_id:` instead, which accepts "   ", " MEP" and "MEP%", so
#   verify_chain answered True over a scope it could not parse.
#
#   THE FILTER RULE. EVERY QUERY, not every function, must name company_id in a
#   filter, a where, or a bound parameter. Per function was the first version of
#   this rule and it was too weak by half: app/state/routing.py's
#   map_change_to_obligation opens two queries and app/state/workflow.py's
#   advance_overdue opens several, so scoping any one of them satisfied a rule
#   that asked about the function. Deleting the tenant filter off the FIRST query
#   in map_change_to_obligation left the function still mentioning company_id in
#   the second, and the rule stayed green over an unscoped read of the changes
#   table. Asking the question once per query took the rule from catching 78 of
#   176 single-line deletions to catching 101, at the cost of eleven exemptions
#   across eight functions -- each named below with the sentence that argues it.
#
# WALKED RATHER THAN GREPPED. A grep for "company_id ==" counts the string in a
# docstring, in a comment, and in a line somebody commented out, and misses
# filter_by(company_id=...) and a bound parameter in raw SQL. The AST knows the
# difference between a call and a sentence about a call.
# ===========================================================================

#: The two calls that ARE the check. Anything that reaches one of them has been
#: scoped; row_for_company calls _require_scope itself.
GUARD_CALLS = {"_require_scope", "row_for_company"}

#: What narrowing a query looks like. `having` is here for completeness rather
#: than because anything uses it today -- the day something does, it is a place
#: a scope can be dropped.
NARROWING_CALLS = {"filter", "filter_by", "where", "having"}

#: Functions exempt from the GUARD rule, each with the reason. Empty, and it
#: should stay that way: every public function under app/state that takes a
#: company_id routes it through _require_scope today, so a name appearing here is
#: a claim that one of them should not.
AST_EXEMPT: dict[tuple[str, str], str] = {}

#: Functions holding a query that names no company_id, each with the argument for
#: why that query is right as it stands. EIGHT functions and eleven queries, and
#: every one of them is the same shape: the row the query is keyed on was already
#: resolved under the scope, or the table it reads is vocabulary rather than
#: tenant data.
#:
#: EACH ENTRY PINS A COUNT, AND THE COUNT IS THE WHOLE POINT. Keyed by function
#: alone, an exemption covers every unscoped query that function will ever hold:
#: the first version of this list was, and deleting the tenant filter off
#: map_change_to_obligation's FIRST query -- a genuine unscoped read of the
#: changes table -- was waved through by an exemption written for its SECOND. So
#: the number of unscoped queries is fixed here. Add one and this list has to be
#: edited, which is the sentence somebody has to write to get past the rule.
#: A line number would be tighter still and would go stale on the next edit
#: above it, failing for a reason that has nothing to do with tenancy -- which is
#: the kind of false alarm that gets a guard deleted.
AST_QUERY_EXEMPT: dict[tuple[str, str], tuple[int, str]] = {
    ("app.state.identity", "role_for_company"): (1, 
        "the second query looks for a SYSTEM role, whose company_id IS NULL by "
        "definition; a tenant filter there would find nothing, ever"
    ),
    ("app.state.identity", "permissions_for_role"): (1, 
        "role_permissions is vocabulary, not tenant data -- tests/test_isolation.py"
        "::TENANCY_BY_PARENT names it -- and the role it reads was resolved "
        "scoped one line above"
    ),
    ("app.state.permissions", "_editable_role"): (1, 
        "same as role_for_company: the fallback looks for a system role under "
        "company_id IS NULL, which is what makes printing its name safe"
    ),
    ("app.state.permissions", "edit_role"): (
        1,
        "the RolePermission rows it clears are keyed on a role _editable_role "
        "already resolved under the scope, and role_permissions carries no "
        "company_id -- tests/test_isolation.py::TENANCY_BY_PARENT argues why",
    ),
    ("app.state.permissions", "delete_role"): (3,
        "counted by role_id alone ON PURPOSE, and the function says so at "
        "length: it asks whether ANY row anywhere points at the role about to be "
        "destroyed, and a company filter would hide exactly the rows that should "
        "stop the delete"
    ),
    ("app.state.routing", "map_change_to_obligation"): (1, 
        "the ChangeObligation read is keyed on a change and an obligation that "
        "the two queries above it already resolved under the scope, and both "
        "refused before it runs"
    ),
    ("app.state.workflow", "save_graph"): (2, 
        "the two DELETEs are keyed on a workflow row row_for_company already "
        "handed over; workflow_steps and workflow_edges carry no company_id of "
        "their own and tests/test_isolation.py::TENANCY_BY_PARENT says why"
    ),
    ("app.web.views.admin", "_next_workflow_id"): (1, 
        "a COUNT asking whether a candidate id is taken ANYWHERE, which is the "
        "question -- ids are unique across tenants, and narrowing it to this "
        "company would mint an id another company already holds"
    ),
}


def _mentions_scope(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == SCOPE for n in ast.walk(node))


def _calls_named(node: ast.AST):
    """Every call inside this subtree, as (name, call)."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Attribute):
            yield func.attr, inner
        elif isinstance(func, ast.Name):
            yield func.id, inner


def _opens_a_query(node: ast.AST) -> bool:
    """session.query(...) or session.execute(...) somewhere in this subtree."""
    return any(
        name in ("query", "execute") and isinstance(call.func, ast.Attribute)
        for name, call in _calls_named(node)
    )


def _narrows_on_scope(node: ast.AST) -> bool:
    """A filter, filter_by, where or having in this subtree naming company_id."""
    for name, call in _calls_named(node):
        if name not in NARROWING_CALLS:
            continue
        if any(_mentions_scope(argument) for argument in call.args):
            return True
        for keyword in call.keywords:
            if keyword.arg == SCOPE or _mentions_scope(keyword.value):
                return True
    return False


def _reads_raw_sql(node: ast.AST) -> bool:
    return any(name == "text" for name, _ in _calls_named(node))


def _hands_scope_on(node: ast.AST) -> set[str]:
    """Names this subtree passes company_id to, narrowing calls excepted."""
    given = set()
    for name, call in _calls_named(node):
        if name in NARROWING_CALLS:
            continue
        passed = any(
            isinstance(argument, ast.Name) and argument.id == SCOPE
            for argument in call.args
        ) or any(
            isinstance(keyword.value, ast.Name) and keyword.value.id == SCOPE
            for keyword in call.keywords
        )
        if passed:
            given.add(name)
    return given


def _query_sites(node: ast.AST):
    """One subtree per place a query could be written, with its line.

    A plain statement is one site. A COMPOUND statement is not: its body holds
    other statements, each their own site, and folding them into the block would
    let a scoped query anywhere inside a loop vouch for an unscoped one beside
    it. What the block contributes is its HEADER -- the iterable of a `for`, the
    test of an `if` or a `while`, the subject of a `with` -- and that is yielded
    on its own.

    The header is not a detail. Two of app/state/invites.py's tenant filters live
    in `for row in (session.query(...).filter(...))`, and an earlier version of
    this walk skipped compound statements outright, so deleting either of those
    two filters turned nothing red. The gap was found by deleting all 176 of them
    one at a time and reading which ones this file failed to notice.
    """
    for statement in ast.walk(node):
        if not isinstance(statement, ast.stmt):
            continue
        if isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        blocks = []
        for field in ("body", "orelse", "finalbody", "handlers"):
            blocks += getattr(statement, field, []) or []
        if not blocks:
            yield statement.lineno, statement
            continue
        for field in ("iter", "test"):
            header = getattr(statement, field, None)
            if header is not None:
                yield statement.lineno, header
        for item in getattr(statement, "items", ()) or ():
            yield statement.lineno, item.context_expr


class _Facts:
    """What one function does with the company_id it was handed."""

    def __init__(self, module: str, node: ast.FunctionDef) -> None:
        self.module = module
        self.name = node.name
        self.line = node.lineno
        self.public = not node.name.startswith("_")
        self.guards = any(name in GUARD_CALLS for name, _ in _calls_named(node))
        self.hands_scope_to = _hands_scope_on(node) - GUARD_CALLS

        # THE SCOPE AS A BOUND PARAMETER, judged over the whole function rather
        # than one statement. app/state/queries.py::passage_index_hits builds its
        # parameter dict in one statement and executes the text in another, which
        # is the readable way to write it; requiring both in one line would be a
        # rule about formatting.
        self.binds_scope = any(
            isinstance(inner, ast.Dict)
            and any(
                isinstance(key, ast.Constant)
                and key.value == SCOPE
                and _mentions_scope(value)
                for key, value in zip(inner.keys, inner.values)
            )
            for inner in ast.walk(node)
        )

        #: Every query in this function that names no company_id, by line.
        self.unscoped_queries: list[int] = []
        for line, site in _query_sites(node):
            if not _opens_a_query(site):
                continue
            if _narrows_on_scope(site):
                continue
            if _reads_raw_sql(site) and self.binds_scope:
                continue
            # A site that hands the scope to somebody else has delegated the
            # filter along with it. Whether that somebody actually applies it is
            # asked of them, in their own entry above.
            if _hands_scope_on(site):
                continue
            self.unscoped_queries.append(line)
        self.unscoped_queries.sort()


def _scoped_facts(root: pathlib.Path):
    """Every function under a tree that takes a company_id, and its imports.

    Keyed by dotted module name, so app/state/audit.py and a future
    app/web/audit.py are two different answers rather than one.
    """
    facts: dict[tuple[str, str], _Facts] = {}
    imports: dict[str, dict[str, str]] = {}
    for path in sorted(root.rglob("*.py")):
        module = (
            path.relative_to(APP_DIR.parent).with_suffix("").as_posix().replace("/", ".")
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports[module] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[module][alias.asname or alias.name] = node.module
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            arguments = node.args
            names = [
                a.arg
                for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs
            ]
            if SCOPE in names:
                facts[(module, node.name)] = _Facts(module, node)
    return facts, imports


def _resolve(facts, imports, module: str, called: str):
    """Which definition a call names: this module's, or the one it imported.

    An unresolved name is deliberately NOT treated as satisfying anything. A
    helper this pass cannot find is a helper it cannot vouch for, and vouching
    for it would be the fallback that does not announce itself.
    """
    if (module, called) in facts:
        return (module, called)
    origin = imports.get(module, {}).get(called, "")
    if origin.startswith("app.") and (origin, called) in facts:
        return (origin, called)
    return None


def _reaches(facts, imports, key, attribute: str, seen=None) -> bool:
    """Whether this function, or anything it hands the scope to, does the thing."""
    seen = set() if seen is None else seen
    if key in seen:
        return False
    seen.add(key)
    if getattr(facts[key], attribute):
        return True
    for called in facts[key].hands_scope_to:
        target = _resolve(facts, imports, facts[key].module, called)
        if target is not None and _reaches(facts, imports, target, attribute, seen):
            return True
    return False


def test_every_public_scoped_function_routes_its_company_through_the_guard():
    """The guard rule. `if not company_id:` is not the guard and never was.

    app/state/audit.py held four of these and was the only module under
    app/state with no reference to _require_scope at all. The values that
    substitution lets through -- whitespace, padding, a LIKE wildcard -- match no
    row, so the read comes back empty and the caller is told the tenant is
    clean. In the audit log that reads as "the chain verifies".

    Private helpers are not asked: they are reached through a public entry point
    in the same module, and that entry point is asked here.

    APP/STATE ONLY, AND THE LINE IS DRAWN THERE ON PURPOSE. A view handler takes
    its company_id from Depends(current_company), which is the signed-in
    person's own tenant and has no string for a guard to refuse; requiring a
    _require_scope in every handler would add a call that can never fire and
    twenty-one names to a skip list. The state layer is where a scope arrives as
    a value somebody supplied, so it is where the value is checked. The filter
    rule below does cover the whole tree, because a query with no tenant clause
    is wrong wherever it is written.
    """
    facts, imports = _scoped_facts(STATE_DIR)
    unguarded = sorted(
        f"{module}.{name}:{facts[(module, name)].line}"
        for (module, name) in facts
        if facts[(module, name)].public
        and (module, name) not in AST_EXEMPT
        and not _reaches(facts, imports, (module, name), "guards")
    )
    assert not unguarded, (
        f"{unguarded} take a company_id and never hand it to _require_scope or "
        "row_for_company. A scope nothing validated matches no row and reports "
        "that as an empty tenant."
    )


def test_every_scoped_query_names_its_company_in_a_filter():
    """The filter rule. A parameter taken and not used is the shape of the bug.

    This is the guard that fails when a tenant filter is deleted, whether or not
    any caller reads the rows it would let through. Delete the filter in
    passage_counts_by_version and this test names it; before this file existed,
    nothing in 1,984 tests moved.

    ONE QUESTION PER QUERY. Asking it once per function let a function with two
    queries scope one of them and pass -- see the note at the head of this
    section, and app/state/routing.py::map_change_to_obligation, which is the
    instance.

    THE WHOLE OF app/, NOT ONLY app/state. Twenty-one of these lines live
    elsewhere -- app/auth/policy.py counts a person's claims, app/auth/sessions.py
    lists their live sessions, app/web/views/shares_admin.py and admin.py and
    workflow.py and chat.py each open their own query rather than going through
    the state layer. Every one of them is a tenant filter somebody can delete,
    and a rule that stopped at the package boundary would leave the ones outside
    it exactly as unguarded as passage_counts_by_version was.
    """
    facts, _ = _scoped_facts(APP_DIR)
    unfiltered = []
    for (module, name), entry in facts.items():
        allowed = AST_QUERY_EXEMPT.get((module, name), (0, ""))[0]
        if len(entry.unscoped_queries) <= allowed:
            continue
        # EVERY line, not the ones past the count. Which of them is the new one
        # is not something a count can say, and naming the wrong line would send
        # a reader to the query somebody already argued about.
        unfiltered.append(
            f"{module}.{name}: queries at {entry.unscoped_queries} name no "
            f"company_id, and {allowed} of them are exempt"
        )
    unfiltered.sort()
    assert not unfiltered, (
        f"{unfiltered} sit in a function that took a company_id and open a query "
        "that never names it -- not in a filter, not in a where, not as a bound "
        "parameter, and not by handing the scope to anything else. The scope was "
        "asked for and then not applied. If the query is right as it stands, add "
        "the function to AST_QUERY_EXEMPT above with the sentence that argues it, "
        "and raise its count."
    )


def test_no_exemption_from_the_filter_rule_has_gone_stale():
    """A skip list nobody prunes is the hand-written list again.

    An entry claiming more unscoped queries than the function now holds is an
    exemption somebody is still being granted for a problem that went away, and
    the next unscoped query in that function inherits it.
    """
    facts, _ = _scoped_facts(APP_DIR)
    stale = sorted(
        f"{module}.{name} claims {allowed} and holds "
        f"{len(facts[(module, name)].unscoped_queries) if (module, name) in facts else 0}"
        for (module, name), (allowed, _reason) in AST_QUERY_EXEMPT.items()
        if (module, name) not in facts
        or len(facts[(module, name)].unscoped_queries) < allowed
    )
    assert not stale, (
        f"{stale}. The exemption is wider than the thing it exempts; lower the "
        "count or delete the entry."
    )


def test_the_ast_pass_reads_the_modules_it_claims_to():
    """The control. A walk that parsed nothing would pass both rules above."""
    state, _ = _scoped_facts(STATE_DIR)
    modules = {module for module, _ in state}
    for required in ("app.state.audit", "app.state.queries", "app.state.permissions"):
        assert required in modules, f"{required} was not read"
    assert len(state) > 150, f"only {len(state)} scoped functions found under app/state"

    everywhere, _ = _scoped_facts(APP_DIR)
    outside = {module for module, _ in everywhere} - modules
    assert {"app.auth.policy", "app.web.views.shares_admin"} <= outside, (
        "the filter rule is no longer reading the modules outside app/state that "
        "open their own tenant-scoped queries"
    )


# ===========================================================================
# (c) THE DERIVED HTTP SWEEP
#
# NEITHER ISOLATION FILE TOUCHES THE WEB LAYER. The test the docs cite for it --
# tests/test_screens.py::test_every_route_refuses_another_tenant -- builds its
# OWN FastAPI with no auth middleware and changes tenant by setting
# STRATA_COMPANY_ID. app/web/deps.py says plainly that the environment fallback
# is unreachable through the running application: the middleware sends an
# anonymous request to /login before any view runs. So that test exercises a path
# production cannot reach, over four routes out of fifty-seven.
#
# This one signs in. It walks app.routes on the ASSEMBLED application -- the
# object `make run` starts -- as somebody whose company owns nothing, and asks
# every no-parameter GET for a page. Uncovered before it existed: /projects/{id},
# /review/{id}, /users, /permissions, all four /admin/* screens, /workflow,
# /chat, and every POST outside the workflow editor.
#
# WHAT IT ASSERTS, AND WHY THAT AND NOT MORE. Not the status code: a screen may
# answer 200 with an empty state, 404, or 303, and which of those is right is a
# per-screen decision. What it asserts is that MEP's docket, MEP's claim text and
# MEP's escalation id are not in the body. That is the sentence a leak would
# have to write.
# ===========================================================================

#: Routes this sweep does not open, each with the reason.
HTTP_EXEMPT = {
    "/logout": "ends the session the sweep is holding, so nothing after it is signed in",
    "/healthz": "counts rows across every company by design; app/main.py argues it",
    "/login": "unauthenticated by design and carries no tenant data",
}


def _no_parameter_gets() -> list[str]:
    """Every GET the assembled application serves that needs no path parameter.

    Read off app.routes rather than listed, so a screen mounted next month is
    swept because it exists. Path-parameter routes are opened separately below,
    with MEP's own ids in them, which is the sharper question anyway.
    """
    paths = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "GET" not in methods:
            continue
        if "{" in path or path.startswith("/static"):
            continue
        if path in HTTP_EXEMPT:
            continue
        paths.add(path)
    return sorted(paths)


@pytest.fixture(scope="module")
def rival_client() -> TestClient:
    """The assembled application, signed in as somebody who owns nothing.

    The corpus is MEP's. The person holding this client belongs to RIVAL, which
    has no proceeding, no claim and no escalation. Every page they open must say
    so, and none may say anything about MEP.

    THE ENVIRONMENT VARIABLE MOVES ONCE, FOR THE LOGIN, AND THEN POINTS THE OTHER
    WAY FOR THE WHOLE SWEEP. A user row is unique per company and email, so the
    login form has to know which tenant it is authenticating against before it
    knows who -- app/web/views/auth.py says so at length, and with nobody signed
    in that answer comes from STRATA_COMPANY_ID. That is the deployment's own
    setting and it is the production path for signing in. Everything AFTER the
    login is a different question, and the variable is put back to MEP before a
    single page is opened: if any screen reads the environment rather than the
    person, it serves MEP's rows here and the assertions fail. That is the
    difference between this sweep and the one it replaces, which switched tenant
    by setting the variable and never signed anybody in at all.
    """
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        # The proposed actions too, so app/state/actions.py has rows to probe.
        # Without them the sweep cannot build a call for record_decision, and a
        # probe it cannot build is a probe that silently stopped running.
        ensure_demo_actions(session)
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        _populate(session)
    client = TestClient(app, base_url="https://testserver")
    patch = pytest.MonkeyPatch()
    try:
        patch.setenv(deps.COMPANY_ENV, RIVAL)
        response = client.post(
            "/login",
            data={"email": RIVAL_EMAIL, "password": RIVAL_PASSWORD},
            follow_redirects=False,
        )
    finally:
        patch.undo()
    assert response.status_code == 303, "the sweep must be signed in to mean anything"

    with pytest.MonkeyPatch.context() as pointing_at_mep:
        pointing_at_mep.setenv(deps.COMPANY_ENV, COMPANY)
        yield client


#: How long a string has to be before its presence in a page means anything.
#:
#: MEP's document version ids are "v1", "v2", "v3". Searching an HTML page for
#: "v1" finds it in a class name, a version number and half the English words
#: with a v in them, and the sweep reported four routes leaking when none was.
#: A sentinel shorter than this proves nothing either way, so it is dropped from
#: the leak test rather than left in to produce noise -- and it stays in the
#: substitution map below, because putting a real short id INTO a URL is still
#: the right question to ask.
SENTINEL_FLOOR = 6


@pytest.fixture(scope="module")
def mep_ids(rival_client) -> dict[str, str]:
    """MEP's real ids, for putting into somebody else's request.

    Takes rival_client so that the corpus is already seeded when this reads it.
    Two module fixtures both call init_db(), which DROPS every table, so the one
    that reads has to be the one that runs second -- and saying so in the
    signature is the only way to make that true rather than lucky.

    THE LONGEST ID OF EACH KIND, not the first. The corpus holds PRJ-1 and
    PRJ-MPUC-2026-0142, and only the second is a string whose appearance on a
    page says something. Picking by length is derived; picking by name would be
    another hand-kept list.
    """
    from app.state import models as m

    def longest(model):
        rows = (
            session.query(model).filter(model.company_id == COMPANY).all()
        )
        return max((row.id for row in rows), key=len, default=None)

    with session_scope() as session:
        version = (
            session.query(m.DocumentVersion)
            .filter(m.DocumentVersion.company_id == COMPANY)
            .order_by(m.DocumentVersion.id)
            .first()
        )
        claim = (
            session.query(m.Claim)
            .filter(m.Claim.company_id == COMPANY)
            .order_by(m.Claim.id)
            .first()
        )
        assert version is not None and claim is not None
        return {
            "docket": version.docket,
            "version_id": version.id,
            "claim_statement": claim.statement[:60],
            "claim_id": longest(m.Claim),
            "escalation_id": longest(m.Escalation),
            "change_id": longest(m.Change),
            "project_id": longest(m.Project),
            "feedback_id": longest(m.Feedback),
            "invitation_id": longest(m.Invitation),
            "source_id": longest(m.SourceRegistration),
            "role_id": longest(m.Role),
            "workflow_id": longest(m.ApprovalWorkflow),
            "share_id": longest(m.ShareLink),
        }


@pytest.fixture(scope="module")
def mep_sentinels(mep_ids) -> dict[str, str]:
    """The subset of MEP's ids long enough for their presence to mean something."""
    kept = {
        name: value
        for name, value in mep_ids.items()
        if value and len(value) >= SENTINEL_FLOOR
    }
    for required in ("docket", "escalation_id", "claim_statement"):
        assert required in kept, f"{required} is no longer a usable sentinel"
    return kept


def _leaking(body: str, sentinels: dict[str, str]) -> list[str]:
    return sorted(
        name
        for name, value in sentinels.items()
        if value and value in body
    )


def test_no_route_hands_a_page_of_another_tenants_rows_to_the_signed_in_person(
    rival_client, mep_sentinels
):
    """Every no-parameter GET on the assembled app, opened as RIVAL.

    Fifty-odd routes rather than four, and behind the real middleware rather than
    a bare router, so what is proved here is what a person actually gets.
    """
    swept = _no_parameter_gets()
    assert swept, "app.routes yielded no GET routes, so this test proves nothing"

    leaked = {}
    for path in swept:
        response = rival_client.get(path, follow_redirects=False)
        found = _leaking(response.text, mep_sentinels)
        if found:
            leaked[path] = found
    assert not leaked, f"these routes served MEP's rows to RIVAL: {leaked}"


def test_no_route_taking_an_id_serves_that_id_when_it_belongs_to_another_tenant(
    rival_client, mep_ids, mep_sentinels
):
    """The sharper half. Ids are unique across tenants and the corpus prints them.

    Every path-parameter GET is opened with MEP's real id substituted in. A
    screen that resolves the row by primary key and forgets whose it is answers
    200 with the page, which is how app/state/routing.py::ensure_obligation used
    to behave one layer down.
    """
    substitutions = dict(mep_ids)
    substitutions["proceeding_id"] = mep_ids["docket"]

    opened, leaked = [], {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "GET" not in methods or "{" not in path:
            continue
        names = re.findall(r"{([^}:]+)", path)
        if any(substitutions.get(name) is None for name in names):
            continue
        target = path
        for name in names:
            target = re.sub(r"{" + re.escape(name) + r"(:[^}]+)?}", substitutions[name], target)
        opened.append(target)
        response = rival_client.get(target, follow_redirects=False)
        found = _leaking(response.text, mep_sentinels)
        # The id the sweep itself put in the URL is not a leak; a page echoing
        # the path back says nothing about whether a row was found. What matters
        # is anything MEP's that the sweep did NOT supply.
        supplied = {
            name
            for name, value in mep_sentinels.items()
            if value and value in target
        }
        found = [name for name in found if name not in supplied]
        if found:
            leaked[target] = found

    assert opened, "no path-parameter GET could be built, so this proves nothing"
    assert not leaked, f"these routes served MEP's rows to RIVAL: {leaked}"


def test_no_post_writes_into_another_tenants_record(
    rival_client, mep_ids, mep_sentinels
):
    """Every POST the assembled application serves, sent as RIVAL with MEP's ids.

    THE WRITE HALF, WHICH NOTHING SWEPT AT ALL. The test this replaces posted to
    exactly one endpoint -- the escalation resolver -- and every other POST in
    the product went unasked: granting a permission, revoking a share, editing a
    role, provisioning a login, activating a route, filing a bug. A read that
    crosses a tenant boundary shows somebody a page. A WRITE that crosses one
    changes another company's record, and in this product several of those
    records are the ones an auditor is meant to be able to trust.

    Three assertions, and the second and third are the ones that matter. The
    response must not carry MEP's rows. MEP's audit chain must be exactly as long
    afterwards as before, because every act worth doing writes to it -- so a
    cross-tenant write that succeeded shows up as a number that moved, whatever
    the response said. And MEP's escalation must still be closed, which is a
    single concrete piece of state one of these routes can reach.

    Bodies are the same value map the read sweep uses. Most of these come back
    422 or 403, and that is the right answer: the sweep is asking whether a
    refusal happens, not building a valid form for each one.
    """
    from app.state import audit, models

    with session_scope() as session:
        before = audit.event_count(session, COMPANY)
        escalation = session.get(models.Escalation, mep_ids["escalation_id"])
        resolved_before = escalation.resolved_at

    substitutions = dict(mep_ids)
    substitutions["proceeding_id"] = mep_ids["docket"]
    substitutions["token"] = "not-a-live-token"
    form = {
        "decision": "approve",
        "reason": "the derived tenancy sweep",
        "name": "Sweep probe",
        "title": "Sweep probe",
        "detail": "the derived tenancy sweep",
        "email": "sweep-probe@rival.example",
        "display_name": "A sweep probe",
        "role": ROLE_ANALYST,
        "code": "claim.read",
        "codes": "claim.read",
        "user_id": "not-a-user",
        "share_id": mep_ids["share_id"] or "not-a-share",
        "instruction": "the derived tenancy sweep",
        "message": "the derived tenancy sweep",
        "project_id": mep_ids["project_id"],
        "escalation_id": mep_ids["escalation_id"],
        "change_id": mep_ids["change_id"],
    }

    sent, leaked = [], {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "POST" not in methods or path in HTTP_EXEMPT:
            continue
        names = re.findall(r"{([^}:]+)", path)
        if any(substitutions.get(name) is None for name in names):
            continue
        target = path
        for name in names:
            target = re.sub(
                r"{" + re.escape(name) + r"(:[^}]+)?}", substitutions[name], target
            )
        sent.append(target)
        response = rival_client.post(target, data=form, follow_redirects=False)
        # WHAT THE SWEEP PUT IN IS NOT WHAT THE SWEEP GOT OUT. A 422 echoes the
        # submitted body back verbatim, and PRJ-MPUC-2026-0142 carries the docket
        # inside it -- so an id is discounted when it appears anywhere in the URL
        # or in any field sent, as a substring and not only whole. Matching whole
        # values only reported four routes as leaking when none was, which is the
        # kind of false alarm that gets a guard switched off.
        supplied = {
            name
            for name, value in mep_sentinels.items()
            if value
            and (value in target or any(value in str(sent_value) for sent_value in form.values()))
        }
        found = [
            name
            for name in _leaking(response.text, mep_sentinels)
            if name not in supplied
        ]
        if found:
            leaked[target] = found

    assert sent, "no POST could be built, so this test proves nothing"
    assert not leaked, f"these POSTs answered with MEP's rows: {leaked}"

    with session_scope() as session:
        after = audit.event_count(session, COMPANY)
        escalation = session.get(models.Escalation, mep_sentinels["escalation_id"])
        assert after == before, (
            f"a POST sent as RIVAL wrote {after - before} row(s) into MEP's audit "
            "chain"
        )
        assert escalation.resolved_at == resolved_before, (
            "a POST sent as RIVAL changed the state of MEP's escalation"
        )


def test_the_http_sweep_covers_the_screens_the_hand_written_list_missed(
    rival_client, mep_sentinels
):
    """The control, named after the gap it closes.

    tests/test_screens.py::test_every_route_refuses_another_tenant covered four
    routes out of fifty-seven, all of them GETs bar one. These are the ones it
    did not, and if a router stops being mounted this fails here rather than
    letting the sweep silently shrink back to where it started.
    """
    swept = set(_no_parameter_gets())
    for path in (
        "/users",
        "/permissions",
        "/admin",
        "/admin/shares",
        "/admin/sources",
        "/admin/feedback",
        "/admin/invites",
        "/admin/workflows",
        "/workflow",
        "/projects",
    ):
        assert path in swept, f"{path} is not in the derived GET sweep"

    posts = {
        getattr(route, "path", "")
        for route in app.routes
        if "POST" in (getattr(route, "methods", None) or set())
    }
    for path in (
        "/chat",
        "/chat/feedback",
        "/feedback/bug",
        "/permissions/grant",
        "/permissions/revoke",
        "/permissions/roles",
        "/users/provision",
        "/admin/shares/revoke",
        "/review/steer",
    ):
        assert path in posts, f"{path} is no longer mounted, so the sweep lost it"


def test_the_signed_in_owner_still_sees_their_own_rows(mep_sentinels):
    """Not vacuous. The same routes serve MEP to somebody who belongs to MEP.

    Without this, a build where every screen answered 500 would pass the two
    sweeps above with room to spare.
    """
    admin = next(
        account for account in demo_account_list() if account.role == ROLE_ADMIN
    )
    client = TestClient(app, base_url="https://testserver")
    assert (
        client.post(
            "/login",
            data={"email": admin.email, "password": DEMO_PASSWORD},
            follow_redirects=False,
        ).status_code
        == 303
    )
    seen = set()
    for path in _no_parameter_gets():
        seen.update(_leaking(client.get(path, follow_redirects=False).text, mep_sentinels))
    assert seen, "no screen showed MEP anything, so the sweep above proves nothing"
