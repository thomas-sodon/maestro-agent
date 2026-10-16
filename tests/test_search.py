"""Tests for the BM25 search helpers in maestro.search."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import pytest

from maestro.search import (
    _FTS5_CREATE,
    _FTS5_TOKENIZE_SPEC,
    EphemeralJournalSearch,
    JournalSearchBackend,
    QueryCoverageReport,
    TermCoverage,
    _bm25_rank_fts5,
    assert_fts5_available,
    bm25_rank,
    query_term_coverage,
    tokenize,
)

# ── tokenize ─────────────────────────────────────────────────────────


def test_tokenize_lowercases_and_splits() -> None:
    assert tokenize("Hello, World!") == ["hello", "world"]


def test_tokenize_empty_string_returns_empty() -> None:
    assert tokenize("") == []


def test_tokenize_non_str_returns_empty() -> None:
    assert tokenize(None) == []
    assert tokenize(123) == []
    assert tokenize(["list"]) == []


def test_tokenize_collapses_punctuation() -> None:
    # Regex special chars must split cleanly without blowing up.
    assert tokenize("func(x, y)") == ["func", "x", "y"]
    assert tokenize("[critical]") == ["critical"]


def test_tokenize_preserves_digits_and_underscores() -> None:
    # \W+ keeps \w, which includes digits and underscores.
    assert tokenize("plan_42 has 3 steps") == ["plan_42", "has", "3", "steps"]


def test_tokenize_whitespace_only() -> None:
    assert tokenize("   \n\t  ") == []


# ── bm25_rank ────────────────────────────────────────────────────────


def test_bm25_rank_empty_corpus_returns_empty() -> None:
    assert bm25_rank([], "anything") == []


def test_bm25_rank_empty_query_returns_empty() -> None:
    assert bm25_rank(["hello world"], "") == []


def test_bm25_rank_whitespace_query_returns_empty() -> None:
    assert bm25_rank(["hello world"], "   ") == []


def test_bm25_rank_punctuation_only_query_returns_empty() -> None:
    # A query that tokenizes to [] (e.g., "!!!") produces no candidates.
    assert bm25_rank(["hello world"], "!!!") == []


def test_bm25_rank_no_match_returns_empty() -> None:
    docs = ["apple pie recipe", "banana bread tutorial"]
    assert bm25_rank(docs, "xyzzy") == []


def test_bm25_rank_single_match_small_corpus() -> None:
    # Classic N=2 edge case: BM25 IDF is zero when a term appears in
    # exactly one of two docs, so a naive score>0 filter would return
    # []. The set-intersection pre-filter guarantees the matching doc
    # is still returned.
    docs = ["I love learning about code", "The weather is nice"]
    assert bm25_rank(docs, "code") == [0]


def test_bm25_rank_ranks_multi_token_over_single_token() -> None:
    # With enough docs for positive IDFs, a doc containing both query
    # tokens should rank above a doc containing only one.
    docs = [
        "refactor the planner module urgently",  # both
        "refactor the authentication layer",      # one
        "the weather is nice today",              # none
        "update the docs for release notes",      # none
        "the cache layer needs a quick fix",      # none
    ]
    result = bm25_rank(docs, "refactor planner")
    assert result == [0, 1]


def test_bm25_rank_filters_non_string_docs() -> None:
    # Non-string "docs" tokenize to [] and are skipped by the
    # set-intersection filter.
    docs = ["hello world foo", None, "foo bar baz"]  # type: ignore[list-item]
    result = bm25_rank(docs, "foo")
    assert set(result) == {0, 2}


def test_bm25_rank_stable_tie_break_by_index() -> None:
    # Two docs with identical text get identical scores; the stable
    # tie-breaker should return them in original-index order.
    docs = ["same content here", "same content here"]
    result = bm25_rank(docs, "same")
    assert result == [0, 1]


def test_bm25_rank_candidate_filter_handles_empty_docs() -> None:
    # An empty-string doc must not crash the ranker's corpus statistics.
    docs = ["", "only real doc with words", ""]
    result = bm25_rank(docs, "real words")
    assert result == [1]


# ── ranker structural contract (FTS5) ───────────────────────────────

# (corpus, query, expected indices) — the shared bm25_rank ordering
# fixtures, pinning both the public ``bm25_rank`` entry point and the
# underlying ``_bm25_rank_fts5`` implementation to a known result on cases
# where the ordering is unambiguous.
_PARITY_CASES: list[tuple[list[str], str, list[int]]] = [
    ([], "anything", []),
    (["hello world"], "", []),
    (["hello world"], "   ", []),
    (["hello world"], "!!!", []),
    (["apple pie recipe", "banana bread tutorial"], "xyzzy", []),
    (["I love learning about code", "The weather is nice"], "code", [0]),
    (
        [
            "refactor the planner module urgently",  # both tokens
            "refactor the authentication layer",      # one token
            "the weather is nice today",              # none
            "update the docs for release notes",      # none
            "the cache layer needs a quick fix",      # none
        ],
        "refactor planner",
        [0, 1],
    ),
    (["same content here", "same content here"], "same", [0, 1]),
    (["", "only real doc with words", ""], "real words", [1]),
    # Digits and underscores survive the FTS5 tokenizer (tokenchars '_').
    (["plan_42 has 3 steps", "plan_99 done", "plan_42 again"], "plan_42", [2, 0]),
    # Accented word characters survive intact: FTS5's ``remove_diacritics 0``
    # matches the ``\W+``/``str.lower()`` tokenizer, which keeps accents.  An
    # ASCII ``naive`` query is a candidate only for the un-accented doc.
    # Without ``remove_diacritics 0`` FTS5 would fold ``naïve`` -> ``naive``
    # and wrongly include index 0 — so this case pins the candidate-set
    # behaviour the ASCII fixtures miss.
    (["naïve approach", "a naive method", "unrelated text"], "naive", [1]),
]


class TestSearchRankerParity:
    """The FTS5 ranker honours the structural contract on the shared fixtures.

    Patterned on ``tests/test_cost_tracking.py::TestBackendParity``: drive
    both the public ``bm25_rank`` entry point and its underlying
    ``_bm25_rank_fts5`` implementation over one shared fixture set and
    assert each returns the known index list.  These fixtures pin the
    *structural* contract the ranker honours — empty / whitespace /
    punctuation-only and no-match queries return ``[]``, docs sharing more
    query tokens sort ahead of docs sharing fewer, and ties break by
    original index — on cases where the ordering is unambiguous.
    """

    @pytest.mark.parametrize("corpus, query, expected", _PARITY_CASES)
    def test_bm25_rank_and_fts5_agree(
        self, corpus: Sequence[str], query: str, expected: list[int],
    ) -> None:
        assert bm25_rank(corpus, query) == expected
        assert _bm25_rank_fts5(corpus, query) == expected

    def test_bm25_rank_and_fts5_agree_on_non_string_docs(self) -> None:
        # Non-string docs tokenize to [] and are excluded.
        docs = ["hello world foo", None, "foo bar baz"]  # type: ignore[list-item]
        assert bm25_rank(docs, "foo") == [0, 2]
        assert _bm25_rank_fts5(docs, "foo") == [0, 2]


# ── journal-search backend seam ──────────────────────────────────────


def test_fts5_tokenize_spec_is_shared_by_the_ephemeral_ddl() -> None:
    """The extracted tokenizer spec still composes the ephemeral ``_FTS5_CREATE``.

    The persistent ``journal_search`` table reuses ``_FTS5_TOKENIZE_SPEC``
    verbatim, so pinning that the ephemeral DDL is built from the same constant
    guards against a drift that would silently desync the two rankings.
    """
    assert _FTS5_TOKENIZE_SPEC == "unicode61 remove_diacritics 0 tokenchars '_'"
    assert f'tokenize = "{_FTS5_TOKENIZE_SPEC}"' in _FTS5_CREATE


def test_ephemeral_journal_search_satisfies_the_protocol() -> None:
    """The default adapter is a structural :class:`JournalSearchBackend`."""
    assert isinstance(EphemeralJournalSearch(), JournalSearchBackend)


class TestEphemeralJournalSearchParity:
    """The default adapter *is* ``bm25_rank`` — identical on the shared fixtures.

    :class:`EphemeralJournalSearch` is the injectable-seam wrapper the un-injected
    :class:`~maestro.journal.Journal` keeps, so it must return byte-identical
    orders to the ``bm25_rank`` it delegates to across every parity fixture,
    proving the seam is a no-op for existing callers.
    """

    @pytest.mark.parametrize("corpus, query, expected", _PARITY_CASES)
    def test_ephemeral_matches_bm25_rank(
        self, corpus: Sequence[str], query: str, expected: list[int],
    ) -> None:
        assert EphemeralJournalSearch().rank(corpus, query) == expected
        assert EphemeralJournalSearch().rank(corpus, query) == bm25_rank(corpus, query)

    def test_ephemeral_matches_bm25_rank_on_non_string_docs(self) -> None:
        docs = ["hello world foo", None, "foo bar baz"]  # type: ignore[list-item]
        assert EphemeralJournalSearch().rank(docs, "foo") == [0, 2]


# ── assert_fts5_available ────────────────────────────────────────────


def test_assert_fts5_available_succeeds_with_fts5() -> None:
    """CPython's bundled sqlite3 ships FTS5, so the check passes silently."""
    assert assert_fts5_available() is None


