"""``PostgresAdapter.dependents`` against postgres:16 (tests/smoke/postgres-stack).

Run the stack first:
    docker compose -f tests/smoke/postgres-stack/docker-compose.yml up -d --wait

Every fixture creates uniquely named schemas (and a NOLOGIN role) and drops them
in teardown, whether the test passed or not.
"""
import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest

from continuo_postgres_adapter.adapter import PostgresAdapter

from .test_integration_postgres_conformance import PG

pytestmark = pytest.mark.integration

_ESTATE = """
CREATE TABLE "{s}".seed (id int PRIMARY KEY, name text);
CREATE VIEW "{s}".v AS SELECT id, name FROM "{s}".seed;
CREATE MATERIALIZED VIEW "{s}".mv AS SELECT id FROM "{s}".seed;
CREATE VIEW "{s}".v_on_v AS SELECT id FROM "{s}".v;
CREATE FUNCTION "{s}".seed_count() RETURNS bigint LANGUAGE sql BEGIN ATOMIC SELECT count(*) FROM "{s}".seed; END;
CREATE VIEW "{b}".bi_v AS SELECT id FROM "{s}".seed;
ALTER VIEW "{b}".bi_v OWNER TO "{r}";
CREATE TABLE "{b}".orders (id int, seed_id int REFERENCES "{s}".seed (id));
ALTER TABLE "{b}".orders OWNER TO "{r}";
CREATE TABLE "{b}".guarded (id int);
CREATE POLICY seen ON "{b}".guarded USING (id IN (SELECT id FROM "{s}".seed));
CREATE TABLE "{b}".child () INHERITS ("{s}".seed);
CREATE TABLE "{b}".typed (row_value "{s}".seed);
"""

# The objects of schemas %(s)s and %(b)s as `dependents()` names them, minus the
# seed and what is attached to it (its own constraints and columns).
_OBJECTS = """
SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view' ELSE 'table' END,
       n.nspname, c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN (%(s)s, %(b)s) AND c.relkind IN ('r', 'p', 'f', 'v', 'm')
  AND NOT (n.nspname = %(s)s AND c.relname = 'seed')
UNION
SELECT CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END, n.nspname,
       p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname IN (%(s)s, %(b)s)
UNION
SELECT 'constraint', n.nspname, k.conname || ' on ' || c.relname
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN (%(s)s, %(b)s) AND NOT (n.nspname = %(s)s AND c.relname = 'seed')
UNION
SELECT 'policy', n.nspname, p.polname || ' on ' || c.relname
FROM pg_policy p
JOIN pg_class c ON c.oid = p.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN (%(s)s, %(b)s) AND NOT (n.nspname = %(s)s AND c.relname = 'seed')
"""

# A column a cascade drops leaves its table in place, so a dropped column is
# read as the change of its table.
_COLUMNS = """
SELECT n.nspname, c.relname, a.attname
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN (%(s)s, %(b)s) AND c.relkind IN ('r', 'p', 'f')
  AND a.attnum > 0 AND NOT a.attisdropped
  AND NOT (n.nspname = %(s)s AND c.relname = 'seed')
"""


def _connect() -> Any:
    conn = psycopg2.connect(**PG)
    conn.autocommit = True
    return conn


def _query(sql: str, *params: Any) -> list[tuple[Any, ...]]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def _snapshot(cur: Any, params: dict[str, str]) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    cur.execute(_OBJECTS, params)
    objects = set(cur.fetchall())
    cur.execute(_COLUMNS, params)
    return objects, set(cur.fetchall())


def _dropped_by_cascade(s: str, b: str) -> set[tuple[str, str, str]]:
    """What ``DROP TABLE s.seed CASCADE`` removes or changes, as (kind, schema, name); rolled back."""
    params = {"s": s, "b": b}
    conn = psycopg2.connect(**PG)
    try:
        with conn.cursor() as cur:
            objects_before, columns_before = _snapshot(cur, params)
            cur.execute(f'DROP TABLE "{s}".seed CASCADE')
            objects_after, columns_after = _snapshot(cur, params)
        conn.rollback()
    finally:
        conn.close()
    changed_tables = {("table", schema, table) for schema, table, _ in columns_before - columns_after}
    return (objects_before - objects_after) | changed_tables


