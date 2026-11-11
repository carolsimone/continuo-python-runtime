"""Unit tests for the view-definition scan behind ``TrinoAdapter.dependents``.

``_view_reads_table`` decides, from one view's SQL text alone, whether the view
reads a given table. ``dependents`` runs one query over ``information_schema.views``
and applies it to every row; the cases below use a scripted cursor, so no Trino is
needed. The live behaviour is checked by ``TestTrinoDependents`` in
``test_integration_trino_conformance.py``.
"""
import logging

import pytest

from continuo_trino_adapter.adapter import TrinoAdapter, _view_reads_table


@pytest.mark.parametrize(
    ("definition", "reads"),
    [
        ('SELECT id FROM "iceberg"."analytics"."seed"', True),
        ("SELECT id FROM analytics.seed", True),
        ("SELECT id FROM seed", True),  # unqualified: the view's own schema
        ("SELECT id FROM other.seed", False),
        ("SELECT id FROM analytics.seed_ext", False),
        ("SELECT id FROM lake.analytics.seed", False),  # another catalog
        ("WITH seed AS (SELECT 1 AS id) SELECT id FROM seed", False),  # a CTE, not the table
        ("SELECT a.id FROM analytics.orders a JOIN analytics.SEED s ON a.id = s.id", True),
        ("SELECT id FROM (SELECT id FROM analytics.seed) x", True),
        ("SELECT id FROM analytics.seed WHERE ((", True),  # unparsable, name present
        ("SELECT id FROM analytics.orders WHERE ((", False),  # unparsable, name absent
        ('SELECT id FROM "Analytics"."SEED"', True),  # quoted, mixed case
        ('SELECT id FROM "ICEBERG"."Analytics"."Seed"', True),
        ("SELECT a.id FROM analytics.seed a JOIN analytics.seed b ON a.id = b.id", True),
        ("SELECT id FROM analytics.orders WHERE id IN (SELECT id FROM analytics.seed)", True),
        (
            "SELECT id FROM analytics.orders UNION ALL SELECT id FROM analytics.seed",
            True,
        ),
        ("SELECT id FROM analytics.orders UNION ALL SELECT id FROM analytics.other", False),
        # A view that is itself named like the seed, in another schema: reading it
        # is not reading the seed.
        ("SELECT * FROM other.seed", False),
        ("SELECT * FROM lake.other.seed", False),
        ("SELECT id FROM analytics.seed WHERE ((", True),
        ("SELECT id FROM analytics.seed_ext WHERE ((", False),  # unparsable, only a longer name
        ("SELECT id FROM analytics.my_seed WHERE ((", False),
        ('SELECT id FROM "seed" WHERE ((', True),  # unparsable, quoted name
        ("", False),
        ("   ", False),
        ("CREATE TABLE analytics.seed (id BIGINT)", False),  # parses, but is not a read
        ("SELECT 1", False),
    ],
)
def test_view_reads_table(definition, reads):
    assert _view_reads_table(definition, "analytics", "iceberg", "analytics", "seed") is reads


def test_unqualified_reference_resolves_in_the_views_own_schema():
    assert _view_reads_table("SELECT id FROM seed", "other", "iceberg", "analytics", "seed") is False
    assert _view_reads_table("SELECT id FROM seed", "other", "iceberg", "other", "seed") is True


def test_two_part_reference_resolves_in_the_adapters_catalog():
    definition = "SELECT id FROM analytics.seed"
    assert _view_reads_table(definition, "views", "iceberg", "analytics", "seed") is True
    assert _view_reads_table(definition, "views", "lake", "analytics", "seed") is True
    assert _view_reads_table(definition, "views", "lake", "other", "seed") is False


def test_a_cte_shadows_only_an_unqualified_reference():
    definition = "WITH seed AS (SELECT 1 AS id) SELECT id FROM seed JOIN analytics.seed s ON true"
    assert _view_reads_table(definition, "analytics", "iceberg", "analytics", "seed") is True


def test_an_unparsable_definition_logs_a_warning_naming_the_view(caplog):
    with caplog.at_level(logging.WARNING, logger="continuo_trino_adapter"):
        _view_reads_table(
            "SELECT id FROM analytics.seed WHERE ((",
            "reports",
            "iceberg",
            "analytics",
            "seed",
            view="broken_view",
        )
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 1
    assert "reports.broken_view" in messages[0]


def test_a_parsable_definition_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="continuo_trino_adapter"):
        _view_reads_table(
            "SELECT id FROM analytics.seed", "reports", "iceberg", "analytics", "seed", view="v"
        )
    assert caplog.records == []


class _Cursor:
    def __init__(self, rows, log):
        self._rows = rows
        self._log = log

    def execute(self, statement, params=None):
        self._log.append(statement)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Connection:
    def __init__(self, rows):
        self.statements: list[str] = []
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows, self.statements)


def test_dependents_runs_one_query_and_lists_the_views_that_read_the_table(caplog):
    conn = _Connection(
        [
            ("reports", "by_seed", 'SELECT id FROM "iceberg"."analytics"."seed"'),
            ("reports", "by_orders", "SELECT id FROM analytics.orders"),
            ("analytics", "local", "SELECT id FROM seed"),
            ("reports", "no_definition", None),
            ("reports", "broken", "SELECT id FROM analytics.seed WHERE (("),
        ]
    )
    adapter = TrinoAdapter(conn, "iceberg")

    with caplog.at_level(logging.WARNING, logger="continuo_trino_adapter"):
        result = adapter.dependents("analytics", "seed")

    assert conn.statements == [
        'SELECT table_schema, table_name, view_definition FROM "iceberg".information_schema.views'
    ]
    assert result.completeness == "best_effort"
    assert [(d.schema, d.name, d.kind, d.owner, d.depth) for d in result.objects] == [
        ("reports", "by_seed", "view", "", 1),
        ("analytics", "local", "view", "", 1),
        ("reports", "broken", "view", "", 1),
    ]
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "reports.broken" in warned
    assert "reports.no_definition" in warned


def test_dependents_quotes_the_catalog_name():
    conn = _Connection([])
    TrinoAdapter(conn, 'we"ird').dependents("analytics", "seed")
    assert conn.statements == [
        'SELECT table_schema, table_name, view_definition FROM "we""ird".information_schema.views'
    ]


def test_dependents_of_a_schema_with_no_views_is_empty():
    result = TrinoAdapter(_Connection([]), "iceberg").dependents("nowhere", "seed")
    assert result.objects == ()
    assert result.completeness == "best_effort"
