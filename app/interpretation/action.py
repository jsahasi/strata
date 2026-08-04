"""Draft and final are separate code paths, decided before any model runs.

Per ADR-005 the status is read from the version's explicit field and used to
dispatch. The model is never asked which branch it is in, because that is the
distinction an analyst checks first when deciding whether to trust the tool.
"""

ACTION_MONITOR = "monitor"
ACTION_COMMENT = "comment"
ACTION_COMPLY = "comply"

_VOCABULARY = {
    "DRAFT": (ACTION_MONITOR, ACTION_COMMENT),
    "FINAL": (ACTION_COMPLY,),
}


def _checked(status: str) -> str:
    if status not in _VOCABULARY:
        raise ValueError(
            f"unknown version status {status!r}; expected DRAFT or FINAL. "
            "Refusing to default, because guessing this wrong is the most "
            "expensive error available in this domain."
        )
    return status


def action_vocabulary(status: str) -> tuple[str, ...]:
    """The only actions a change at this status may produce."""
    return _VOCABULARY[_checked(status)]


def requires_effective_date(status: str) -> bool:
    """A final order binds from a date. A draft does not bind at all."""
    return _checked(status) == "FINAL"
