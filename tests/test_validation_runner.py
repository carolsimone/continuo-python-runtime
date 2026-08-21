"""Unit tests for the runner — hand-written fakes, no live DB or S3."""
import boto3
import pytest

from continuo_engine_contract import result
from continuo_engine_contract.port import AdapterDiscoveryError, WarehouseAdapter
from continuo_python_runtime.validation import runner


class FakeWarehouseAdapter(WarehouseAdapter):
    """Records every adapter call; no live DB required."""

    def __init__(self):
        self.schemas_ensured = []
        self.schemas_dropped = []
        self.builds = []    # list of (schema, table, sql)
        self.clones = []    # list of (candidate_schema, prod_schema, table)
        self.column_builds = []   # list of (schema, table, columns, config)
        self.checked_binds = []   # list of sql, in call order
        self.calls = []     # unified (method, args) log, in call order across methods
        self.closed = False
        self.raise_on_binds = {}  # sql -> exception to raise from check_binds
        self.raise_on_column_build = None  # exception to raise from build_empty_from_columns

    @classmethod
    def required_env(cls) -> list[str]:
        """Return required environment variables."""
        return []

    @classmethod
    def from_env(cls) -> "FakeWarehouseAdapter":
        """Create adapter from environment."""
        return cls()

    def ensure_schema(self, schema: str) -> None:
        """Record schema ensure call."""
        self.schemas_ensured.append(schema)
        self.calls.append(("ensure_schema", (schema,)))

    def drop_schema(self, schema: str) -> None:
        """Record schema drop call."""
        self.schemas_dropped.append(schema)
        self.calls.append(("drop_schema", (schema,)))

    def build_empty_from_sql(self, schema: str, table: str, compiled_sql: str) -> None:
        """Record build call."""
        self.builds.append((schema, table, compiled_sql))
        self.calls.append(("build_empty_from_sql", (schema, table, compiled_sql)))

    def clone_empty_from_prod(self, candidate_schema: str, prod_schema: str, table: str) -> None:
        """Record clone call."""
        self.clones.append((candidate_schema, prod_schema, table))
        self.calls.append(("clone_empty_from_prod", (candidate_schema, prod_schema, table)))

    def build_empty_from_columns(
        self, schema: str, table: str, columns: list[dict], config: dict
    ) -> None:
        """Record typed-column build call, including the physical-layout config."""
        self.column_builds.append((schema, table, columns, config))
        self.calls.append(("build_empty_from_columns", (schema, table, columns, config)))
        if self.raise_on_column_build is not None:
            raise self.raise_on_column_build

    def check_binds(self, sql: str) -> None:
        """Record a bind-check call, in order, and optionally raise for *sql*."""
        self.checked_binds.append(sql)
        self.calls.append(("check_binds", (sql,)))
        if sql in self.raise_on_binds:
            raise self.raise_on_binds[sql]

    # WarehouseAdapter's data-plane methods (fetch/ensure_table/load) back the
    # runtime harness's python-node I/O, not the validation runner under test
    # here — the runner never calls them. They exist only so this fake, which
    # subclasses the single merged port, is concrete enough to instantiate.
    def fetch(self, sql: str):
        raise NotImplementedError("fetch is not exercised by the validation runner")

    def ensure_table(
        self, schema: str, table: str, columns: list[dict], *, config: dict
    ) -> None:
        raise NotImplementedError("ensure_table is not exercised by the validation runner")

    def load(self, schema: str, table: str, data) -> None:
        raise NotImplementedError("load is not exercised by the validation runner")

    def close(self) -> None:
        """Record close call."""
        self.closed = True


def _install_fake_adapter(monkeypatch, adapter, required=()):
    """Patch discovery to return a plugin class wrapping *adapter*."""

    class _Plugin:
        @staticmethod
        def required_env() -> list[str]:
            return list(required)

        @staticmethod
        def from_env() -> WarehouseAdapter:
            return adapter

    monkeypatch.setattr(runner, "discover_adapter", lambda: ("fake", _Plugin))


class _FakeBody:
    """Mimics the S3 streaming body returned inside get_object()["Body"]."""

    def __init__(self, data: bytes) -> None:
        """Store data."""
        self._data = data

    def read(self) -> bytes:
        """Return stored data."""
        return self._data


