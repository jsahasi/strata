"""The tool layer is the security boundary of chat. This suite is that claim, tested.

Seven wrong implementations this file exists to catch. Each of them passes a
casual read of app/chat/tools.py, and each has shipped in somebody's product.

1. A dispatcher that IGNORES an identity argument instead of refusing it. The
   turn then succeeds, correctly scoped, and the only record that somebody tried
   to pass a company_id is gone. Silently dropping the argument destroys the
   evidence that prompt injection reached the tool layer.
2. A tool that takes company_id positionally, or with a default. Either one lets
   a caller -- or a refactor -- put the model in charge of the scope.
3. search_claims returning the text of a claim whose citation did not verify,
   at any depth of the returned structure. That is the one thing this product
   promises never to do.
4. A tool that filtered something out and did not say so. A silent count is
   worse than a wrong one, because nothing on the page is untrue.
5. A write that skips the permission gate, or one whose refusal leaves no row.
6. Another tenant's id resolving to a row rather than to nothing.
7. A refusal that breaks the audit chain it is written into.

The suite also pins the ALLOWLIST against the signatures, so a tool added later
cannot quietly accept an argument nobody declared.
"""

import inspect
from dataclasses import is_dataclass

import pytest

from app.auth import policy
from app.chat import tools as chat_tools
from app.state.audit import event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    create_user,
    ensure_system_roles,
    grant_role,
)
from app.state.models import (
    AUTHOR_ANALYST,
    Change,
    Claim,
    DocumentVersion,
    Escalation,
    Obligation,
    Proceeding,
    Project,
    ProjectChange,
    ResearchTurn,
)
from app.state.projects import projects_for_company

COMPANY = "MEP"
RIVAL = "RIVAL"
PASSWORD = "correct-horse-battery-staple"
ANALYST = "ana@mep.example"
OWNER = "owen@mep.example"

SOURCE_V1 = (
    "Each utility shall file a distribution system implementation plan within "
    "one hundred and twenty days of the effective date of this order."
)
SOURCE_V2 = (
    "Each utility shall file a distribution system implementation plan within "
    "sixty days of the effective date of this order."
)

# The sentence the product must refuse to say. Nothing in either source text
# contains it, so any appearance in a tool result came from the claim row.
WITHHELD_STATEMENT = "GAS MAINS MUST BE REPLACED WITHIN NINETY DAYS"
WITHHELD_FRAGMENT = "NINETY DAYS"

VERIFIED_STATEMENT = "The plan is now due within sixty days."


# ---------------------------------------------------------------------------
# The workspace every test reads
# ---------------------------------------------------------------------------


