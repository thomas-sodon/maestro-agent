"""Tests for :mod:`maestro.question_eviction`."""

from maestro.question_eviction import split_cap_eviction


def test_returns_post_save_questions_and_no_eviction() -> None:
    """When the save kept everything the evicted set is empty."""
    questions = ["[audit] alpha bravo?", "[limitation] charlie delta?"]
    after, evicted = split_cap_eviction(questions, questions)
    assert after == questions
    assert evicted == set()


def test_dropped_question_reported_as_evicted() -> None:
    """A question the sub-cap dropped surfaces as a normalized evicted key."""
    after, evicted = split_cap_eviction(
        ["[audit] alpha bravo?", "[audit] charlie delta?"],
        ["[audit] charlie delta?"],
    )
    assert after == ["[audit] charlie delta?"]
    assert evicted == {"[audit] alpha bravo?"}


def test_non_string_entries_ignored_on_both_sides() -> None:
    """Corrupt non-string rows are filtered out, never keyed or evicted."""
    after, evicted = split_cap_eviction(
        ["[audit] alpha bravo?", 7, None],
        [{"bad": 1}, "[audit] alpha bravo?"],
    )
    assert after == ["[audit] alpha bravo?"]
    assert evicted == set()