class FakeS3Client:
    """Returns pre-loaded bytes for known (bucket, key) pairs; records calls made."""

    def __init__(self, objects: dict):
        """Initialize with object mapping."""
        self._objects = objects
        self.calls = []

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        """Retrieve object or raise."""
        self.calls.append({"Bucket": Bucket, "Key": Key})
        data = self._objects.get((Bucket, Key))
        if data is None:
            raise RuntimeError(f"FakeS3Client: unknown key s3://{Bucket}/{Key}")
        return {"Body": _FakeBody(data)}


def _set_common_env(monkeypatch):
    """Set common environment variables."""
    monkeypatch.setenv("DBT_TARGET_SCHEMA", "_candidate_relA")
    monkeypatch.setenv("TABLE_NAME", "orders")


# --------------------------------------------------------------------------
# load_candidate_sql
# --------------------------------------------------------------------------

def test_load_candidate_sql_empty_when_uri_unset(monkeypatch):
    """Return empty string when CANDIDATE_SQL_URI is unset."""
    monkeypatch.delenv("CANDIDATE_SQL_URI", raising=False)
    assert runner.load_candidate_sql() == ""


def test_load_candidate_sql_fetches_and_decodes_utf8(monkeypatch):
    """Fetch and decode UTF-8 SQL from S3."""
    fake_s3 = FakeS3Client({("continuo", "candidate-sql/rel-1/svc.orders.sql"): b"  select 2  \n"})
    monkeypatch.setenv("CANDIDATE_SQL_URI", "s3://continuo/candidate-sql/rel-1/svc.orders.sql")
    monkeypatch.setattr(runner.s3, "make_s3_client", lambda: fake_s3)
    assert runner.load_candidate_sql() == "  select 2  \n"
    assert fake_s3.calls == [{"Bucket": "continuo", "Key": "candidate-sql/rel-1/svc.orders.sql"}]


def test_load_candidate_sql_raises_on_bad_uri(monkeypatch):
    """Raise ValueError on invalid S3 URI."""
    monkeypatch.setenv("CANDIDATE_SQL_URI", "not-an-s3-uri")
    with pytest.raises(ValueError):
        runner.load_candidate_sql()


# --------------------------------------------------------------------------
# main — build_from_sql / clone_from_prod / unknown op (ported behaviors)
# --------------------------------------------------------------------------

def test_main_build_from_sql_calls_adapter_and_emits_success(monkeypatch, capsys):
    """Build from SQL and emit success block."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_sql", lambda: "SELECT 1 AS id")

    runner.main()

    assert fake.schemas_ensured == ["_candidate_relA"]
    assert fake.builds == [("_candidate_relA", "orders", "SELECT 1 AS id")]
    assert fake.clones == []
    assert fake.closed is True
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"success"' in out
    assert out.strip().endswith(result.SENTINEL_END)


def test_main_uses_node_id_env_when_set(monkeypatch, capsys):
    """The structured block's unique_id echoes NODE_ID when the executor sets it."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")
    monkeypatch.setenv("NODE_ID", "model.svc.orders_v2")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_sql", lambda: "SELECT 1 AS id")

    runner.main()

    out = capsys.readouterr().out
    assert '"unique_id":"model.svc.orders_v2"' in out


def test_main_falls_back_to_model_table_when_node_id_unset(monkeypatch, capsys):
    """Without NODE_ID, unique_id falls back to model.<table> for compatibility."""
    _set_common_env(monkeypatch)
    monkeypatch.delenv("NODE_ID", raising=False)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_sql", lambda: "SELECT 1 AS id")

    runner.main()

    out = capsys.readouterr().out
    assert '"unique_id":"model.orders"' in out


def test_main_build_from_sql_empty_candidate_sql_errors(monkeypatch, capsys):
    """Exit 2 when candidate SQL is empty."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")
    monkeypatch.setattr(runner, "load_candidate_sql", lambda: "")
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    assert '"status":"error"' in capsys.readouterr().out


def test_main_build_from_sql_s3_error_emits_error_block(monkeypatch, capsys):
    """Exit 1 on S3 fetch error."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")

    def _raise():
        raise RuntimeError("S3 down")

    monkeypatch.setattr(runner, "load_candidate_sql", _raise)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"error"' in out