def _seed(session, company: str) -> dict:
    """One proceeding, two versions, one change, two claims -- one of each kind.

    Built by hand rather than through the pipeline. This suite is about what the
    tool layer will and will not hand back, not about how a claim is written,
    and the pipeline would decide the offsets for me.
    """
    tag = company.lower()
    v1, v2 = f"ver-{tag}-1", f"ver-{tag}-2"
    proceeding_id = f"prc-{tag}"
    change_id = f"chg-{tag}-1"

    session.add(
        Proceeding(
            id=proceeding_id,
            company_id=company,
            docket="24-M-0001",
            commission="NYPSC",
            subject="Distribution system implementation plans",
        )
    )
    for version_id, text, label in (
        (v1, SOURCE_V1, "Order, October"),
        (v2, SOURCE_V2, "Order, December"),
    ):
        session.add(
            DocumentVersion(
                id=version_id,
                company_id=company,
                docket="24-M-0001",
                label=label,
                status="FINAL",
                source_text=text,
                source_sha256="0" * 64,
            )
        )
    session.add(
        Change(
            id=change_id,
            company_id=company,
            proceeding_id=proceeding_id,
            from_version_id=v1,
            to_version_id=v2,
            change_type="modified",
            before_start=0,
            before_end=len(SOURCE_V1),
            after_start=0,
            after_end=len(SOURCE_V2),
            section="II",
            alignment_confidence=0.94,
            materiality=None,
            status="FINAL",
        )
    )

    good_quote = "within sixty days"
    good_start = SOURCE_V2.index(good_quote)
    session.add(
        Claim(
            id=f"clm-{tag}-good",
            company_id=company,
            change_id=change_id,
            statement=VERIFIED_STATEMENT,
            citation_version_id=v2,
            citation_start=good_start,
            citation_end=good_start + len(good_quote),
            citation_quote=good_quote,
            cited_occurrence=None,
            confidence_bp=9000,
        )
    )
    # The citation quotes words the source does not carry at those offsets, so
    # verified_claims() withholds it on every read.
    session.add(
        Claim(
            id=f"clm-{tag}-bad",
            company_id=company,
            change_id=change_id,
            statement=WITHHELD_STATEMENT,
            citation_version_id=v2,
            citation_start=good_start,
            citation_end=good_start + len(good_quote),
            citation_quote="within fifteen days",
            cited_occurrence=None,
            confidence_bp=9500,
        )
    )
    session.add(
        Escalation(
            id=f"esc-{tag}-bad",
            company_id=company,
            claim_id=f"clm-{tag}-bad",
            reason_code="CITATION_QUOTE_MISMATCH",
            reason_text="the quoted text is not what the source says there",
            detail="quoted 'within fifteen days'",
        )
    )
    session.add(
        Project(
            id=f"proj-{tag}",
            company_id=company,
            name=f"{company} distribution plans",
            docket_ref="24-M-0001",
            jurisdiction="New York",
            status="active",
            owner="somebody",
            summary="",
        )
    )
    session.add(
        ProjectChange(
            company_id=company,
            project_id=f"proj-{tag}",
            change_id=change_id,
            attached_by="somebody",
        )
    )
    session.add(
        Obligation(
            id=f"OBL-{tag}",
            company_id=company,
            title="File the plan on time",
            owner_user_id=None,
            project_id=f"proj-{tag}",
            source_document_ref="DOC-2",
        )
    )
    session.flush()
    return {
        "proceeding_id": proceeding_id,
        "change_id": change_id,
        "project_id": f"proj-{tag}",
        "escalation_id": f"esc-{tag}-bad",
        "obligation_id": f"OBL-{tag}",
    }


@pytest.fixture
def world():
    """Two companies, two people, and one claim of each kind in each."""
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        mep = _seed(session, COMPANY)
        rival = _seed(session, RIVAL)

        analyst = create_user(
            session,
            COMPANY,
            email=ANALYST,
            display_name="Ana Ruiz",
            password=PASSWORD,
            actor="setup",
        )
        owner = create_user(
            session,
            COMPANY,
            email=OWNER,
            display_name="Owen Blake",
            password=PASSWORD,
            actor="setup",
        )
        grant_role(
            session,
            COMPANY,
            user_id=analyst.id,
            role_name=ROLE_ANALYST,
            actor="setup",
        )
        grant_role(
            session,
            COMPANY,
            user_id=owner.id,
            role_name=ROLE_OBLIGATION_OWNER,
            actor="setup",
        )
        outsider = create_user(
            session,
            RIVAL,
            email="rival@rival.example",
            display_name="Rae Vale",
            password=PASSWORD,
            actor="setup",
        )
        session.commit()
        return {
            "mep": mep,
            "rival": rival,
            "analyst_id": analyst.id,
            "owner_id": owner.id,
            "outsider_id": outsider.id,
        }