def _drop_everything(schemas: list[str], roles: list[str]) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for schema in schemas:
                with contextlib.suppress(psycopg2.Error):
                    cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for role in roles:
                with contextlib.suppress(psycopg2.Error):
                    cur.execute(f'DROP OWNED BY "{role}"')
                with contextlib.suppress(psycopg2.Error):
                    cur.execute(f'DROP ROLE IF EXISTS "{role}"')
    finally:
        conn.close()


@pytest.fixture
def estate() -> Iterator[tuple[PostgresAdapter, str, str, str]]:
    suffix = uuid.uuid4().hex[:10]
    s, b, r = f"dep_{suffix}", f"bi_{suffix}", f"bi_{suffix}"
    conn = _connect()
    adapter = PostgresAdapter(psycopg2.connect(**PG))
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE ROLE "{r}" NOLOGIN')
            cur.execute(f'CREATE SCHEMA "{s}"')
            cur.execute(f'CREATE SCHEMA "{b}" AUTHORIZATION "{r}"')
            cur.execute(_ESTATE.format(s=s, b=b, r=r))
        yield adapter, s, b, r
    finally:
        adapter.close()
        conn.close()
        _drop_everything([s, b], [r])


@pytest.fixture
def scratch() -> Iterator[tuple[PostgresAdapter, str, Any]]:
    """An adapter, an empty schema and a cursor that runs DDL in it."""
    schema = f"dep_{uuid.uuid4().hex[:10]}"
    conn = _connect()
    adapter = PostgresAdapter(psycopg2.connect(**PG))
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            yield adapter, schema, cur
    finally:
        adapter.close()
        conn.close()
        _drop_everything([schema], [])


def _listed(adapter, s, table="seed"):
    return {(d.schema, d.name, d.kind, d.owner, d.depth) for d in adapter.dependents(s, table).objects}


def test_dependents_lists_every_view_and_function_the_cascade_drops(estate):
    adapter, s, b, r = estate
    assert adapter.dependents(s, "seed").completeness == "exact"
    assert {
        (s, "v", "view", "continuo", 1),
        (s, "mv", "materialized_view", "continuo", 1),
        (s, "v_on_v", "view", "continuo", 2),
        (s, "seed_count()", "function", "continuo", 1),
        (b, "bi_v", "view", r, 1),
    } <= _listed(adapter, s)


def test_view_table_usage_misses_what_the_walk_finds(estate):
    adapter, s, _, _ = estate
    usage = {name for (name,) in _query(
        "SELECT view_name FROM information_schema.view_table_usage WHERE table_schema = %s AND table_name = 'seed'", s)}
    walked = {d.name for d in adapter.dependents(s, "seed").objects}
    assert {"mv", "v_on_v", "seed_count()"} <= walked - usage


def test_dependents_lists_constraints_policies_and_tables_the_cascade_changes(estate):
    adapter, s, b, r = estate
    assert {
        (b, "orders_seed_id_fkey on orders", "constraint", r, 1),
        (b, "seen on guarded", "policy", "continuo", 1),
        (b, "child", "table", "continuo", 1),
        (b, "typed", "table", "continuo", 1),
    } <= _listed(adapter, s)


def test_every_object_the_cascade_drops_was_listed(estate):
    adapter, s, b, _ = estate
    listed = {(d.kind, d.schema, d.name) for d in adapter.dependents(s, "seed").objects}
    dropped = _dropped_by_cascade(s, b)
    assert dropped and dropped <= listed


def test_the_cascade_guard_sees_the_changes_it_is_meant_to_see(estate):
    _, s, b, _ = estate
    assert {
        ("view", s, "v"), ("materialized_view", s, "mv"), ("function", s, "seed_count()"),
        ("constraint", b, "orders_seed_id_fkey on orders"), ("policy", b, "seen on guarded"),
        ("table", b, "child"), ("table", b, "typed"),
    } <= _dropped_by_cascade(s, b)


def test_dependents_finds_the_seed_whatever_the_letter_case(estate):
    adapter, s, _, _ = estate
    found = adapter.dependents(s.upper(), "SEED").objects
    assert found and found == adapter.dependents(s, "seed").objects