def test_main_clone_from_prod_calls_adapter(monkeypatch, capsys):
    """Clone from prod and emit success block."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.setenv("PROD_SCHEMA", "analytics")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)

    runner.main()

    assert fake.schemas_ensured == ["_candidate_relA"]
    assert fake.clones == [("_candidate_relA", "analytics", "orders")]
    assert fake.builds == []
    assert fake.closed is True
    assert '"status":"success"' in capsys.readouterr().out


def test_main_clone_from_prod_missing_prod_schema_exits(monkeypatch, capsys):
    """Exit 2 when PROD_SCHEMA is missing, with a structured error block."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.delenv("PROD_SCHEMA", raising=False)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert "missing required env var PROD_SCHEMA" in out


def test_main_ensure_schema_calls_adapter_without_table(monkeypatch, capsys):
    """ensure_schema op creates the schema and needs no TABLE_NAME."""
    monkeypatch.setenv("DBT_TARGET_SCHEMA", "_candidate_relA")
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.delenv("NODE_ID", raising=False)
    monkeypatch.setenv("VALIDATION_OP", "ensure_schema")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)

    runner.main()

    assert fake.schemas_ensured == ["_candidate_relA"]
    assert fake.schemas_dropped == []
    assert fake.builds == [] and fake.clones == []
    assert fake.closed is True
    out = capsys.readouterr().out
    assert '"status":"success"' in out
    assert '"unique_id":"schema._candidate_relA"' in out


def test_main_drop_schema_calls_adapter_without_ensuring(monkeypatch, capsys):
    """drop_schema op drops the schema, needs no TABLE_NAME, and never ensures."""
    monkeypatch.setenv("DBT_TARGET_SCHEMA", "_candidate_relA")
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setenv("VALIDATION_OP", "drop_schema")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)

    runner.main()

    assert fake.schemas_dropped == ["_candidate_relA"]
    assert fake.schemas_ensured == []  # teardown must not recreate the schema
    assert fake.builds == [] and fake.clones == []
    assert fake.closed is True
    assert '"status":"success"' in capsys.readouterr().out


def test_main_missing_dbt_target_schema_exits_2_with_block(monkeypatch, capsys):
    """Exit 2 when DBT_TARGET_SCHEMA is missing, with a structured error block."""
    monkeypatch.delenv("DBT_TARGET_SCHEMA", raising=False)
    monkeypatch.setenv("TABLE_NAME", "orders")
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert "missing required env var DBT_TARGET_SCHEMA" in out


def test_main_unknown_validation_op_exits_2(monkeypatch, capsys):
    """Exit 2 on unknown VALIDATION_OP."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "bogus")
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"error"' in out


# --------------------------------------------------------------------------
# main — NEW behaviors: discovery failure, missing required adapter env
# --------------------------------------------------------------------------

def test_main_discovery_failure_exits_2_with_error_block(monkeypatch, capsys):
    """Exit 2 on adapter discovery failure."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.setenv("PROD_SCHEMA", "analytics")

    def _fail():
        raise AdapterDiscoveryError("no warehouse adapter installed")

    monkeypatch.setattr(runner, "discover_adapter", _fail)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert "no warehouse adapter installed" in out