def _actor(session, user_id: str):
    from app.state.models import User

    return session.query(User).filter(User.id == user_id).one()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _every_string(value):
    """Every string reachable in a result, however deeply it is buried.

    Objects that are neither containers nor strings are rendered with str(), so
    an ORM row or a dataclass that leaked into the payload is caught by its own
    repr rather than slipping past as "not a string".
    """
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _every_string(key)
            yield from _every_string(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _every_string(item)
        return
    if is_dataclass(value) and not isinstance(value, type):
        yield repr(value)
        return
    yield str(value)


def _assert_no_withheld_text(result) -> None:
    for text in _every_string(result):
        assert WITHHELD_STATEMENT not in text, f"the statement escaped in {text!r}"
        assert WITHHELD_FRAGMENT not in text, f"a fragment escaped in {text!r}"


def _actions(session, company_id: str) -> list[str]:
    from app.state.models import AuditEvent

    return [
        row.action
        for row in session.query(AuditEvent)
        .filter(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.seq)
        .all()
    ]


# ---------------------------------------------------------------------------
# 1. The dispatcher refuses identity arguments, and records the attempt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,value",
    [
        ("company_id", RIVAL),
        ("company_id", COMPANY),
        ("actor", "admin"),
        ("user_id", "usr-someone"),
        ("session", "anything"),
    ],
)
def test_an_identity_argument_refuses_the_turn(world, name, value):
    """Not ignored. Refused, halted, and written into the chain as an attempt.

    The COMPANY case is deliberate: an argument naming the caller's own company
    is refused too. What is being recorded is that the model supplied identity
    at all, and a check that only fires on a mismatch misses the reconnaissance.
    """
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        before = event_count(session, COMPANY)

        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="find_projects",
            arguments={"query": "distribution", name: value},
        )

        assert result["ok"] is False
        assert result["halt_turn"] is True
        assert result["reason_code"] == chat_tools.REASON_IDENTITY_ARGUMENT
        assert name in result["reason"]
        # The tool did not run.
        assert result["found"] == 0
        assert "projects" not in result

        assert event_count(session, COMPANY) == before + 1
        assert _actions(session, COMPANY)[-1] == chat_tools.ACTION_CHAT_IDENTITY_INJECTED
        assert verify_chain(session, COMPANY) is True


def test_the_injected_value_is_never_echoed_back(world):
    """The refusal names the offending key and never the value.

    The value is attacker-controlled text and the chain cannot be redacted, so
    what goes in the record is what makes the attempt countable and nothing else.
    Echoing it back into the model's context would confirm the guess as well.
    """
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="project_detail",
            arguments={"project_id": world["rival"]["project_id"], "company_id": RIVAL},
        )
        assert result["ok"] is False
        assert "company_id" in result["reason"]
        assert "project" not in result
        for text in _every_string(result):
            assert RIVAL not in text

        from app.state.models import AuditEvent

        row = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .order_by(AuditEvent.seq.desc())
            .first()
        )
        assert row.action == chat_tools.ACTION_CHAT_IDENTITY_INJECTED
        assert RIVAL not in row.reason


def test_an_undeclared_argument_is_refused_and_audited(world):
    """The allowlist is per tool. An argument nobody declared stops the call."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        before = event_count(session, COMPANY)
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="find_projects",
            arguments={"query": "x", "limit": 5},
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_UNKNOWN_ARGUMENT
        # A fumble, not an attack: the model may try another tool.
        assert result["halt_turn"] is False
        assert event_count(session, COMPANY) == before + 1
        assert _actions(session, COMPANY)[-1] == chat_tools.ACTION_CHAT_TOOL_REFUSED


def test_identity_is_reported_before_an_unknown_argument(world):
    """An injection mixed with noise is still recorded as an injection."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="find_projects",
            arguments={"nonsense": 1, "actor": "root"},
        )
        assert result["reason_code"] == chat_tools.REASON_IDENTITY_ARGUMENT
        assert _actions(session, COMPANY)[-1] == chat_tools.ACTION_CHAT_IDENTITY_INJECTED


