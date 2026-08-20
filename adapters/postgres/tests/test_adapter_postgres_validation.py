"""Unit tests for the postgres adapter — mock-free.

DDL behavior (ensure_schema, drop_schema, build_empty_from_sql, clone_empty_from_prod,
the psycopg2 connection built by from_env, and DDL validity) is verified against a
live postgres engine in test_integration_postgres.py, not with mocked
cursors/connections here.
"""
import pytest

from continuo_python_runtime_postgres.adapter import PostgresAdapter, _index_name, _validated_indexes


def test_required_env_names_connection_vars():
    """Test that required_env lists the three mandatory POSTGRES_* vars."""
    assert PostgresAdapter.required_env() == [
        "POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER",
    ]


def test_build_empty_from_columns_rejects_bad_type_before_touching_db() -> None:
    """Test that a malformed type string is rejected before any DB access."""
    adapter = PostgresAdapter.__new__(PostgresAdapter)  # no connection needed
    with pytest.raises(ValueError):
        adapter.build_empty_from_columns(
            "s", "t", [{"name": "id", "type": "INTEGER; DROP TABLE x"}], {}
        )


def _reject(config, columns=None):
    """Call build_empty_from_columns with *config* on a connectionless adapter.

    Every config check runs before the first statement, so a rejection is
    observable with no database at all — which is exactly the guarantee under
    test: a bad config never leaves a half-built table behind.
    """
    adapter = PostgresAdapter.__new__(PostgresAdapter)  # no connection needed
    adapter.build_empty_from_columns(
        "s", "t", columns or [{"name": "id", "type": "INTEGER"}], config
    )


def test_unknown_config_key_is_rejected_before_touching_db():
    """A key outside the postgres vocabulary is an authoring error, never ignored."""
    with pytest.raises(ValueError, match="sortkey"):
        _reject({"sortkey": ["id"]})


def test_unknown_index_key_is_rejected():
    """An unrecognized key inside an index entry rejects too — fail closed all the way down."""
    with pytest.raises(ValueError, match="method"):
        _reject({"indexes": [{"columns": ["id"], "method": "brin"}]})


def test_index_on_undeclared_column_is_rejected():
    """An index must name columns the node actually produces."""
    with pytest.raises(ValueError, match="nope"):
        _reject({"indexes": [{"columns": ["nope"]}]})


@pytest.mark.parametrize(
    "indexes",
    [
        [{"columns": []}],
        [{"columns": "id"}],
        [{"columns": ["id", ""]}],
        [{}],
        ["id"],
    ],
    ids=["empty", "string", "empty_name", "missing", "not_a_mapping"],
)
def test_malformed_index_entries_are_rejected(indexes):
    """Every malformed shape raises rather than half-applying the layout."""
    with pytest.raises(ValueError):
        _reject({"indexes": indexes})


def test_indexes_must_be_a_list():
    """A scalar where a list belongs is an authoring error."""
    with pytest.raises(ValueError, match="must be a list"):
        _reject({"indexes": {"columns": ["id"]}})


def test_non_boolean_unique_is_rejected():
    """`unique: "yes"` must not be truthy-coerced into a UNIQUE index."""
    with pytest.raises(ValueError, match="unique"):
        _reject({"indexes": [{"columns": ["id"], "unique": "yes"}]})


def test_unsupported_index_type_is_rejected():
    """The type allowlist is an injection guard, not just a spelling check."""
    with pytest.raises(ValueError, match="btree"):
        _reject({"indexes": [{"columns": ["id"], "type": "btree) ; DROP TABLE x --"}]})


@pytest.mark.parametrize("index_type", ["brin", "gin", "gist", "hash", "spgist"])
def test_unique_on_a_non_btree_access_method_is_rejected(index_type):
    """Only `btree` can enforce uniqueness, so the other five must reject.

    Confirmed against postgres:16 for each of the five: `CREATE UNIQUE INDEX
    ... USING <am>` fails with 'access method "<am>" does not support unique
    indexes'. Catching it here names the config key at fault instead.
    """
    with pytest.raises(ValueError, match="btree"):
        _reject({"indexes": [{"columns": ["id"], "unique": True, "type": index_type}]})