def test_main_missing_required_env_exits_2_naming_vars(monkeypatch, capsys):
    """Exit 2 when adapter required env vars are missing."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.setenv("PROD_SCHEMA", "analytics")
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake, required=["POSTGRES_HOST"])
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "POSTGRES_HOST" in out
    assert '"status":"error"' in out
    assert fake.closed is False  # never connected


# --------------------------------------------------------------------------
# main — build_from_columns (NEW op: CANDIDATE_SPEC_URI, bind-checked reads)
# --------------------------------------------------------------------------

def _spec(reads=None, columns=None, config=None):
    """Build a minimal build_from_columns spec dict for tests.

    *config* is omitted from the spec entirely when None, so the default case
    exercises the genuinely absent key rather than an explicit empty object.
    """
    spec = {
        "reads": list(reads) if reads is not None else [],
        "output_columns": list(columns) if columns is not None else [
            {"name": "id", "type": "BIGINT", "nullable": False},
        ],
    }
    if config is not None:
        spec["config"] = config
    return spec


def test_main_build_from_columns_checks_binds_then_builds_in_order(monkeypatch, capsys):
    """Happy path: ensure_schema, then check_binds per read in order, then build."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    columns = [
        {"name": "id", "type": "BIGINT", "nullable": False},
        {"name": "amount", "type": "DOUBLE PRECISION", "nullable": True},
    ]
    reads = [
        "select id from _candidate_relA.upstream_a",
        "select id from _candidate_relA.upstream_b",
    ]
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(reads, columns))

    runner.main()

    assert fake.calls == [
        ("ensure_schema", ("_candidate_relA",)),
        ("check_binds", (reads[0],)),
        ("check_binds", (reads[1],)),
        ("build_empty_from_columns", ("_candidate_relA", "orders", columns, {})),
    ]
    assert fake.checked_binds == reads
    assert fake.column_builds == [("_candidate_relA", "orders", columns, {})]
    assert fake.closed is True
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"success"' in out
    assert '"unique_id":"model.orders"' in out
    assert out.strip().endswith(result.SENTINEL_END)


def test_main_build_from_columns_check_binds_raises_blocks_build(monkeypatch, capsys):
    """A failing bind check emits an error block, exits 1, and never builds."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    reads = [
        "select id from _candidate_relA.upstream_a",
        "select missing_col from _candidate_relA.upstream_b",
    ]
    fake.raise_on_binds = {reads[1]: RuntimeError('column "missing_col" does not exist')}
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(reads))

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    assert fake.checked_binds == reads  # attempted in order, up to and including the failure
    assert fake.column_builds == []     # binds gate the build: never reached
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"error"' in out
    assert "missing_col" in out


@pytest.mark.parametrize("uri", [None, ""], ids=["unset", "empty"])
def test_main_build_from_columns_missing_spec_uri_exits_2(monkeypatch, capsys, uri):
    """Missing or empty CANDIDATE_SPEC_URI for build_from_columns is bad config: exit 2."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    if uri is None:
        monkeypatch.delenv("CANDIDATE_SPEC_URI", raising=False)
    else:
        monkeypatch.setenv("CANDIDATE_SPEC_URI", uri)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"error"' in out
    assert "CANDIDATE_SPEC_URI" in out


def test_main_build_from_columns_empty_output_columns_exits_2(monkeypatch, capsys):
    """A spec with no output_columns cannot be validated: exit 2, message names the field."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: {"reads": [], "output_columns": []})

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert "output_columns" in out


def test_main_build_from_columns_invalid_spec_json_exits_2(monkeypatch, capsys):
    """Spec JSON that fails json.loads is bad input from the control plane: exit 2.

    ``json.JSONDecodeError`` is a ``ValueError`` subclass, so it lands in the same
    except arm as an unset ``CANDIDATE_SPEC_URI`` — both are deterministic bad input
    that no retry fixes (owner ruling on the brief's Step 1/Step 3 discrepancy).
    """
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake_s3 = FakeS3Client({("continuo", "candidate-spec/rel-1/svc.orders.json"): b"not-json"})
    monkeypatch.setattr(runner.s3, "make_s3_client", lambda: fake_s3)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"error"' in out
    assert "Expecting value" in out  # json.JSONDecodeError message, proves it parsed
    assert fake_s3.calls == [
        {"Bucket": "continuo", "Key": "candidate-spec/rel-1/svc.orders.json"}
    ]


@pytest.mark.parametrize(
    "body",
    [b"[]", b"null", b"42", b'"x"'],
    ids=["list", "null", "int", "str"],
)
def test_main_build_from_columns_non_object_spec_json_exits_2(monkeypatch, capsys, body):
    """Spec JSON that parses but isn't a JSON object must still emit a block: exit 2.

    ``json.loads`` happily returns a list, ``None``, an int, or a str for valid
    JSON that isn't an object; ``load_candidate_spec`` must reject anything that
    isn't a dict itself, before ``main`` ever calls ``.get("output_columns")`` on
    it — otherwise a non-dict spec crashes with an uncaught AttributeError and
    prints no result block at all, breaking the wire contract the k8s-controller
    depends on.
    """
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake_s3 = FakeS3Client({("continuo", "candidate-spec/rel-1/svc.orders.json"): body})
    monkeypatch.setattr(runner.s3, "make_s3_client", lambda: fake_s3)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert result.SENTINEL_BEGIN in out
    assert '"status":"error"' in out


def test_main_build_from_columns_empty_reads_still_builds(monkeypatch, capsys):
    """Zero declared reads is valid: no bind checks, still builds and succeeds."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    columns = [{"name": "id", "type": "BIGINT", "nullable": False}]
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec([], columns))

    runner.main()

    assert fake.checked_binds == []
    assert fake.column_builds == [("_candidate_relA", "orders", columns, {})]
    out = capsys.readouterr().out
    assert '"status":"success"' in out


