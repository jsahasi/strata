"""The suggested actions under a reply. These are the product, not decoration.

WHY THEY CARRY THE WEIGHT. The persona caps a reply at two or three sentences
and pushes elaboration into the next turn. That only works if the next turn is
one tap away, so the pills are where the depth lives: after "three changes
landed" the person wants the newest change, its owner, and the review queue,
and they should not have to know how to ask for them.

THREE RULES, and each one is a failure this file exists to stop.

REACHABLE. A pill is a button. Offering an action the person's permissions
refuse produces a refusal they did not ask for and cannot fix, and it teaches
them the product is broken rather than that they lack a grant. Every pill that
needs authority names the permission code, and the code is checked against
PERMISSION_CODES at import: a pill gated on a permission the product does not
define would be hidden from everyone for ever, silently.

DERIVED. Pills come from the tool results in hand, not from a fixed menu.
A fixed menu is the same three suggestions after every answer, which reads as a
template and is ignored by the second turn. The fixed sets below are for the two
moments where there is nothing to derive from -- the opening turn and a refusal
-- and for topping a thin turn up to the floor.

BOUNDED. Three to five, always, including on a refusal. Fewer than three is a
dead end; more than five is a menu nobody reads, and on a phone it is a wall.

WHAT THIS MODULE IS NOT. It holds no tone and no rules. app/chat/persona.py owns
what Clarke is and how it speaks, including the pills a refusal offers, and this
file takes those from the screen rather than writing its own. There is one place
where the voice is defined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.state.models import PERMISSION_CODES

# Three is the floor because two suggestions look like the end of the road.
# Five is the ceiling because the reply above them is three sentences and the
# pair has to fit one phone screen without scrolling.
MIN_PILLS = 3
MAX_PILLS = 5

# How many pills one tool result may contribute. Without a cap, a search that
# matched nine projects would fill the tray with nine near-identical buttons and
# push out the escalation pill that actually matters.
PER_RESULT = 2


@dataclass(frozen=True)
class Pill:
    """One suggested next turn.

    label is what the person reads; prompt is what gets sent as the next
    message. They differ on purpose: "Who owns it?" is a good button and a bad
    question, because by the time it arrives the model no longer knows what
    "it" was. The prompt carries the subject.
    """

    label: str
    prompt: str
    #: A code from PERMISSION_CODES, or "" for a pill anyone signed in may take.
    requires: str = ""

    def as_dict(self) -> dict[str, str]:
        """The wire shape. requires is ours and never leaves the server."""
        return {"label": self.label, "prompt": self.prompt}


@dataclass(frozen=True)
class Consulted:
    """One tool call and what came back, as the pill layer needs to see it.

    Declared here rather than imported from the agent so that this module has no
    dependency on the turn loop and can be exercised on its own. The agent fills
    it in.

    withheld is None when the tool cannot withhold anything, and an integer when
    it can -- including zero. None and zero are different facts: one means the
    question does not arise, the other means somebody counted and the answer was
    nothing. Folding them would let a tool that forgot to count look like a tool
    that found nothing to withhold.
    """

    tool: str
    found: int
    withheld: int | None = None
    result: Mapping[str, Any] | None = None


# --------------------------------------------------------------------------- #
# The fixed sets                                                              #
# --------------------------------------------------------------------------- #

# Withholding outranks everything else in the tray. When the product has refused
# to assert something, the two questions worth asking are why, and who can
# resolve it -- and those are exactly the two a person will not think to type.
WITHHELD_REASON = Pill(
    "Why was it withheld?",
    "Why was that claim withheld, and what did its citation fail on?",
)
REVIEW_QUEUE = Pill(
    "Open escalations",
    "Show the open escalations and who is holding each one.",
)

LATEST_CHANGES = Pill(
    "What changed this week?",
    "What changed in the last seven days, newest first?",
)
MY_OPEN_ITEMS = Pill(
    "My open items",
    "Show the escalations and approvals waiting on me.",
)
FIND_PROJECT = Pill(
    "Find a project",
    "Find a project by name, jurisdiction or docket.",
)
WHO_OWNS_WHAT = Pill(
    "Who owns what?",
    "Show the obligations and who owns each one.",
)
START_PROJECT = Pill(
    "Start a project",
    "Start a project for a new docket.",
    requires="project.create",
)
# GATED, and that follows the tool rather than leading it. This pill was ungated
# because record_note was, and record_note was ungated because the product
# defines no note.write code. An ungated write reachable from a chat panel was
# the wrong shape; app/chat/tools.py now gates it on knowledge.write and says at
# PERMISSION_NOTE_WRITE why that is an alias and what the alias costs. The pill
# moves with it in the same change: a pill gated looser than its tool offers a
# button that refuses, and one gated tighter hides a button from people entitled
# to press it. tests/test_chat_agent.py pins the two together, so this constant
# cannot drift from the gate without turning that test red.
RECORD_NOTE = Pill(
    "Record a note",
    "Record a note on this project. A note, not an approval.",
    requires="knowledge.write",
)

#: What a turn falls back to when there is nothing to derive from: the opening
#: turn, and any turn whose tools returned nothing worth drilling into. Ordered,
#: because the first three are what a thin turn will actually show.
OPENING_PILLS: tuple[Pill, ...] = (
    LATEST_CHANGES,
    MY_OPEN_ITEMS,
    FIND_PROJECT,
    WHO_OWNS_WHAT,
    START_PROJECT,
)

# Every pill this module can emit, so the import-time check below covers all of
# them rather than the ones somebody remembered to list.
ALL_PILLS: tuple[Pill, ...] = (
    WITHHELD_REASON,
    REVIEW_QUEUE,
    LATEST_CHANGES,
    MY_OPEN_ITEMS,
    FIND_PROJECT,
    WHO_OWNS_WHAT,
    START_PROJECT,
    RECORD_NOTE,
)

# Loud at import beats quiet for ever, the same argument app/auth/policy.py
# makes about its own permission constant. A pill gated on a code the product
# does not define is hidden from every user, and nothing on the screen or in the
# log would say why -- it would simply never appear.
_unknown = sorted({p.requires for p in ALL_PILLS if p.requires and p.requires not in PERMISSION_CODES})
if _unknown:
    raise ValueError(
        f"these pills are gated on codes the product does not define: {_unknown}. "
        f"The codes are {', '.join(PERMISSION_CODES)}. A pill checked against a "
        "code nobody grants is a button nobody can ever see."
    )


# --------------------------------------------------------------------------- #
# Deriving pills from what the turn actually did                              #
# --------------------------------------------------------------------------- #


def _label_for(item: Any) -> str:
    """A short name for one result row, or "" when the row will not say.

    Never invents one. A pill labelled with an id the person has never seen is
    worse than one fewer pill.
    """
    if isinstance(item, Mapping):
        for key in ("name", "title", "label", "summary"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:48]
        for key in ("id", "change_id", "project_id", "escalation_id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:48]
    return ""


def _rows(result: Mapping[str, Any] | None, *keys: str) -> list[Any]:
    """The first list-valued entry under one of these keys."""
    if not isinstance(result, Mapping):
        return []
    for key in keys:
        value = result.get(key)
        if isinstance(value, (list, tuple)) and value:
            return list(value)
    return []


def _from_projects(consulted: Consulted) -> list[Pill]:
    out: list[Pill] = []
    for item in _rows(consulted.result, "projects", "items", "results")[:PER_RESULT]:
        label = _label_for(item)
        if label:
            out.append(Pill(f"Open {label}", f"Open the project {label}."))
    if not out:
        out.append(FIND_PROJECT)
    return out


def _from_changes(consulted: Consulted) -> list[Pill]:
    out: list[Pill] = []
    for item in _rows(consulted.result, "changes", "items", "results")[:1]:
        label = _label_for(item)
        if label:
            out.append(Pill(f"Open {label}", f"Show the diff for change {label}."))
    out.append(
        Pill("Who owns it?", "Who owns the obligation behind the newest change?")
    )
    out.append(Pill("Where is it in the route?", "Where is that item in its approval route?"))
    return out


def _from_change_detail(consulted: Consulted) -> list[Pill]:
    return [
        Pill("Who owns it?", "Who owns the obligation this change touches?"),
        Pill("Where is it in the route?", "Where is this change in its approval route?"),
    ]


def _from_project_detail(consulted: Consulted) -> list[Pill]:
    return [
        LATEST_CHANGES,
        REVIEW_QUEUE,
        RECORD_NOTE,
    ]


def _from_escalations(consulted: Consulted) -> list[Pill]:
    out: list[Pill] = []
    for item in _rows(consulted.result, "escalations", "items", "results")[:1]:
        label = _label_for(item)
        if label:
            out.append(Pill(f"Open {label}", f"Show the escalation {label} and why it is held."))
    out.append(Pill("Who holds them?", "Who is holding each open escalation?"))
    return out


def _from_owner(consulted: Consulted) -> list[Pill]:
    return [
        Pill("What changed for them?", "What changed against the obligations they own?"),
        Pill("Where is it in the route?", "Where is that item in its approval route?"),
    ]


def _from_route(consulted: Consulted) -> list[Pill]:
    return [
        Pill("Who owns it?", "Who owns the obligation behind this item?"),
        REVIEW_QUEUE,
    ]


def _from_claims(consulted: Consulted) -> list[Pill]:
    return [
        Pill("Show the source", "Show the passage each of those claims cites."),
        REVIEW_QUEUE,
    ]


#: The one reason code on a coverage map row that means "searched, and it holds
#: nothing matching". Spelled here rather than imported because this module
#: deliberately depends on nothing but the result dicts it is handed -- and it is
#: pinned against app/state/search.py by tests/test_coverage_map.py, so the two
#: cannot drift without a test going red.
COVERAGE_SILENT = "NO_PASSAGE_MATCHED"


def _from_coverage(consulted: Consulted) -> list[Pill]:
    """The silence first, because it is the half nobody thinks to ask about.

    A coverage map answers two questions at once and a reader takes the loud one:
    they read the filings that carry the words and skim past the ones that do
    not. The filings that carry nothing are the reason the tool exists, so the
    first pill sends the next turn straight at them.

    IT BRANCHES ON THE REASON, NEVER ON AN EMPTY SPAN LIST, and that distinction
    is the whole reason this function is careful. A coverage map has FOUR kinds
    of row with no spans -- searched and nothing matched; no stored passages to
    search; never searched; matched and then not handed over -- and only the
    first is silence. This offered "Which filings are silent?" over an empty span
    list for one afternoon, which meant that a filing whose matching passage was
    refused for carrying a withheld claim's citation was labelled silent, on a
    button, in the only layer a person actually reads. app/state/search.py spends
    three reason codes and sixty lines of comment keeping those four apart; a
    pill that reads `if not spans` throws all of it away at the last step.
    """
    rows = [
        item
        for item in _rows(consulted.result, "versions", "items", "results")
        if isinstance(item, Mapping)
    ]
    silent = [item for item in rows if item.get("reason") == COVERAGE_SILENT]

    out: list[Pill] = []
    if silent:
        out.append(
            Pill(
                "Which filings are silent?",
                "Which of those filings carry none of those words, and what do "
                "they cover instead?",
            )
        )
        # Named, because "which filings are silent" is a question somebody has to
        # reformulate and "open the Staff report" is a button. Only ever a row
        # this result says is silent.
        for item in silent:
            label = _label_for(item)
            if label:
                out.append(Pill(f"Open {label}", f"Show what the {label} covers."))
                break

    if any(item.get("spans") for item in rows):
        out.append(
            Pill("Show the source", "Show the passage each of those filings cites.")
        )
    return out


def _from_write(consulted: Consulted) -> list[Pill]:
    return [LATEST_CHANGES, REVIEW_QUEUE]


#: Tool name to the pills its result suggests. The names are the chat tool
#: vocabulary in the turn contract; a tool this table has never heard of simply
#: contributes nothing, which costs a suggestion rather than an exception.
_BY_TOOL = {
    "find_projects": _from_projects,
    "project_detail": _from_project_detail,
    "latest_changes": _from_changes,
    "change_detail": _from_change_detail,
    "open_escalations": _from_escalations,
    "obligation_owner": _from_owner,
    "approval_route": _from_route,
    "search_claims": _from_claims,
    "coverage_map": _from_coverage,
    "create_project": _from_write,
    "record_note": _from_write,
}


def from_results(consulted: Sequence[Consulted]) -> tuple[Pill, ...]:
    """Pills for what this turn actually found. Withholding leads.

    A tool that found nothing contributes nothing: "open the project I just told
    you does not exist" is a worse suggestion than none, and choose() will top
    the turn up from the opening set instead.
    """
    out: list[Pill] = []

    if any((item.withheld or 0) > 0 for item in consulted):
        out.append(WITHHELD_REASON)
        out.append(REVIEW_QUEUE)

    for item in consulted:
        if item.found <= 0:
            continue
        builder = _BY_TOOL.get(item.tool)
        if builder is None:
            continue
        out.extend(builder(item)[:3])

    return tuple(out)


def from_screen(labels: Iterable[str]) -> tuple[Pill, ...]:
    """The screen's pills, as pills. The wording is the persona's, not ours.

    persona.screen returns plain strings because it holds no view model. They
    are written as things a person would type, so label and prompt are the same
    string and neither is rephrased here -- a second wording of a refusal is a
    second voice.
    """
    return tuple(Pill(label=text, prompt=text) for text in labels if text)


def choose(
    candidates: Sequence[Pill],
    *,
    held: frozenset[str] | set[str] | None = None,
    floor: Sequence[Pill] = OPENING_PILLS,
) -> list[dict[str, str]]:
    """Filter to what this person can actually do, top up, cap, and serialise.

    The order of the three steps matters. Filtering after topping up would let
    an unreachable pill take a slot and then vanish, leaving a turn with two
    suggestions; filtering first means the floor is measured against what will
    really be shown.

    How far to top up depends on whether the turn found anything. With derived
    pills in hand the floor only fills to the minimum, because a generic
    suggestion beside a specific one dilutes it. With nothing derived -- the
    opening turn, or a turn whose tools found nothing -- the floor IS the offer
    and it runs to the ceiling. Without that split the last two entries of the
    opening set could never be shown to anyone, which is how a permissioned pill
    ends up dead code that no test notices.
    """
    held = frozenset(held or ())

    picked: list[Pill] = []
    seen: set[str] = set()

    def add(pill: Pill) -> None:
        if pill.requires and pill.requires not in held:
            return
        key = pill.label.casefold()
        if key in seen:
            return
        seen.add(key)
        picked.append(pill)

    for pill in candidates:
        if len(picked) >= MAX_PILLS:
            break
        add(pill)

    target = MIN_PILLS if picked else MAX_PILLS
    for pill in floor:
        if len(picked) >= target:
            break
        add(pill)

    return [pill.as_dict() for pill in picked[:MAX_PILLS]]


__all__ = [
    "ALL_PILLS",
    "MAX_PILLS",
    "MIN_PILLS",
    "OPENING_PILLS",
    "Consulted",
    "Pill",
    "choose",
    "from_results",
    "from_screen",
]
