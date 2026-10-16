"""BM25 ranking helpers shared by Journal and Memory search.

Ranks free-text search over the journal and evolution-plans corpora
through SQLite FTS5's native ``bm25()``.  A simple lowercase + ``\\W+``
tokenizer feeds both the query and the corpus, so the two see an identical
token stream.  No stemming, no stopwords.  The evolution-plans corpus is
bounded by dreaming/review cycles, so rebuilding its ephemeral ``:memory:``
FTS5 index per call is fine; the journal corpus is deliberately unbounded
(``journal.jsonl`` is never archived), so under ``[paths]
sqlite_state_enabled`` its ranking may instead route through a persistent,
incrementally-maintained ``state.db`` index (the :class:`JournalSearchBackend`
seam → ``maestro.state_store.SqliteJournalSearchIndex``) that tops itself up
rather than rebuilding per call.  Default-off, that persistent index is absent
and both corpora use the per-call ephemeral rebuild (proposal 106).

FTS5 is a hard requirement (proposal 106): :func:`assert_fts5_available`
is called once at startup (from ``cli.main``) so a build whose bundled
SQLite lacks the extension fails loudly at construction rather than
mid-loop inside a journal/plan search.

Public API:

- :func:`tokenize` — lowercase + non-word split; returns ``[]`` for
  non-string or empty input.
- :func:`bm25_rank` — rank a corpus by relevance to a query, returning
  indices sorted descending by BM25 score.  Docs that contain *none*
  of the query tokens are filtered out before ranking, so callers get
  sensible "no match → empty list" behaviour even on small corpora
  where the BM25 IDF can legitimately return zero for a matching doc.
- :func:`assert_fts5_available` — fail-loud startup check that the bundled
  SQLite provides the FTS5 extension ``bm25_rank`` depends on.
- :class:`JournalSearchBackend` / :class:`EphemeralJournalSearch` — the
  injectable journal-ranker seam and its default per-call adapter; the
  persistent ``state.db`` alternative lives in ``maestro.state_store``.
- :func:`query_term_coverage` — diagnostic: tokenize a query and return
  per-term coverage stats (document frequency + Okapi IDF weight) plus an
  overall summary.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_TOKEN_SPLIT = re.compile(r"\W+")

# Sentinel token substituted for empty docs so the corpus statistics span
# the same N documents even when a doc tokenizes to nothing.  Chosen to be
# a string that can never appear in a natural-language tokenize() output
# because natural tokenize() never emits leading/trailing underscores on
# its own — every real token comes from a \W+ split so underscores are
# preserved but the leading double-underscore marker here is deliberately
# unique.
_EMPTY_DOC_SENTINEL = "__maestro_empty_doc__"

# FTS5 tokenizer spec (proposal 106).  ``unicode61`` keeps alphanumerics
# as token characters and, with ``tokenchars '_'``, underscores too — so
# a token like ``plan_42`` survives whole, matching the ``\W+`` tokenizer
# (which treats ``_`` as a word char).  ``remove_diacritics 0`` disables
# unicode61's default (``remove_diacritics 1``) diacritic-folding, which
# would otherwise strip accents from Latin-script word characters (indexing
# ``naïve`` as the token ``naive``).  The ``\W+``/``str.lower()`` tokenizer
# treats accented letters as word chars and preserves them, so *without*
# ``remove_diacritics 0`` a query and its corpus would diverge on the
# *candidate set*: a doc containing ``naïve`` would MATCH an ASCII ``naive``
# query under FTS5 but not under the token-intersection the tokenizer
# implies (``naïve`` != ``naive``).  Documents are pre-tokenized with
# :func:`tokenize` and re-joined by spaces before insertion, so with
# diacritic-folding disabled query and corpus see the same accent-preserving
# token stream for the word characters these natural-language corpora
# contain.
#: The tokenizer clause shared by the ephemeral ``:memory:`` ranker's
#: :data:`_FTS5_CREATE` and the *persistent* ``journal_search`` FTS5 table in
#: :mod:`maestro.state_store` (proposal 106).  Extracted so the persistent index
#: tokenizes byte-for-byte identically to the ephemeral one — any drift here
#: would silently desync the two rankings the parity tests pin.
_FTS5_TOKENIZE_SPEC = "unicode61 remove_diacritics 0 tokenchars '_'"

_FTS5_CREATE = (
    "CREATE VIRTUAL TABLE t USING fts5("
    f'content, tokenize = "{_FTS5_TOKENIZE_SPEC}")'
)


def tokenize(text: object) -> list[str]:
    """Lowercase ``text`` and split on non-word characters.

    Empty tokens are dropped.  Non-string input (including ``None``)
    returns an empty list so callers do not need their own defensive
    ``isinstance`` guards.
    """
    if not isinstance(text, str) or not text:
        return []
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def bm25_rank(corpus: Sequence[str], query: str) -> list[int]:
    """Rank documents in ``corpus`` by BM25 relevance to ``query``.

    Returns indices into ``corpus`` sorted by descending BM25 score, ranked
    by SQLite FTS5's native ``bm25()`` over an ephemeral ``:memory:`` index
    (:func:`_bm25_rank_fts5`).  Documents that share no tokens with the
    query are excluded; ties are broken by original index so the ranking is
    stable.

    Returns an empty list when:

    - ``query`` is empty / whitespace / punctuation-only,
    - ``corpus`` is empty,
    - no document in ``corpus`` contains any token from ``query``.

    Fails loud on an interpreter whose bundled SQLite lacks the FTS5
    extension: the ``CREATE VIRTUAL TABLE`` raises
    :class:`sqlite3.OperationalError`.  :func:`assert_fts5_available`
    surfaces that at startup instead.
    """
    return _bm25_rank_fts5(corpus, query)


def _bm25_rank_fts5(corpus: Sequence[str], query: str) -> list[int]:
    """Rank via SQLite FTS5's native ``bm25()`` over an ephemeral index.

    Builds a one-shot ``:memory:`` FTS5 table, inserts one row per document
    (``rowid`` = its original corpus index; every non-string or empty doc
    stored as empty content so the corpus statistics span the same N
    documents), and ranks the "shares any query token" candidate set by
    ``bm25()`` with a stable ``rowid`` tie-break — a ``(-score, index)``
    order.

    Fails loud on an interpreter whose bundled SQLite lacks the FTS5
    extension: the ``CREATE VIRTUAL TABLE`` raises
    :class:`sqlite3.OperationalError`.
    """
    if not corpus:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    # Each token double-quoted so FTS5 operator words (OR/AND/NOT/NEAR)
    # are treated as literals; OR-joined to reproduce the "shares any
    # token" candidate set.  tokenize() emits only ``\w`` runs, so a token
    # can never contain a quote that breaks the string.
    match_query = " OR ".join(f'"{t}"' for t in query_tokens)
    rows_in = [(i, " ".join(tokenize(doc))) for i, doc in enumerate(corpus)]

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(_FTS5_CREATE)
        conn.executemany("INSERT INTO t(rowid, content) VALUES (?, ?)", rows_in)
        rows = conn.execute(
            "SELECT rowid FROM t WHERE t MATCH ? ORDER BY bm25(t), rowid",
            (match_query,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def assert_fts5_available() -> None:
    """Fail loud if the bundled SQLite lacks the FTS5 extension.

    :func:`bm25_rank` (proposal 106) has no fallback ranker — it depends
    unconditionally on FTS5's native ``bm25()``.  Called once from
    ``cli.main`` so a build whose ``sqlite3`` was compiled without FTS5
    fails at construction with a clear message, rather than mid-loop the
    first time a journal or evolution-plan search runs.

    Opens a throwaway ``:memory:`` connection and runs the same
    ``CREATE VIRTUAL TABLE ... USING fts5`` DDL the ranker uses; a missing
    extension raises :class:`sqlite3.OperationalError`, re-raised here as a
    :class:`RuntimeError` naming the missing capability.
    """
    try:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute(_FTS5_CREATE)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "SQLite FTS5 extension is unavailable, but maestro's search "
            "ranker (maestro.search.bm25_rank) requires it. Reinstall or "
            "rebuild Python with an FTS5-enabled sqlite3 build "
            f"(underlying error: {exc})."
        ) from exc


# ── journal-search backend seam ─────────────────────────────────────


@runtime_checkable
class JournalSearchBackend(Protocol):
    """Ranks a filtered journal corpus by relevance to a query (proposal 106).

    The single seam :meth:`~maestro.journal.Journal.search` ranks through, so
    the per-call ephemeral FTS5 rebuild (:class:`EphemeralJournalSearch`, the
    default) can be swapped for the persistent, incrementally-maintained
    ``state.db`` index (``maestro.state_store.SqliteJournalSearchIndex``) under
    ``[paths] sqlite_state_enabled`` without touching the journal.  ``rank``
    honours the exact contract :func:`bm25_rank` already does: it returns
    indices into ``corpus`` sorted by descending BM25 relevance with a stable
    original-index tie-break, and ``[]`` for an empty/whitespace/punctuation-only
    query, an empty corpus, or a corpus sharing no query token.
    """

    def rank(self, corpus: Sequence[str], query: str) -> list[int]:
        ...


class EphemeralJournalSearch:
    """Default :class:`JournalSearchBackend` — today's per-call FTS5 rebuild.

    Delegates straight to :func:`bm25_rank`, so an un-injected
    :class:`~maestro.journal.Journal` keeps its exact current behaviour: the
    throwaway ``:memory:`` FTS5 index rebuilt on every search.  It *is* the
    status quo, just behind the injectable seam, so wiring the seam is a
    byte-for-byte no-op for every caller until the persistent backend is
    selected.
    """

    def rank(self, corpus: Sequence[str], query: str) -> list[int]:
        return bm25_rank(corpus, query)


# ── query_term_coverage ─────────────────────────────────────────────


@dataclass(frozen=True)
class TermCoverage:
    """Coverage statistics for a single query term."""

    term: str
    document_frequency: int
    idf: float


@dataclass(frozen=True)
class QueryCoverageReport:
    """Overall coverage report for all query terms against a corpus."""

    terms: list[TermCoverage]
    total_terms: int
    zero_match_terms: int
    corpus_size: int


def _okapi_idf(tokenized: Sequence[Sequence[str]]) -> dict[str, float]:
    """Okapi BM25 IDF per corpus term, computed with the standard library.

    ``idf = log((N - df + 0.5) / (df + 0.5))`` for each unique term over the
    ``N``-document corpus.  Negative IDFs — terms appearing in more than
    ~half the docs — are floored to ``eps = 0.25 * average_idf`` (the
    epsilon variant), so a term present in every doc still earns a small
    positive weight instead of a misleading negative one.  Empty docs are
    replaced by :data:`_EMPTY_DOC_SENTINEL` so the corpus statistics span
    the same ``N`` documents.  Returns ``{}`` for an empty corpus, so every
    lookup then defaults to ``0.0``.
    """
    safe_corpus = [list(dt) if dt else [_EMPTY_DOC_SENTINEL] for dt in tokenized]
    n = len(safe_corpus)
    if n == 0:
        return {}

    df: dict[str, int] = {}
    for doc_tokens in safe_corpus:
        for term in set(doc_tokens):
            df[term] = df.get(term, 0) + 1

    idf: dict[str, float] = {}
    negatives: list[str] = []
    idf_sum = 0.0
    for term, freq in df.items():
        value = math.log((n - freq + 0.5) / (freq + 0.5))
        idf[term] = value
        idf_sum += value
        if value < 0:
            negatives.append(term)

    eps = 0.25 * (idf_sum / len(idf))
    for term in negatives:
        idf[term] = eps
    return idf


def query_term_coverage(
    query: str,
    corpus: Sequence[str],
) -> QueryCoverageReport:
    """Analyse per-term coverage of *query* against *corpus*.

    Tokenizes the query and, for each **unique** query term, reports its
    document frequency and Okapi BM25 IDF weight against the tokenized
    corpus (reusing :func:`tokenize`, :func:`_okapi_idf`, and the
    :data:`_EMPTY_DOC_SENTINEL` safeguard for empty docs), plus an overall
    summary.  A query term absent from the corpus (``df == 0``) yields an
    IDF of ``0.0``.

    Duplicate query tokens are collapsed — each unique term appears
    exactly once in the result.
    """
    query_tokens = tokenize(query)

    tokenized: list[list[str]] = [tokenize(doc) for doc in corpus]

    # Build document-frequency map: term → number of docs containing it.
    nd: dict[str, int] = {}
    for doc_tokens in tokenized:
        for t in set(doc_tokens):
            nd[t] = nd.get(t, 0) + 1

    # Okapi IDF per corpus term (empty docs guarded with the sentinel so the
    # corpus statistics span the same N documents the df map counts).
    idf_map = _okapi_idf(tokenized)

    seen: set[str] = set()
    term_entries: list[TermCoverage] = []
    for token in query_tokens:
        if token in seen:
            continue
        seen.add(token)
        df = nd.get(token, 0)
        idf = idf_map.get(token, 0.0)
        term_entries.append(TermCoverage(term=token, document_frequency=df, idf=idf))

    zero_match = sum(1 for tc in term_entries if tc.document_frequency == 0)

    return QueryCoverageReport(
        terms=term_entries,
        total_terms=len(term_entries),
        zero_match_terms=zero_match,
        corpus_size=len(corpus),
    )