def test_main_build_from_columns_passes_spec_config_to_the_adapter(monkeypatch, capsys):
    """The spec's config block reaches the adapter unchanged — the runner is engine-blind."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    config = {"indexes": [{"columns": ["id"], "unique": True}]}
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(config=config))

    runner.main()

    assert fake.column_builds[0][3] == config
    assert '"status":"success"' in capsys.readouterr().out


@pytest.mark.parametrize("config", [None, {}], ids=["absent", "explicit_null"])
def test_main_build_from_columns_absent_config_becomes_empty_dict(monkeypatch, capsys, config):
    """A spec with no config (or an explicit JSON null) builds with an empty block."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    spec = _spec()
    if config is not None:
        spec["config"] = None
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: spec)

    runner.main()

    assert fake.column_builds[0][3] == {}
    assert '"status":"success"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad", [[], "indexes", 3, True], ids=["list", "string", "int", "bool"]
)
def test_main_build_from_columns_non_object_config_exits_2(monkeypatch, capsys, bad):
    """A config that isn't a JSON object is bad input: exit 2, message names the field."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(config=bad))

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 2
    assert fake.column_builds == []  # never reached the adapter
    out = capsys.readouterr().out
    assert '"status":"error"' in out
    assert "config" in out


def test_main_build_from_columns_adapter_config_rejection_fails_the_gate(monkeypatch, capsys):
    """An adapter rejecting an unknown key surfaces as an error block and exit 1.

    This is the whole point of applying config at validation: the engine's own
    fail-closed rejection has to reach the release gate as a normal validation
    failure, not as a crash or a silent success.
    """
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    fake.raise_on_column_build = ValueError("unrecognized config key(s) 'sortkey'")
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(config={"sortkey": ["id"]}))

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert '"status":"error"' in out
    assert "sortkey" in out


# --------------------------------------------------------------------------
# main — build_from_columns csv_source header check (python-csv-nodes A6)
# --------------------------------------------------------------------------

CSV_HEADER_WITH_EXTRA_BODY = b"order_id,amount,extra\n1,10.5,x\n2,20.0,y\n"
CSV_HEADER_MISSING_COLUMN_BODY = b"order_id,amount\n1,10.5\n2,20.0\n"


@pytest.fixture(scope="session")
def csv_source_bucket(minio_container):
    """Real minio-backed csv fixtures for the csv_source header-check tests.

    Uploads objects into a bucket dedicated to this module (kept separate
    from test_csv_readers_integration.py's own ``drops`` bucket so the two
    modules' session-scoped setup never race each other): a csv whose header
    has an extra undeclared column, one missing a declared column, and a
    0-byte object (FIX 6 — empty/no-header source). ``nope.csv`` is
    deliberately never uploaded, so a test can point csv_source at it to
    exercise the unreachable-source path against the real minio backend.
    """
    endpoint, access, secret = minio_container
    client = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access, aws_secret_access_key=secret,
    )
    client.create_bucket(Bucket="csv-validation")
    client.put_object(Bucket="csv-validation", Key="orders.csv", Body=CSV_HEADER_WITH_EXTRA_BODY)
    client.put_object(
        Bucket="csv-validation", Key="orders_missing_col.csv",
        Body=CSV_HEADER_MISSING_COLUMN_BODY,
    )
    client.put_object(Bucket="csv-validation", Key="empty.csv", Body=b"")
    return endpoint


@pytest.mark.integration
def test_main_build_from_columns_with_csv_source_checks_header(
    monkeypatch, capsys, caplog, csv_source_bucket
):
    """csv_source header conformance against a real minio object: declared
    columns are all present, and the csv's extra undeclared column ("extra")
    logs a structured warning but does not block the build."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("S3_ENDPOINT_URL", csv_source_bucket)
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    columns = [
        {"name": "order_id", "type": "BIGINT", "nullable": False},
        {"name": "amount", "type": "DOUBLE PRECISION", "nullable": True},
    ]
    spec = _spec(columns=columns)
    spec["csv_source"] = "s3://csv-validation/orders.csv"
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: spec)

    with caplog.at_level("WARNING"):
        runner.main()

    assert fake.column_builds == [("_candidate_relA", "orders", columns, {})]
    out = capsys.readouterr().out
    assert '"status":"success"' in out
    assert "csv_header_extra_columns" in caplog.text
    assert "extra" in caplog.text