def test_an_unknown_tool_is_refused_and_audited(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        before = event_count(session, COMPANY)
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="delete_everything",
            arguments={},
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_UNKNOWN_TOOL
        assert event_count(session, COMPANY) == before + 1
        assert _actions(session, COMPANY)[-1] == chat_tools.ACTION_CHAT_TOOL_REFUSED


def test_an_argument_of_the_wrong_type_is_refused(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="latest_changes",
            arguments={"limit": "all of them"},
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_BAD_ARGUMENT


def test_an_actor_from_another_company_halts_the_turn(world):
    """The two injected values have to agree. Disagreement is not a scope hint."""
    with session_scope() as session:
        outsider = _actor(session, world["outsider_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=outsider,
            tool="find_projects",
            arguments={"query": ""},
        )
        assert result["ok"] is False
        assert result["halt_turn"] is True
        assert result["reason_code"] == chat_tools.REASON_ACTOR_MISMATCH
        assert _actions(session, COMPANY)[-1] == chat_tools.ACTION_CHAT_TOOL_REFUSED


def test_a_missing_actor_is_a_caller_defect_not_a_refusal(world):
    """The tool layer injects the actor. Its absence is a bug in the caller."""
    with session_scope() as session:
        with pytest.raises(ValueError):
            chat_tools.dispatch(
                session,
                company_id=COMPANY,
                actor=None,
                tool="find_projects",
                arguments={"query": ""},
            )


def test_an_unscoped_dispatch_raises(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        with pytest.raises(ValueError):
            chat_tools.dispatch(
                session,
                company_id="",
                actor=actor,
                tool="find_projects",
                arguments={"query": ""},
            )


# ---------------------------------------------------------------------------
# 2. The shape of every tool, checked structurally rather than by review
# ---------------------------------------------------------------------------


def test_every_tool_takes_identity_keyword_only_with_no_default():
    """The isolation design, asserted against the signatures rather than trusted.

    A tool added later that spells company_id as a positional parameter, or
    gives it a default, fails here rather than in production.
    """
    for name, tool in chat_tools.REGISTRY.items():
        signature = inspect.signature(tool.run)
        parameters = list(signature.parameters.values())
        assert parameters[0].name == "session", name
        assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        for wanted in ("company_id", "actor"):
            parameter = signature.parameters[wanted]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}.{wanted}"
            assert parameter.default is inspect.Parameter.empty, f"{name}.{wanted}"
        for parameter in parameters[1:]:
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{name}.{parameter.name} must be keyword-only"
            )


def test_the_allowlist_matches_the_signature():
    """The declared arguments and the real ones cannot drift apart."""
    for name, tool in chat_tools.REGISTRY.items():
        signature = inspect.signature(tool.run)
        from_signature = {
            parameter
            for parameter in signature.parameters
            if parameter not in ("session", "company_id", "actor")
        }
        assert set(tool.arguments) == from_signature, name


def test_no_tool_declares_an_identity_argument():
    for name, tool in chat_tools.REGISTRY.items():
        assert not set(tool.arguments) & chat_tools.IDENTITY_ARGUMENTS, name


def test_the_contract_tools_are_all_present():
    assert set(chat_tools.REGISTRY) == {
        "find_projects",
        "project_detail",
        "latest_changes",
        "change_detail",
        "open_escalations",
        "obligation_owner",
        "approval_route",
        "search_claims",
        "create_project",
        "record_note",
    }
    writes = {name for name, tool in chat_tools.REGISTRY.items() if tool.writes}
    assert writes == {"create_project", "record_note"}


def test_a_gated_tool_carries_its_own_permission_code():
    """Which tool is gated is spelt once, in the registry, not in every caller.

    app/chat/agent.py needs the code to tell a person up front that they do not
    hold it, rather than spending a round on a refusal. Reading it from the tool
    means a gate added later is offered to that caller without an edit; a map
    kept beside the registry is a second answer that goes stale in silence.
    """
    from app.state.models import PERMISSION_CODES

    assert (
        chat_tools.REGISTRY["create_project"].permission
        == chat_tools.PERMISSION_PROJECT_CREATE
    )
    for name, tool in chat_tools.REGISTRY.items():
        if tool.permission:
            assert tool.permission in PERMISSION_CODES, name
        else:
            assert name != "create_project"


def test_every_result_carries_a_withheld_count_and_an_omitted_count(world):
    """A caller summing these must never reach a missing key and read it as zero."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        calls = {
            "find_projects": {"query": ""},
            "project_detail": {"project_id": world["mep"]["project_id"]},
            "latest_changes": {},
            "change_detail": {"change_id": world["mep"]["change_id"]},
            "open_escalations": {},
            "obligation_owner": {},
            "approval_route": {},
            "search_claims": {"query": "plan"},
        }
        for name, arguments in calls.items():
            result = chat_tools.dispatch(
                session,
                company_id=COMPANY,
                actor=actor,
                tool=name,
                arguments=arguments,
            )
            assert result["ok"] is True, (name, result)
            assert isinstance(result["withheld"], int), name
            assert isinstance(result["omitted"], int), name
            assert isinstance(result["found"], int), name
            assert result["tool"] == name


# ---------------------------------------------------------------------------
# 3. search_claims: the statement of a withheld claim never comes back
# ---------------------------------------------------------------------------


def test_search_claims_never_returns_a_withheld_statement(world):
    """Ask for the withheld claim by its own words and get the reason instead."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        for query in ("gas mains", "ninety", WITHHELD_STATEMENT, ""):
            result = chat_tools.search_claims(
                session, company_id=COMPANY, actor=actor, query=query, project_id=None
            )
            _assert_no_withheld_text(result)


def test_search_claims_says_how_many_it_withheld_and_why(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="", project_id=None
        )
        assert result["withheld"] == 1
        assert len(result["withheld_claims"]) == 1
        held = result["withheld_claims"][0]
        assert held["claim_id"] == "clm-mep-bad"
        assert held["reason_code"] == "CITATION_QUOTE_MISMATCH"
        assert held["reason_text"]
        assert held["escalation_id"] == world["mep"]["escalation_id"]
        assert "statement" not in held


def test_search_claims_returns_the_verified_statement(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="sixty", project_id=None
        )
        assert result["found"] == 1
        assert result["claims"][0]["statement"] == VERIFIED_STATEMENT
        assert result["claims"][0]["claim_id"] == "clm-mep-good"


def test_search_claims_still_reports_withholding_when_it_finds_nothing(world):
    """found 0 and withheld 1 is a real answer. found 0 alone is a lie."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.search_claims(
            session,
            company_id=COMPANY,
            actor=actor,
            query="a phrase no claim contains",
            project_id=None,
        )
        assert result["found"] == 0
        assert result["withheld"] == 1


def test_search_claims_through_the_dispatcher_withholds_too(world):
    """The property has to hold on the path the model actually takes."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="search_claims",
            arguments={"query": "ninety days"},
        )
        _assert_no_withheld_text(result)
        assert result["withheld"] == 1


def test_change_detail_withholds_the_same_claim(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.change_detail(
            session, company_id=COMPANY, actor=actor, change_id=world["mep"]["change_id"]
        )
        _assert_no_withheld_text(result)
        assert result["withheld"] == 1
        assert len(result["claims"]) == 1
        assert result["claims"][0]["statement"] == VERIFIED_STATEMENT


def test_a_claim_withheld_by_an_edit_to_the_source_stops_asserting(world):
    """The verdict is recomputed on every read, so chat follows the source.

    Editing the cited bytes must flip a verified claim to withheld on the next
    call, with nothing re-running and no stored verdict to update.
    """
    with session_scope() as session:
        version = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.company_id == COMPANY)
            .filter(DocumentVersion.id == "ver-mep-2")
            .one()
        )
        version.source_text = SOURCE_V2.replace("sixty", "ninety")
        session.flush()

        actor = _actor(session, world["analyst_id"])
        result = chat_tools.change_detail(
            session, company_id=COMPANY, actor=actor, change_id=world["mep"]["change_id"]
        )
        assert result["found"] == 0
        assert result["withheld"] == 2
        for text in _every_string(result):
            assert VERIFIED_STATEMENT not in text