def test_dependents_of_a_partitioned_table_include_what_reads_its_partitions(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}".seed (id int, day date) PARTITION BY RANGE (day);
        CREATE TABLE "{s}".seed_2026 PARTITION OF "{s}".seed FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
        CREATE VIEW "{s}".v AS SELECT id FROM "{s}".seed;
        CREATE VIEW "{s}".on_partition AS SELECT id FROM "{s}".seed_2026;
        CREATE VIEW "{s}".on_on_partition AS SELECT id FROM "{s}".on_partition;
    ''')
    assert _listed(adapter, s) == {
        (s, "v", "view", "continuo", 1),
        (s, "on_partition", "view", "continuo", 1),
        (s, "on_on_partition", "view", "continuo", 2),
    }


def test_dependents_include_what_reads_a_sequence_a_column_owns(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}".seed (id serial PRIMARY KEY);
        CREATE TABLE "{s}".other (id int DEFAULT nextval('"{s}".seed_id_seq'));
    ''')
    assert _listed(adapter, s) == {
        ("", f"default value for column id of table {s}.other", "other", "", 1),
    }


def test_a_foreign_key_of_the_table_onto_itself_is_part_of_the_table(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}".seed (id int PRIMARY KEY, boss int REFERENCES "{s}".seed (id));
    ''')
    assert _listed(adapter, s) == set()


def test_dependents_finds_a_seed_whose_name_needs_quoting(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}"."Mixed Case" (id int);
        CREATE VIEW "{s}"."Odd View" AS SELECT id FROM "{s}"."Mixed Case";
    ''')
    expected = {(s, "Odd View", "view", "continuo", 1)}
    assert _listed(adapter, s, "Mixed Case") == expected
    assert _listed(adapter, s, "mixed case") == expected
    assert _listed(adapter, s.upper(), "MIXED CASE") == expected


def test_a_view_of_a_view_of_a_view_is_listed_at_depths_one_two_three(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}".seed (id int);
        CREATE VIEW "{s}".v1 AS SELECT id FROM "{s}".seed;
        CREATE VIEW "{s}".v2 AS SELECT id FROM "{s}".v1;
        CREATE VIEW "{s}".v3 AS SELECT id FROM "{s}".v2;
    ''')
    assert _listed(adapter, s) == {
        (s, "v1", "view", "continuo", 1),
        (s, "v2", "view", "continuo", 2),
        (s, "v3", "view", "continuo", 3),
    }


def test_an_object_reached_by_several_paths_is_listed_once_at_its_shallowest_depth(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}".seed (id int);
        CREATE VIEW "{s}".left_side AS SELECT id FROM "{s}".seed;
        CREATE VIEW "{s}".right_side AS SELECT id FROM "{s}".seed;
        CREATE VIEW "{s}".diamond AS
            SELECT l.id FROM "{s}".left_side l JOIN "{s}".right_side r USING (id);
        CREATE VIEW "{s}".shortcut AS
            SELECT s.id FROM "{s}".seed s JOIN "{s}".left_side l USING (id);
    ''')
    objects = adapter.dependents(s, "seed").objects
    assert len({(d.kind, d.schema, d.name) for d in objects}) == len(objects)
    assert {(d.name, d.depth) for d in objects} == {
        ("left_side", 1), ("right_side", 1), ("diamond", 2), ("shortcut", 1),
    }


def test_tables_that_reference_each_other_terminate_and_list_each_object_once(scratch):
    adapter, s, cur = scratch
    cur.execute(f'''
        CREATE TABLE "{s}".seed (id int PRIMARY KEY, boss int);
        ALTER TABLE "{s}".seed ADD FOREIGN KEY (boss) REFERENCES "{s}".seed (id);
        CREATE TABLE "{s}".other (id int PRIMARY KEY, seed_id int REFERENCES "{s}".seed (id));
        ALTER TABLE "{s}".seed ADD COLUMN other_id int REFERENCES "{s}".other (id);
    ''')
    objects = adapter.dependents(s, "seed").objects
    assert len({(d.kind, d.schema, d.name) for d in objects}) == len(objects)
    assert (s, "other_seed_id_fkey on other", "constraint", "continuo", 1) in {
        (d.schema, d.name, d.kind, d.owner, d.depth) for d in objects
    }