@pytest.mark.integration
def test_main_build_from_columns_csv_header_missing_column_fails(
    monkeypatch, capsys, csv_source_bucket
):
    """A declared column absent from the real csv header fails the release
    gate: exit 1, error block names the missing column."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("S3_ENDPOINT_URL", csv_source_bucket)
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    columns = [
        {"name": "order_id", "type": "BIGINT", "nullable": False},
        {"name": "customer_id", "type": "BIGINT", "nullable": False},
    ]
    spec = _spec(columns=columns)
    spec["csv_source"] = "s3://csv-validation/orders_missing_col.csv"
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: spec)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    assert fake.column_builds == []  # header check blocks the build
    out = capsys.readouterr().out
    assert '"status":"error"' in out
    assert "missing declared column" in out


@pytest.mark.integration
def test_main_build_from_columns_unreachable_csv_fails(monkeypatch, capsys, csv_source_bucket):
    """An unreachable csv_source blocks promotion: exit 1, error block emitted."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("S3_ENDPOINT_URL", csv_source_bucket)
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    spec = _spec()
    spec["csv_source"] = "s3://csv-validation/nope.csv"
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: spec)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    assert fake.column_builds == []
    out = capsys.readouterr().out
    assert '"status":"error"' in out


@pytest.mark.integration
def test_main_build_from_columns_empty_csv_source_gives_legible_error(
    monkeypatch, capsys, csv_source_bucket
):
    """A 0-byte csv_source (fetch_header_line returns "") must not surface as
    an opaque StopIteration from csv.reader: exit 1, error block names the
    empty/no-header source (FIX 6)."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("S3_ENDPOINT_URL", csv_source_bucket)
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    spec = _spec()
    spec["csv_source"] = "s3://csv-validation/empty.csv"
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: spec)

    with pytest.raises(SystemExit) as exc:
        runner.main()

    assert exc.value.code == 1
    assert fake.column_builds == []  # header check blocks the build
    out = capsys.readouterr().out
    assert '"status":"error"' in out
    assert "no header line" in out
    assert "s3://csv-validation/empty.csv" in out


def test_main_build_from_columns_without_csv_source_unchanged(monkeypatch, capsys):
    """No csv_source key in the spec: behavior is unchanged from before A6 —
    no header fetch is attempted, and the build proceeds straight through."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    columns = [{"name": "id", "type": "BIGINT", "nullable": False}]
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec([], columns))

    runner.main()

    assert fake.column_builds == [("_candidate_relA", "orders", columns, {})]
    assert '"status":"success"' in capsys.readouterr().out


# --------------------------------------------------------------------------
# main — sentinel-block invariant across every block-emitting exit path
# --------------------------------------------------------------------------

def _setup_success(monkeypatch):
    """Arrange a build_from_sql run that reaches the success block."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_sql", lambda: "SELECT 1 AS id")


def _setup_empty_candidate_sql(monkeypatch):
    """Arrange a build_from_sql run whose candidate SQL is empty."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")
    monkeypatch.setattr(runner, "load_candidate_sql", lambda: "")


def _setup_s3_error(monkeypatch):
    """Arrange a build_from_sql run whose S3 fetch raises."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_sql")

    def _raise():
        raise RuntimeError("S3 down")

    monkeypatch.setattr(runner, "load_candidate_sql", _raise)


def _setup_unknown_op(monkeypatch):
    """Arrange a run with an unrecognized VALIDATION_OP."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "bogus")


