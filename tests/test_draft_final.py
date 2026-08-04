import pytest

from app.interpretation.action import (
    ACTION_COMMENT,
    ACTION_COMPLY,
    ACTION_MONITOR,
    action_vocabulary,
    requires_effective_date,
)


def test_draft_offers_only_monitor_and_comment():
    assert action_vocabulary("DRAFT") == (ACTION_MONITOR, ACTION_COMMENT)


def test_final_offers_only_comply():
    assert action_vocabulary("FINAL") == (ACTION_COMPLY,)


def test_the_vocabularies_do_not_overlap():
    # Acting on a draft wastes money on something that may not survive comment.
    # Treating a final order as a draft misses a binding deadline. Neither word
    # may appear in both lists, or the two paths have started to converge.
    assert not set(action_vocabulary("DRAFT")) & set(action_vocabulary("FINAL"))


def test_only_final_requires_an_effective_date():
    assert requires_effective_date("FINAL") is True
    assert requires_effective_date("DRAFT") is False


def test_an_unknown_status_raises_rather_than_defaulting():
    # Defaulting would pick a branch silently, which is the exact error with
    # the highest cost in this domain.
    with pytest.raises(ValueError):
        action_vocabulary("PROPOSED")
    with pytest.raises(ValueError):
        requires_effective_date("")