# ---------------------------------------------------------------------------
# 4. Tenant isolation, through the tools
# ---------------------------------------------------------------------------


def test_another_companys_ids_resolve_to_nothing(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        rival = world["rival"]

        detail = chat_tools.project_detail(
            session, company_id=COMPANY, actor=actor, project_id=rival["project_id"]
        )
        assert detail["found"] == 0
        assert detail["project"] is None

        change = chat_tools.change_detail(
            session, company_id=COMPANY, actor=actor, change_id=rival["change_id"]
        )
        assert change["found"] == 0
        assert change["change"] is None

        obligation = chat_tools.obligation_owner(
            session,
            company_id=COMPANY,
            actor=actor,
            obligation_id=rival["obligation_id"],
        )
        assert obligation["found"] == 0


def test_a_search_scoped_to_another_companys_project_finds_nothing(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.search_claims(
            session,
            company_id=COMPANY,
            actor=actor,
            query="",
            project_id=world["rival"]["project_id"],
        )
        assert result["found"] == 0
        assert result["withheld"] == 0
        assert result["note"]


def test_find_projects_lists_only_this_company(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.find_projects(
            session, company_id=COMPANY, actor=actor, query=""
        )
        ids = {row["project_id"] for row in result["projects"]}
        assert ids == {world["mep"]["project_id"]}


# ---------------------------------------------------------------------------
# 5. Reads that must not guess, and counts that must not hide
# ---------------------------------------------------------------------------


def test_latest_changes_names_the_clock_it_sorted_by(world):
    """Ordering has to be checkable. A silent guess at "newest" is a fallback."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.latest_changes(
            session, company_id=COMPANY, actor=actor, project_id=None, limit=None
        )
        assert result["found"] == 1
        assert result["ordered_by"] in chat_tools.ORDER_BASES
        assert result["note"]
        row = result["changes"][0]
        assert row["from_version_id"] == "ver-mep-1"
        assert row["to_version_id"] == "ver-mep-2"
        assert row["from_version_label"] == "Order, October"
        assert row["to_version_label"] == "Order, December"


def test_latest_changes_counts_what_the_limit_left_out(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        for tag in ("2", "3"):
            session.add(
                Change(
                    id=f"chg-mep-{tag}",
                    company_id=COMPANY,
                    proceeding_id="prc-mep",
                    from_version_id="ver-mep-1",
                    to_version_id="ver-mep-2",
                    change_type="added",
                    before_start=None,
                    before_end=None,
                    after_start=0,
                    after_end=5,
                    section="III",
                    alignment_confidence=0.5,
                    materiality=None,
                    status="FINAL",
                )
            )
        session.flush()
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.latest_changes(
            session, company_id=COMPANY, actor=actor, project_id=None, limit=1
        )
        assert result["found"] == 1
        assert result["omitted"] == 2
        assert "2" in result["note"]


def test_a_limit_the_tool_would_not_honour_says_so(world):
    """Clamping in silence is a fallback that does not announce itself."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        high = chat_tools.latest_changes(
            session,
            company_id=COMPANY,
            actor=actor,
            project_id=None,
            limit=chat_tools.MAX_CHANGE_LIMIT + 1,
        )
        assert str(chat_tools.MAX_CHANGE_LIMIT) in high["note"]

        low = chat_tools.latest_changes(
            session, company_id=COMPANY, actor=actor, project_id=None, limit=0
        )
        assert "0" in low["note"]


def test_the_registry_refuses_a_tool_that_gets_identity_wrong():
    """The guard that makes the whole design structural, tested from the outside.

    Four ways to write a tool wrongly, each of which would put the model in
    charge of the scope. All four must stop the process at import rather than
    ship a hole that reviews well.
    """

    def positional_scope(session, company_id, *, actor):
        return {}

    def defaulted_scope(session, *, company_id="MEP", actor=None):
        return {}

    def declares_identity(session, *, company_id, actor, user_id=None):
        return {}

    def untyped_argument(session, *, company_id, actor, wobble=None):
        return {}

    for candidate in (
        positional_scope,
        defaulted_scope,
        declares_identity,
        untyped_argument,
    ):
        with pytest.raises(ValueError):
            chat_tools._declare(candidate)


def test_open_escalations_counts_every_row_as_withheld(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.open_escalations(
            session, company_id=COMPANY, actor=actor, mine_only=None
        )
        assert result["found"] == 1
        assert result["withheld"] == 1
        held = result["escalations"][0]
        assert held["escalation_id"] == world["mep"]["escalation_id"]
        assert held["assigned_to"] is None
        assert held["reason_code"]
        _assert_no_withheld_text(result)


def test_open_escalations_mine_only_returns_only_mine(world):
    with session_scope() as session:
        escalation = (
            session.query(Escalation)
            .filter(Escalation.company_id == COMPANY)
            .one()
        )
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.open_escalations(
            session, company_id=COMPANY, actor=actor, mine_only=True
        )
        assert result["found"] == 0

        from datetime import datetime, timezone

        escalation.assigned_to_user_id = world["analyst_id"]
        escalation.assigned_at = datetime.now(timezone.utc)
        session.flush()

        mine = chat_tools.open_escalations(
            session, company_id=COMPANY, actor=actor, mine_only=True
        )
        assert mine["found"] == 1
        assert mine["escalations"][0]["assigned_to"] == "Ana Ruiz"


def test_an_unowned_obligation_reads_as_work_not_as_a_blank(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.obligation_owner(
            session,
            company_id=COMPANY,
            actor=actor,
            obligation_id=world["mep"]["obligation_id"],
        )
        assert result["found"] == 1
        row = result["obligations"][0]
        assert row["owner"] is None
        assert row["note"]


def test_approval_route_says_there_is_no_route_rather_than_nothing(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.approval_route(
            session, company_id=COMPANY, actor=actor, item_id=None
        )
        assert result["found"] == 0
        assert result["route"] is None
        assert result["note"]


# ---------------------------------------------------------------------------
# 6. Writes: gated, audited, and refused without an exception reaching the model
# ---------------------------------------------------------------------------


def test_create_project_is_refused_without_the_permission(world):
    """The obligation owner holds no project.create. The refusal is a row."""
    with session_scope() as session:
        owner = _actor(session, world["owner_id"])
        before = len(projects_for_company(session, COMPANY))
        result = chat_tools.create_project(
            session,
            company_id=COMPANY,
            actor=owner,
            name="Gas safety programme",
            jurisdiction="New York",
            docket=None,
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_NOT_PERMITTED
        assert "project.create" in result["reason"]
        assert len(projects_for_company(session, COMPANY)) == before
        assert "access.denied" in _actions(session, COMPANY)
        assert verify_chain(session, COMPANY) is True


def test_create_project_writes_when_permitted(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.create_project(
            session,
            company_id=COMPANY,
            actor=actor,
            name="Gas safety programme",
            jurisdiction="New York",
            docket="25-G-0007",
        )
        assert result["ok"] is True
        assert result["found"] == 1
        project = (
            session.query(Project)
            .filter(Project.company_id == COMPANY)
            .filter(Project.id == result["project"]["project_id"])
            .one()
        )
        assert project.docket_ref == "25-G-0007"
        assert project.owner == "Ana Ruiz"
        assert "project.created" in _actions(session, COMPANY)
        assert verify_chain(session, COMPANY) is True


def test_create_project_refuses_a_blank_name_without_raising(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.create_project(
            session,
            company_id=COMPANY,
            actor=actor,
            name="",
            jurisdiction="New York",
            docket=None,
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_BAD_ARGUMENT


def test_record_note_writes_a_turn_that_cites_nothing(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.record_note(
            session,
            company_id=COMPANY,
            actor=actor,
            project_id=world["mep"]["project_id"],
            text="Chase the December order with counsel.",
        )
        assert result["ok"] is True
        turn = (
            session.query(ResearchTurn)
            .filter(ResearchTurn.company_id == COMPANY)
            .one()
        )
        assert turn.body == "Chase the December order with counsel."
        assert turn.claim_id is None
        # A model-relayed note is not the analyst's own keystrokes.
        assert turn.author_kind != AUTHOR_ANALYST
        assert chat_tools.ACTION_CHAT_NOTE_RECORDED in _actions(session, COMPANY)
        assert verify_chain(session, COMPANY) is True


def test_the_note_text_is_not_copied_into_the_audit_chain(world):
    """The chain cannot be redacted. The note lives in the turn, once."""
    from app.state.models import AuditEvent

    secret = "the settlement number is 4.2 million"
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        chat_tools.record_note(
            session,
            company_id=COMPANY,
            actor=actor,
            project_id=world["mep"]["project_id"],
            text=secret,
        )
        rows = (
            session.query(AuditEvent).filter(AuditEvent.company_id == COMPANY).all()
        )
        for row in rows:
            assert secret not in (row.reason or "")
            assert secret not in (row.subject_id or "")


def test_two_notes_share_one_thread(world):
    from app.state.models import ResearchThread

    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        for text in ("first", "second"):
            chat_tools.record_note(
                session,
                company_id=COMPANY,
                actor=actor,
                project_id=world["mep"]["project_id"],
                text=text,
            )
        threads = (
            session.query(ResearchThread)
            .filter(ResearchThread.company_id == COMPANY)
            .all()
        )
        assert len(threads) == 1
        assert (
            session.query(ResearchTurn)
            .filter(ResearchTurn.company_id == COMPANY)
            .count()
            == 2
        )


def test_record_note_refuses_another_companys_project(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.record_note(
            session,
            company_id=COMPANY,
            actor=actor,
            project_id=world["rival"]["project_id"],
            text="should never land",
        )
        assert result["ok"] is False
        assert (
            session.query(ResearchTurn)
            .filter(ResearchTurn.company_id == RIVAL)
            .count()
            == 0
        )


def test_record_note_refuses_empty_text(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        result = chat_tools.record_note(
            session,
            company_id=COMPANY,
            actor=actor,
            project_id=world["mep"]["project_id"],
            text="   ",
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_BAD_ARGUMENT


def test_a_write_tool_cannot_be_reached_with_an_injected_actor(world):
    """The gate reads the injected actor, so the model must not be able to name one."""
    with session_scope() as session:
        owner = _actor(session, world["owner_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=owner,
            tool="create_project",
            arguments={
                "name": "Sneaky",
                "jurisdiction": "New York",
                "actor": world["analyst_id"],
            },
        )
        assert result["ok"] is False
        assert result["reason_code"] == chat_tools.REASON_IDENTITY_ARGUMENT
        assert projects_for_company(session, COMPANY) != []
        assert not [
            row
            for row in projects_for_company(session, COMPANY)
            if row.name == "Sneaky"
        ]


# ---------------------------------------------------------------------------
# 7. The chain survives everything above
# ---------------------------------------------------------------------------


def test_the_chain_verifies_after_a_mixed_run(world):
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="find_projects",
            arguments={"query": "", "company_id": RIVAL},
        )
        chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="no_such_tool",
            arguments={},
        )
        chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="create_project",
            arguments={"name": "Real", "jurisdiction": "New York"},
        )
        chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="record_note",
            arguments={"project_id": world["mep"]["project_id"], "text": "note"},
        )
        assert verify_chain(session, COMPANY) is True
        assert verify_chain(session, RIVAL) is True


def test_a_read_writes_nothing_to_the_chain(world):
    """Auditing every read fills the chain with rows nobody decided."""
    with session_scope() as session:
        actor = _actor(session, world["analyst_id"])
        before = event_count(session, COMPANY)
        for name, arguments in (
            ("find_projects", {"query": ""}),
            ("latest_changes", {}),
            ("change_detail", {"change_id": world["mep"]["change_id"]}),
            ("search_claims", {"query": "plan"}),
            ("open_escalations", {}),
        ):
            chat_tools.dispatch(
                session,
                company_id=COMPANY,
                actor=actor,
                tool=name,
                arguments=arguments,
            )
        assert event_count(session, COMPANY) == before


def test_policy_is_reached_through_the_module_not_a_bound_name():
    """A reloaded policy module must still be the gate this layer calls."""
    assert chat_tools.policy is policy