def _setup_discovery_failure(monkeypatch):
    """Arrange a clone_from_prod run whose adapter discovery fails."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.setenv("PROD_SCHEMA", "analytics")

    def _fail():
        raise AdapterDiscoveryError("no warehouse adapter installed")

    monkeypatch.setattr(runner, "discover_adapter", _fail)


def _setup_missing_required_env(monkeypatch):
    """Arrange a clone_from_prod run whose adapter is missing required env."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.setenv("PROD_SCHEMA", "analytics")
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    fake = FakeWarehouseAdapter()
    _install_fake_adapter(monkeypatch, fake, required=["POSTGRES_HOST"])


def _setup_missing_dbt_target_schema(monkeypatch):
    """Arrange a run missing the required DBT_TARGET_SCHEMA env var."""
    monkeypatch.delenv("DBT_TARGET_SCHEMA", raising=False)
    monkeypatch.setenv("TABLE_NAME", "orders")


def _setup_missing_prod_schema(monkeypatch):
    """Arrange a clone_from_prod run missing the required PROD_SCHEMA env var."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "clone_from_prod")
    monkeypatch.delenv("PROD_SCHEMA", raising=False)


def _setup_ensure_schema(monkeypatch):
    """Arrange an ensure_schema run that reaches the success block."""
    monkeypatch.setenv("DBT_TARGET_SCHEMA", "_candidate_relA")
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setenv("VALIDATION_OP", "ensure_schema")
    _install_fake_adapter(monkeypatch, FakeWarehouseAdapter())


def _setup_drop_schema(monkeypatch):
    """Arrange a drop_schema run that reaches the success block."""
    monkeypatch.setenv("DBT_TARGET_SCHEMA", "_candidate_relA")
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setenv("VALIDATION_OP", "drop_schema")
    _install_fake_adapter(monkeypatch, FakeWarehouseAdapter())


def _setup_build_from_columns_success(monkeypatch):
    """Arrange a build_from_columns run that reaches the success block."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    _install_fake_adapter(monkeypatch, FakeWarehouseAdapter())
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec([], None))


def _setup_build_from_columns_check_binds_raises(monkeypatch):
    """Arrange a build_from_columns run whose bind check raises."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake = FakeWarehouseAdapter()
    fake.raise_on_binds = {"select 1": RuntimeError("boom")}
    _install_fake_adapter(monkeypatch, fake)
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(["select 1"]))


def _setup_build_from_columns_missing_spec_uri(monkeypatch):
    """Arrange a build_from_columns run missing CANDIDATE_SPEC_URI."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.delenv("CANDIDATE_SPEC_URI", raising=False)


def _setup_build_from_columns_empty_output_columns(monkeypatch):
    """Arrange a build_from_columns run whose spec has no output_columns."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: {"reads": [], "output_columns": []})


def _setup_build_from_columns_non_object_config(monkeypatch):
    """Arrange a build_from_columns run whose spec config is not a JSON object."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    _install_fake_adapter(monkeypatch, FakeWarehouseAdapter())
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: _spec(config=["indexes"]))