def test_multicolumn_hash_index_is_rejected():
    """Only `hash`, of the allowlisted access methods, refuses several columns."""
    with pytest.raises(ValueError, match="single column"):
        _reject(
            {"indexes": [{"columns": ["id", "other"], "type": "hash"}]},
            columns=[{"name": "id", "type": "INTEGER"}, {"name": "other", "type": "INTEGER"}],
        )


def test_multicolumn_non_hash_indexes_are_allowed():
    """The single-column rule is hash-specific: brin and btree take several columns."""
    declared = ["id", "other"]
    for index_type in ("brin", "btree", "gin", "gist", "spgist"):
        entries = _validated_indexes(
            {"indexes": [{"columns": declared, "type": index_type}]}, "t", declared
        )
        assert entries == [{
            "columns": declared,
            "unique": False,
            "type": index_type,
            "name": "ix_t_id_other",
        }]


def test_index_name_is_deterministic_and_within_the_postgres_limit():
    """Same table and columns, same name; and never longer than postgres's 63-byte cap."""
    columns = ["a_very_long_column_name"] * 5
    first = _index_name("a_table_with_quite_a_long_name", columns)
    assert first == _index_name("a_table_with_quite_a_long_name", columns)
    assert len(first.encode("utf-8")) <= 63


def test_index_name_distinguishes_the_same_columns_on_different_tables():
    """Postgres index names are schema-scoped, not table-scoped.

    Two nodes building into the same candidate schema can each declare the
    identical index columns; the table is part of the name, so their generated
    names do not collide.
    """
    assert _index_name("t1", ["id"]) != _index_name("t2", ["id"])


def test_duplicate_index_definition_is_rejected():
    """Two entries that normalize identically must reject, not silently collapse.

    The connection is autocommit, so each CREATE INDEX the caller emits commits
    on its own — there is no wrapping transaction to undo a duplicate's raw
    postgres error after the first copy has already applied. Catching the
    collision here, before either statement runs, is what keeps a bad config
    from leaving a half-built table behind.
    """
    with pytest.raises(ValueError, match="duplicate"):
        _reject({"indexes": [{"columns": ["id"]}, {"columns": ["id"]}]})


def test_entries_differing_only_in_column_order_are_not_duplicates():
    """Column order is part of an index's identity, and of its derived name."""
    indexes = [{"columns": ["a", "b"]}, {"columns": ["b", "a"]}]
    assert len(_validated_indexes({"indexes": indexes}, "t", ["id", "a", "b"])) == 2


@pytest.mark.parametrize(
    "indexes",
    [
        [{"columns": ["id"], "unique": False}, {"columns": ["id"], "unique": True}],
        [{"columns": ["id"], "type": "btree"}, {"columns": ["id"], "type": "hash"}],
    ],
    ids=["differ_by_unique", "differ_by_type"],
)
def test_entries_differing_only_in_unique_or_type_collide_on_one_name(indexes):
    """Two entries over the same columns resolve to one name and are rejected.

    The merged index name is ``ix_<table>_<columns>``: it deliberately excludes
    `unique` and `type`, because it names indexes on PRODUCTION tables under
    ``CREATE INDEX IF NOT EXISTS``, where folding more of the definition in
    would rename every existing index and build a second copy beside it. Two
    entries that differ only in `unique` or `type` therefore collide, and
    colliding is rejected rather than silently dropping all but the first. An
    author who genuinely wants two access methods over one column set gives one
    of them an explicit `name:`.
    """
    with pytest.raises(ValueError, match="duplicate index name"):
        _validated_indexes({"indexes": indexes}, "t", ["id", "a", "b"])


def test_an_explicit_name_admits_two_access_methods_over_one_column_set():
    """The escape hatch for the collision above: name one of them yourself."""
    indexes = [
        {"columns": ["id"], "type": "btree"},
        {"columns": ["id"], "type": "hash", "name": "ix_t_id_hash"},
    ]
    entries = _validated_indexes({"indexes": indexes}, "t", ["id"])
    assert [e["name"] for e in entries] == ["ix_t_id", "ix_t_id_hash"]