def test_assert_fts5_available_raises_without_fts5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SQLite build lacking FTS5 surfaces a clear ``RuntimeError``."""

    class _NoFTS5Conn:
        def execute(self, *args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("no such module: fts5")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "maestro.search.sqlite3.connect", lambda *a, **k: _NoFTS5Conn()
    )
    with pytest.raises(RuntimeError, match="FTS5 extension is unavailable"):
        assert_fts5_available()


# ── query_term_coverage ─────────────────────────────────────────────


def test_query_term_coverage_basic() -> None:
    corpus = [
        "the cat sat on the mat",
        "the dog chased the cat",
        "birds fly in the sky",
    ]
    report = query_term_coverage("cat dog", corpus)

    assert isinstance(report, QueryCoverageReport)
    assert report.total_terms == 2
    assert report.corpus_size == 3
    assert report.zero_match_terms == 0

    by_term = {tc.term: tc for tc in report.terms}
    assert by_term["cat"].document_frequency == 2
    assert by_term["dog"].document_frequency == 1
    # IDF should be higher for rarer term
    assert by_term["dog"].idf > by_term["cat"].idf


def test_query_term_coverage_zero_match_term() -> None:
    corpus = ["hello world", "foo bar"]
    report = query_term_coverage("hello xyzzy", corpus)

    assert report.total_terms == 2
    assert report.zero_match_terms == 1
    by_term = {tc.term: tc for tc in report.terms}
    assert by_term["hello"].document_frequency == 1
    assert by_term["xyzzy"].document_frequency == 0
    assert by_term["xyzzy"].idf == 0.0


def test_query_term_coverage_empty_query() -> None:
    report = query_term_coverage("", ["hello world"])
    assert report.total_terms == 0
    assert report.zero_match_terms == 0
    assert report.corpus_size == 1
    assert report.terms == []


def test_query_term_coverage_empty_corpus() -> None:
    report = query_term_coverage("hello", [])
    assert report.total_terms == 1
    assert report.zero_match_terms == 1
    assert report.corpus_size == 0
    assert report.terms[0].term == "hello"
    assert report.terms[0].document_frequency == 0
    assert report.terms[0].idf == 0.0


def test_query_term_coverage_deduplicates_query_tokens() -> None:
    corpus = ["alpha beta gamma"]
    report = query_term_coverage("alpha alpha beta", corpus)
    # "alpha" appears twice in query but should be collapsed to one entry.
    assert report.total_terms == 2
    terms = [tc.term for tc in report.terms]
    assert terms == ["alpha", "beta"]


def test_query_term_coverage_handles_empty_docs() -> None:
    # Empty docs should not crash (sentinel safeguard).
    corpus = ["", "hello world", ""]
    report = query_term_coverage("hello", corpus)
    assert report.corpus_size == 3
    assert report.total_terms == 1
    assert report.terms[0].document_frequency == 1


def test_query_term_coverage_idf_from_bm25() -> None:
    # Verify the Okapi IDF weighting: with 5 docs where "common" appears in
    # 4, its IDF should be low or epsilon-floored; "rare" in 1 should be high.
    corpus = [
        "common word here",
        "common word there",
        "common stuff again",
        "common plus rare",
        "nothing relevant",
    ]
    report = query_term_coverage("common rare", corpus)
    by_term = {tc.term: tc for tc in report.terms}

    assert by_term["common"].document_frequency == 4
    assert by_term["rare"].document_frequency == 1
    # The Okapi IDF floors negative values to eps*average_idf, so common
    # should still have a positive (but small) IDF; rare should be
    # strictly larger.
    assert by_term["rare"].idf > by_term["common"].idf


def test_query_term_coverage_punctuation_only_query() -> None:
    report = query_term_coverage("!!!", ["hello"])
    assert report.total_terms == 0
    assert report.terms == []


def test_query_term_coverage_returns_frozen_dataclasses() -> None:
    report = query_term_coverage("test", ["test doc"])
    assert isinstance(report.terms[0], TermCoverage)
    # Frozen — should not allow attribute assignment.
    try:
        report.terms[0].term = "changed"  # type: ignore[misc]
        assert False, "Expected FrozenInstanceError"  # noqa: B011
    except AttributeError:
        pass


def test_query_term_coverage_structure_fields_match_implementation() -> None:
    """Verify every field on both dataclasses is present and correctly typed."""
    corpus = ["alpha beta", "gamma delta", "alpha gamma"]
    report = query_term_coverage("alpha missing", corpus)

    # QueryCoverageReport fields
    assert isinstance(report.terms, list)
    assert isinstance(report.total_terms, int)
    assert isinstance(report.zero_match_terms, int)
    assert isinstance(report.corpus_size, int)

    # TermCoverage fields — one hit, one miss
    hit = report.terms[0]
    miss = report.terms[1]
    assert isinstance(hit, TermCoverage)
    assert isinstance(hit.term, str)
    assert isinstance(hit.document_frequency, int)
    assert isinstance(hit.idf, float)
    assert isinstance(miss, TermCoverage)
    assert isinstance(miss.term, str)
    assert isinstance(miss.document_frequency, int)
    assert isinstance(miss.idf, float)

    # Consistency: total_terms == len(terms)
    assert report.total_terms == len(report.terms)
    # Consistency: zero_match_terms counts only df==0 entries
    actual_zeros = sum(1 for tc in report.terms if tc.document_frequency == 0)
    assert report.zero_match_terms == actual_zeros


def test_query_term_coverage_report_frozen() -> None:
    """QueryCoverageReport itself is frozen (immutable scalars)."""
    report = query_term_coverage("x", ["x y"])
    try:
        report.total_terms = 99  # type: ignore[misc]
        assert False, "Expected FrozenInstanceError"  # noqa: B011
    except AttributeError:
        pass


def test_query_term_coverage_whitespace_only_query() -> None:
    """Whitespace-only query tokenizes to nothing → empty stats."""
    report = query_term_coverage("   \t\n  ", ["hello world"])
    assert report.total_terms == 0
    assert report.zero_match_terms == 0
    assert report.corpus_size == 1
    assert report.terms == []


def test_query_term_coverage_single_term_partial_match() -> None:
    """Single query term that matches some but not all docs.

    BM25 IDF is zero when a term appears in exactly half the corpus
    (log(N-n+0.5) - log(n+0.5) = 0 when n = N/2), so we use a 5-doc
    corpus where 'brown' appears in 2 docs (< half) to get a positive IDF.
    """
    corpus = [
        "the quick brown fox",
        "lazy brown dog",
        "blue sky above",
        "green grass below",
        "red leaves falling",
    ]
    report = query_term_coverage("brown", corpus)

    assert report.total_terms == 1
    assert report.zero_match_terms == 0
    assert report.corpus_size == 5

    tc = report.terms[0]
    assert tc.term == "brown"
    assert tc.document_frequency == 2
    # In fewer than half the docs → IDF is strictly positive.
    assert tc.idf > 0.0


def test_query_term_coverage_multi_term_all_miss() -> None:
    """Every query term misses every corpus document."""
    corpus = ["apple pie", "banana bread", "cherry cake"]
    report = query_term_coverage("xyzzy plugh", corpus)

    assert report.total_terms == 2
    assert report.zero_match_terms == 2
    assert report.corpus_size == 3
    for tc in report.terms:
        assert tc.document_frequency == 0
        assert tc.idf == 0.0


def test_query_term_coverage_multi_term_all_hit() -> None:
    """Every query term matches at least one corpus document."""
    corpus = [
        "alpha beta gamma",
        "beta gamma delta",
        "gamma delta epsilon",
    ]
    report = query_term_coverage("alpha beta gamma", corpus)

    assert report.total_terms == 3
    assert report.zero_match_terms == 0
    by_term = {tc.term: tc for tc in report.terms}
    assert by_term["alpha"].document_frequency == 1
    assert by_term["beta"].document_frequency == 2
    assert by_term["gamma"].document_frequency == 3


def test_query_term_coverage_non_string_corpus_entries() -> None:
    """Non-string corpus entries (None, int, list) should not crash."""
    corpus = ["hello world", None, 42, ["list"], "foo bar hello"]  # type: ignore[list-item]
    report = query_term_coverage("hello", corpus)

    # corpus_size counts all entries, even non-string ones.
    assert report.corpus_size == 5
    assert report.total_terms == 1
    # "hello" appears in doc 0 and doc 4 only; non-strings tokenize to [].
    assert report.terms[0].document_frequency == 2
    assert report.terms[0].idf > 0.0


def test_query_term_coverage_idf_positive_for_partial_coverage() -> None:
    """IDF must be strictly positive for a term appearing in fewer than all docs."""
    corpus = [
        "search query optimization",
        "database index tuning",
        "search engine ranking",
        "web server configuration",
        "search api design",
    ]
    report = query_term_coverage("search database", corpus)
    by_term = {tc.term: tc for tc in report.terms}

    # "search" in 3/5 docs, "database" in 1/5 docs — both < all docs.
    assert by_term["search"].idf > 0.0
    assert by_term["database"].idf > 0.0


def test_query_term_coverage_term_in_every_doc_idf_floored() -> None:
    """A term present in every doc gets a negative raw IDF that the Okapi
    IDF floors to eps * average_idf — still positive but very small."""
    corpus = [
        "the quick brown fox",
        "the lazy brown dog",
        "the bright blue sky",
    ]
    report = query_term_coverage("the fox", corpus)
    by_term = {tc.term: tc for tc in report.terms}

    # "the" appears in all 3 docs — IDF should be epsilon-floored (positive).
    assert by_term["the"].document_frequency == 3
    assert by_term["the"].idf > 0.0
    # "fox" appears in 1/3 docs — IDF should be strictly higher.
    assert by_term["fox"].document_frequency == 1
    assert by_term["fox"].idf > by_term["the"].idf


def test_query_term_coverage_single_doc_corpus() -> None:
    """Edge case: corpus with exactly one document."""
    report = query_term_coverage("hello missing", ["hello world"])

    assert report.corpus_size == 1
    assert report.total_terms == 2
    assert report.zero_match_terms == 1

    by_term = {tc.term: tc for tc in report.terms}
    assert by_term["hello"].document_frequency == 1
    assert by_term["missing"].document_frequency == 0
    assert by_term["missing"].idf == 0.0


def test_query_term_coverage_case_insensitive() -> None:
    """Query and corpus tokens are lowercased before matching."""
    corpus = ["Hello World", "HELLO AGAIN"]
    report = query_term_coverage("HELLO", corpus)

    assert report.total_terms == 1
    tc = report.terms[0]
    assert tc.term == "hello"  # lowercased
    assert tc.document_frequency == 2


def test_query_term_coverage_term_order_preserved() -> None:
    """Terms appear in first-occurrence order from the query."""
    corpus = ["a b c d e"]
    report = query_term_coverage("delta charlie bravo alpha", corpus)

    terms = [tc.term for tc in report.terms]
    assert terms == ["delta", "charlie", "bravo", "alpha"]


def test_query_term_coverage_dedup_preserves_first_occurrence_order() -> None:
    """When duplicates exist, the first occurrence position wins."""
    corpus = ["x y z"]
    report = query_term_coverage("z y z x y x", corpus)

    terms = [tc.term for tc in report.terms]
    # First-seen order: z, y, x
    assert terms == ["z", "y", "x"]
    assert report.total_terms == 3


def test_query_term_coverage_empty_query_empty_corpus() -> None:
    """Both query and corpus empty — fully degenerate case."""
    report = query_term_coverage("", [])
    assert report.total_terms == 0
    assert report.zero_match_terms == 0
    assert report.corpus_size == 0
    assert report.terms == []


def test_query_term_coverage_corpus_all_empty_strings() -> None:
    """Corpus of only empty-string docs (sentinel safeguard exercised)."""
    corpus = ["", "", ""]
    report = query_term_coverage("anything", corpus)

    assert report.corpus_size == 3
    assert report.total_terms == 1
    assert report.zero_match_terms == 1
    assert report.terms[0].document_frequency == 0
    assert report.terms[0].idf == 0.0


def test_query_term_coverage_mixed_hit_miss_summary() -> None:
    """Multi-term query with a deliberate mix of hits and misses — verify
    zero_match_terms counts exactly the miss terms."""
    corpus = [
        "alpha bravo charlie",
        "bravo delta echo",
        "echo foxtrot golf",
    ]
    # alpha(1 doc), bravo(2 docs), zzz(0 docs), qqq(0 docs)
    report = query_term_coverage("alpha bravo zzz qqq", corpus)

    assert report.total_terms == 4
    assert report.zero_match_terms == 2
    by_term = {tc.term: tc for tc in report.terms}
    assert by_term["alpha"].document_frequency == 1
    assert by_term["bravo"].document_frequency == 2
    assert by_term["zzz"].document_frequency == 0
    assert by_term["qqq"].document_frequency == 0