def _setup_build_from_columns_non_string_csv_source(monkeypatch):
    """Arrange a build_from_columns run whose spec csv_source is not a string (FIX 3)."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    _install_fake_adapter(monkeypatch, FakeWarehouseAdapter())
    spec = _spec()
    spec["csv_source"] = 123
    monkeypatch.setattr(runner, "load_candidate_spec", lambda: spec)


def _setup_build_from_columns_invalid_json(monkeypatch):
    """Arrange a build_from_columns run whose spec body is not valid JSON."""
    _set_common_env(monkeypatch)
    monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
    monkeypatch.setenv("CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json")
    fake_s3 = FakeS3Client({("continuo", "candidate-spec/rel-1/svc.orders.json"): b"not-json"})
    monkeypatch.setattr(runner.s3, "make_s3_client", lambda: fake_s3)


def _make_setup_build_from_columns_non_object_spec(body: bytes):
    """Return a setup arranging a build_from_columns run whose spec body isn't a JSON object.

    A factory (not a single function) because each ``_SENTINEL_SCENARIOS`` row
    needs its own closure over a distinct non-object *body* (list/null/int/str).
    """

    def _setup(monkeypatch):
        _set_common_env(monkeypatch)
        monkeypatch.setenv("VALIDATION_OP", "build_from_columns")
        monkeypatch.setenv(
            "CANDIDATE_SPEC_URI", "s3://continuo/candidate-spec/rel-1/svc.orders.json"
        )
        fake_s3 = FakeS3Client({("continuo", "candidate-spec/rel-1/svc.orders.json"): body})
        monkeypatch.setattr(runner.s3, "make_s3_client", lambda: fake_s3)

    return _setup


# (setup, expected SystemExit code, or None when main() returns normally)
_SENTINEL_SCENARIOS = [
    ("success", _setup_success, None),
    ("ensure_schema", _setup_ensure_schema, None),
    ("drop_schema", _setup_drop_schema, None),
    ("empty_candidate_sql", _setup_empty_candidate_sql, 2),
    ("s3_error", _setup_s3_error, 1),
    ("unknown_op", _setup_unknown_op, 2),
    ("discovery_failure", _setup_discovery_failure, 2),
    ("missing_required_env", _setup_missing_required_env, 2),
    ("missing_dbt_target_schema", _setup_missing_dbt_target_schema, 2),
    ("missing_prod_schema", _setup_missing_prod_schema, 2),
    ("build_from_columns_success", _setup_build_from_columns_success, None),
    ("build_from_columns_check_binds_raises", _setup_build_from_columns_check_binds_raises, 1),
    ("build_from_columns_missing_spec_uri", _setup_build_from_columns_missing_spec_uri, 2),
    ("build_from_columns_empty_output_columns", _setup_build_from_columns_empty_output_columns, 2),
    ("build_from_columns_non_object_config", _setup_build_from_columns_non_object_config, 2),
    (
        "build_from_columns_non_string_csv_source",
        _setup_build_from_columns_non_string_csv_source,
        2,
    ),
    ("build_from_columns_invalid_json", _setup_build_from_columns_invalid_json, 2),
    (
        "build_from_columns_non_object_json_list",
        _make_setup_build_from_columns_non_object_spec(b"[]"),
        2,
    ),
    (
        "build_from_columns_non_object_json_null",
        _make_setup_build_from_columns_non_object_spec(b"null"),
        2,
    ),
    (
        "build_from_columns_non_object_json_int",
        _make_setup_build_from_columns_non_object_spec(b"42"),
        2,
    ),
    (
        "build_from_columns_non_object_json_str",
        _make_setup_build_from_columns_non_object_spec(b'"x"'),
        2,
    ),
]


def test_main_emits_exactly_one_sentinel_block_as_last_stdout_line(monkeypatch, capsys):
    """Every block-emitting exit path prints exactly one sentinel block, block-last.

    The contract (see ``result.py``) is: exactly ONE sentinel-framed block, as the
    terminal non-empty stdout line, on every outcome that emits one. Exercises all
    twenty-one block-emitting paths through ``main()`` — success, ensure_schema,
    drop_schema, empty candidate SQL, S3-fetch error, unknown VALIDATION_OP, adapter
    discovery failure, missing required adapter env, missing DBT_TARGET_SCHEMA,
    missing PROD_SCHEMA, and the eleven build_from_columns paths (success, a failing
    bind check, missing CANDIDATE_SPEC_URI, empty output_columns, a non-object config,
    a non-string csv_source, invalid spec JSON, and spec JSON that parses to a
    list/null/int/str instead of an object) — each in its own isolated monkeypatch
    context so scenarios cannot leak
    patches into one another.
    """
    for name, setup, expected_exit in _SENTINEL_SCENARIOS:
        with monkeypatch.context() as mp:
            setup(mp)
            capsys.readouterr()  # drain output from any prior scenario
            if expected_exit is None:
                runner.main()
            else:
                with pytest.raises(SystemExit) as exc:
                    runner.main()
                assert exc.value.code == expected_exit, name

            out = capsys.readouterr().out
            assert out.count(result.SENTINEL_BEGIN) == 1, name
            assert out.count(result.SENTINEL_END) == 1, name
            assert out.strip().splitlines()[-1] == result.SENTINEL_END, name
