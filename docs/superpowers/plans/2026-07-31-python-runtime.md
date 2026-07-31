# continuo-python-runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the python-node runtime: PyPI package (contract loader/validator/merger, content_hash, conform, harness, lint), per-engine base images, and the domain-repo template — per `docs/superpowers/specs/2026-07-31-python-runtime-design.md`.

**Architecture:** One package `continuo_python_runtime` consumed three ways: as a CLI in domain CI (`validate`/`merge`/`hash`/`lint`), as the container entrypoint (`run`), and as a library under test. Warehouse I/O goes through the `RuntimeAdapter` port (ABC in `continuo-validation-contract` 0.3.0; engine impls in continuo-validation-runners). Arrow is the neutral data currency.

**Tech Stack:** Python ≥3.14, hatchling, uv, pyarrow, PyYAML, `continuo-validation-contract` (sentinel + ports), ruff, mypy, pytest. GitHub Actions. Docker.

## Global Constraints

- Public repos: **each PR below is one reviewable unit**; do not merge PRs together. Every PR leaves `main` green and the package importable.
- Python `>= 3.14`; build backend `hatchling`; deps managed with `uv`; pins match continuo-validation-runners: `ruff==0.3.0`, `mypy==1.18.2`, `pytest==9.0.3`, `pytest-cov==4.1.0`.
- Diagnostics via stdlib `logging` to **stderr** only (`logging.basicConfig(stream=sys.stderr, ...)`, module-level loggers). stdout is reserved for exactly one sentinel block printed by the harness — the only permitted `print`.
- Sentinel block: reuse `continuo_validation_contract.result.result_block(status, message, failures, unique_id)`; never re-implement the markers. Error class travels as the deterministic message prefix `"<ErrorClass>: <detail>"`.
- Supported `output_columns` types (exact set): `BIGINT`, `INT`/`INTEGER`, `DOUBLE PRECISION`, `NUMERIC(p,s)`/`DECIMAL(p,s)`, `VARCHAR(n)`/`CHAR(n)`/`TEXT`, `TIMESTAMP`, `DATE`, `BOOLEAN`.
- `content_hash` algorithm is spec §13.2 of the parent design, byte-exact: `"sha256:" + sha256(canonical_json(entry_without_content_hash) + b"\x00" + script_bytes)`, `reads` values whitespace-normalized, JSON sorted keys / compact separators.
- Until `continuo-validation-contract==0.3.0` is on PyPI, depend on it via a uv path source: `[tool.uv.sources] continuo-validation-contract = { path = "../continuo/validation-contract", editable = true }`. Remove the override in PR 8 (images) — images must install from PyPI.
- Commit messages: conventional (`feat:`, `test:`, `chore:`), each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## PR map (merge order)

| PR | Repo | Content | Tasks |
|---|---|---|---|
| PR 1 | continuo (`validation-contract`) | `RuntimeAdapter` ABC + `continuo_runtime.adapters` discovery (additive, part of the planned 0.3.0 bump) | 1 |
| PR 2 | this repo | Scaffold: pyproject, package skeleton, error taxonomy, repo CI; remove pre-design scaffold | 2–3 |
| PR 3 | this repo | Contract schema v1: model, loader, validator | 4–5 |
| PR 4 | this repo | `hashing.py` — content_hash reference impl | 6 |
| PR 5 | this repo | `types.py` (SQL→Arrow) + `conform.py` | 7–8 |
| PR 6 | this repo | Merger + CLI (`validate`, `merge`, `hash`) | 9–10 |
| PR 7 | this repo | `context.py` (RunContext) + `lint.py` + CLI `lint` | 11–12 |
| PR 8 | this repo | `harness.py` + CLI `run` + fake-adapter e2e | 13–14 |
| PR 9 | this repo | `Dockerfile.postgres` + image CI + smoke test | 15 |
| PR 10 | this repo | `template/` + golden test + README rewrite | 16–17 |
| PR 11 | this repo | `Dockerfile.trino` + CI matrix entry (after `continuo-runtime-trino` publishes) | 18 |
| ext-A | continuo-validation-runners | `continuo-runtime-postgres` package (blocks PR 9) | out of this plan |
| ext-B | continuo-validation-runners | `continuo-runtime-trino` package (blocks PR 11) | out of this plan |

External PRs ext-A/ext-B implement the PR 1 ABC per spec §4 (Postgres: `TRUNCATE + INSERT` in one transaction; Trino: staging table → swap) with that repo's docker-compose integration tests; they follow that repo's existing package layout and are planned there.

---

### Task 1: `RuntimeAdapter` ABC + discovery (PR 1, repo `~/github/continuo`)

**Files:**
- Modify: `validation-contract/continuo_validation_contract/port.py`
- Test: `validation-contract/tests/test_port.py` (append)
- Modify: `validation-contract/pyproject.toml` (version → `0.3.0` if not already bumped by the parallel `build_empty_from_columns` work; coordinate — this task only ADDS code)

**Interfaces:**
- Produces: `RuntimeAdapter` ABC with `required_env() -> list[str]` (classmethod), `from_env() -> RuntimeAdapter` (classmethod), `fetch(sql: str) -> "pyarrow.Table"`, `ensure_table(schema: str, table: str, columns: list[dict]) -> None`, `load(schema: str, table: str, data: "pyarrow.Table") -> None`, `close() -> None`; constant `RUNTIME_ENTRY_POINT_GROUP = "continuo_runtime.adapters"`; `discover_runtime_adapter() -> tuple[str, type[RuntimeAdapter]]`.
- Note: pyarrow is referenced **only in annotations under `TYPE_CHECKING`** — validation-contract gains no runtime dependency. `columns` is `list[dict]` (`{"name","type","nullable"}`) at this layer to avoid a model dependency; the runtime repo passes `dataclasses.asdict`-style dicts.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_port.py`)

```python
from continuo_validation_contract.port import (
    RUNTIME_ENTRY_POINT_GROUP,
    AdapterDiscoveryError,
    RuntimeAdapter,
    discover_runtime_adapter,
)


def test_runtime_adapter_is_abstract():
    with pytest.raises(TypeError):
        RuntimeAdapter()  # type: ignore[abstract]


def test_runtime_entry_point_group_name():
    assert RUNTIME_ENTRY_POINT_GROUP == "continuo_runtime.adapters"


def test_discover_runtime_adapter_empty_group_raises():
    with pytest.raises(AdapterDiscoveryError, match="no runtime adapter installed"):
        discover_runtime_adapter()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/github/continuo/validation-contract && uv run pytest tests/test_port.py -v`
Expected: FAIL — `ImportError: cannot import name 'RuntimeAdapter'`

- [ ] **Step 3: Implement** (append to `port.py`; refactor discovery to share one helper)

```python
RUNTIME_ENTRY_POINT_GROUP = "continuo_runtime.adapters"

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow


class RuntimeAdapter(ABC):
    """Port for engine-specific data-plane I/O at python-node runtime.

    The harness is the only caller: scripts never see this surface. Same
    stdout discipline as WarehouseAdapter — log to stderr, never print.
    """

    @classmethod
    @abstractmethod
    def required_env(cls) -> list[str]:
        """Names of env vars that must be non-empty before connecting."""

    @classmethod
    @abstractmethod
    def from_env(cls) -> "RuntimeAdapter":
        """Construct a connected adapter from environment variables."""

    @abstractmethod
    def fetch(self, sql: str) -> "pyarrow.Table":
        """Execute one declared read and return the result as Arrow."""

    @abstractmethod
    def ensure_table(self, schema: str, table: str, columns: list[dict]) -> None:
        """CREATE TABLE IF NOT EXISTS with the typed DDL compiled from columns.

        Each column dict carries ``name``, ``type`` (SQL type string from the
        contract's supported set), ``nullable`` (bool).
        """

    @abstractmethod
    def load(self, schema: str, table: str, data: "pyarrow.Table") -> None:
        """Atomically replace the table's contents with *data*."""

    @abstractmethod
    def close(self) -> None:
        """Release the underlying connection."""


def _discover(group: str, kind: str, base: type) -> tuple[str, type]:
    eps = list(entry_points(group=group))
    if not eps:
        raise AdapterDiscoveryError(
            f"no {kind} adapter installed (entry-point group {group!r} is empty); "
            f"install exactly one engine package"
        )
    if len(eps) > 1:
        names = ", ".join(sorted(ep.name for ep in eps))
        raise AdapterDiscoveryError(
            f"multiple {kind} adapters installed ({names}); an image must install exactly one"
        )
    ep = eps[0]
    try:
        cls = ep.load()
    except Exception as exc:
        raise AdapterDiscoveryError(f"failed to load adapter entry point {ep.name!r}: {exc}") from exc
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise AdapterDiscoveryError(f"entry point {ep.name!r} does not resolve to a {base.__name__} subclass")
    return ep.name, cls


def discover_runtime_adapter() -> tuple[str, type[RuntimeAdapter]]:
    """Return ``(engine_name, adapter_class)`` from the single installed runtime plugin."""
    return _discover(RUNTIME_ENTRY_POINT_GROUP, "runtime", RuntimeAdapter)
```

Also rewrite the existing `discover_adapter()` body as `return _discover(ENTRY_POINT_GROUP, "warehouse", WarehouseAdapter)` — keep its public signature and error-message wording compatible (existing tests must still pass; adjust `_discover` messages only if an existing test pins the old wording, in which case keep two message templates).

- [ ] **Step 4: Run the full suite**

Run: `cd ~/github/continuo/validation-contract && uv run pytest -v`
Expected: PASS (new tests + all pre-existing discovery tests)

- [ ] **Step 5: Commit on a branch in the continuo repo**

```bash
git checkout -b feat/runtime-adapter-port
git add validation-contract
git commit -m "feat(validation-contract): add RuntimeAdapter port + runtime entry-point discovery"
```

---

### Task 2: Repo scaffold + pyproject + CI (PR 2)

**Files:**
- Delete: `contract/`, `code/`, `adapter/`, `hashing/`, `contract-loader/` (pre-design scaffold, superseded per spec §3)
- Create: `pyproject.toml`, `continuo_python_runtime/__init__.py`, `tests/__init__.py`, `.github/workflows/ci.yml`, `README.md` note (defer full rewrite to PR 10)

**Interfaces:**
- Produces: importable package `continuo_python_runtime`; `uv run pytest` green; CI running ruff+mypy+pytest on PRs.

- [ ] **Step 1: Remove the superseded scaffold**

```bash
git rm -r contract code adapter hashing contract-loader
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "continuo-python-runtime"
version = "0.1.0"
description = "Runtime harness, contract tooling, and CI lint for Continuo python nodes."
authors = [{ name = "Simone Carolini" }]
maintainers = [{ name = "Simone Carolini" }]
readme = "README.md"
requires-python = ">= 3.14"
dependencies = [
    "continuo-validation-contract==0.3.0",
    "pyarrow==21.0.0",
    "PyYAML==6.0.3",
]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "Programming Language :: Python :: 3.14",
]

[project.scripts]
continuo-runtime = "continuo_python_runtime.cli:main"

[dependency-groups]
dev = ["ruff==0.3.0", "mypy==1.18.2"]
test = ["pytest==9.0.3", "pytest-cov==4.1.0"]

[tool.uv.sources]
continuo-validation-contract = { path = "../continuo/validation-contract", editable = true }

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["continuo_python_runtime"]
```

(Adjust the pyarrow/PyYAML pins to the latest stable at implementation time; keep them exact-pinned. `cli:main` doesn't exist until PR 6 — a dangling entry point is harmless for `uv run pytest`, but if `uv sync` validates it, create a stub `cli.py` with `def main() -> int: return 0` now.)

- [ ] **Step 3: Create the package + smoke test**

`continuo_python_runtime/__init__.py`:

```python
"""Runtime harness and contract tooling for Continuo python nodes."""
```

`tests/test_package.py`:

```python
import continuo_python_runtime


def test_importable():
    assert continuo_python_runtime.__doc__
```

- [ ] **Step 4: Run**

Run: `uv sync --all-groups && uv run pytest -v && uv run ruff check . && uv run mypy continuo_python_runtime`
Expected: all PASS

- [ ] **Step 5: Write `.github/workflows/ci.yml`**

```yaml
name: ci
on:
  push: { branches: [main] }
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: Sync
        run: uv sync --all-groups
      - name: Lint
        run: uv run ruff check .
      - name: Types
        run: uv run mypy continuo_python_runtime
      - name: Tests
        run: uv run pytest --cov=continuo_python_runtime -v
```

CI cannot see `../continuo`, so the committed `[tool.uv.sources]` entry must be a **git source**, not the path source shown in Step 2 — commit this form (it works both locally and in CI):
`continuo-validation-contract = { git = "https://github.com/carolsimone/continuo", subdirectory = "validation-contract", rev = "<PR 1 merge sha>" }` — replaced by the plain PyPI pin in PR 9 once 0.3.0 publishes.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: package scaffold, CI, remove pre-design prototype"
```

---

### Task 3: Error taxonomy (PR 2)

**Files:**
- Create: `continuo_python_runtime/errors.py`
- Test: `tests/test_errors.py`

**Interfaces:**
- Produces: `HarnessError(Exception)` base with `error_class: str` attribute; subclasses `ContractError`, `ReadError`, `ScriptError`, `ConformError`, `LoadError`; `HarnessError.sentinel_message() -> str` returning `"<ErrorClass>: <str(self)>"`. Every later task raises exactly these.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from continuo_python_runtime.errors import (
    ConformError,
    ContractError,
    HarnessError,
    LoadError,
    ReadError,
    ScriptError,
)


@pytest.mark.parametrize(
    "exc_type,name",
    [
        (ContractError, "ContractError"),
        (ReadError, "ReadError"),
        (ScriptError, "ScriptError"),
        (ConformError, "ConformError"),
        (LoadError, "LoadError"),
    ],
)
def test_error_class_and_sentinel_message(exc_type, name):
    err = exc_type("boom")
    assert isinstance(err, HarnessError)
    assert err.error_class == name
    assert err.sentinel_message() == f"{name}: boom"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_errors.py -v` → FAIL (module not found)

- [ ] **Step 3: Implement `errors.py`**

```python
"""Deterministic error taxonomy for the harness.

The sentinel result block's ``message`` starts with ``<ErrorClass>: `` so the
remediation classifier can key off it without parsing free text.
"""


class HarnessError(Exception):
    """Base for all runtime failures the harness converts to a sentinel block."""

    @property
    def error_class(self) -> str:
        return type(self).__name__

    def sentinel_message(self) -> str:
        return f"{self.error_class}: {self}"


class ContractError(HarnessError):
    """Contract missing, invalid, node not found, or script missing."""


class ReadError(HarnessError):
    """Unknown read name, or a declared read failed at the warehouse."""


class ScriptError(HarnessError):
    """run() raised, has the wrong signature, or returned a non-Arrow-convertible value."""


class ConformError(HarnessError):
    """Structural mismatch, strict-cast failure, or VARCHAR overflow."""


class LoadError(HarnessError):
    """DDL or INSERT failure at the warehouse during the write."""
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_errors.py -v` → PASS
- [ ] **Step 5: Commit** — `git add ... && git commit -m "feat: harness error taxonomy"` — then open **PR 2**.

---

### Task 4: Contract model (PR 3)

**Files:**
- Create: `continuo_python_runtime/contract/__init__.py`, `continuo_python_runtime/contract/model.py`
- Test: `tests/contract/test_model.py` (+ `tests/contract/__init__.py`)

**Interfaces:**
- Produces:
  - `Column(name: str, type: str, nullable: bool = True)` — frozen dataclass.
  - `Node(schema, table, owner, schedule, criticality, script, reads: dict[str, str], output_columns: tuple[Column, ...], description: str = "", extra_columns: str = "raise", content_hash: str | None = None)` — frozen dataclass; property `relation -> str` = `"{schema}.{table}"`.
  - Constants `CRITICALITIES = frozenset({"REGULATORY", "CORE", "SECONDARY"})`, `EXTRA_COLUMNS_POLICIES = frozenset({"raise", "warn"})`, `CONTRACT_VERSION = 1`.

- [ ] **Step 1: Failing test**

```python
from continuo_python_runtime.contract.model import Column, Node


def _node(**over):
    base = dict(
        schema="analytics",
        table="t",
        owner="marketing",
        schedule="daily",
        criticality="SECONDARY",
        script="scripts/t.py",
        reads={"ids": "select id from analytics.a"},
        output_columns=(Column("id", "INTEGER", nullable=False),),
    )
    base.update(over)
    return Node(**base)


def test_defaults_and_relation():
    n = _node()
    assert n.relation == "analytics.t"
    assert n.extra_columns == "raise"
    assert n.description == ""
    assert n.content_hash is None
    assert n.output_columns[0].nullable is False
```

- [ ] **Step 2: Run → FAIL** (`uv run pytest tests/contract/test_model.py -v`)
- [ ] **Step 3: Implement `model.py`** exactly per the Interfaces block (plain frozen `@dataclass`; no validation here — the loader validates).
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: contract v1 model`

---

### Task 5: Contract loader + validator (PR 3)

**Files:**
- Create: `continuo_python_runtime/contract/loader.py`
- Test: `tests/contract/test_loader.py`

**Interfaces:**
- Consumes: `model.py` (Task 4), `ContractError` (Task 3).
- Produces:
  - `parse_node(raw: dict, source: str) -> Node` — validates one mapping; `source` (filename) appears in every error message.
  - `load_contract_dir(path: Path) -> list[Node]` — loads every `*.yml`/`*.yaml` under `path` (sorted), each file `{"nodes": [...]}`; validates cross-file: duplicate `(schema, table)` rejected.
  - Validation rules (each raises `ContractError` naming the source + node): required non-empty strings `schema`, `table`, `owner`, `schedule`, `script`; `criticality` ∈ `CRITICALITIES`; `extra_columns` ∈ `EXTRA_COLUMNS_POLICIES`; `reads` a non-empty map of non-empty strings, duplicate names impossible by dict but empty SQL rejected; `output_columns` non-empty, each `{name, type[, nullable]}` with `type` matching the supported-type grammar (delegates to `types.parse_sql_type` **once PR 5 lands** — in this PR, validate with the regex below, then PR 5's Task 7 swaps the regex for `parse_sql_type` in a one-line change); duplicate column names rejected; unknown keys at node level rejected (misspelling guard).

- [ ] **Step 1: Failing tests** (representative set — implement all)

```python
import pytest

from continuo_python_runtime.contract.loader import load_contract_dir, parse_node
from continuo_python_runtime.errors import ContractError

VALID = {
    "schema": "analytics",
    "table": "t",
    "description": "d",
    "owner": "marketing",
    "schedule": "daily",
    "criticality": "SECONDARY",
    "script": "scripts/t.py",
    "reads": {"ids": "select id from analytics.a"},
    "output_columns": [
        {"name": "id", "type": "INTEGER", "nullable": False},
        {"name": "amount", "type": "NUMERIC(10,2)"},
    ],
}


def test_parse_valid_node():
    n = parse_node(VALID, "f.yml")
    assert n.relation == "analytics.t"
    assert n.output_columns[1].type == "NUMERIC(10,2)"


@pytest.mark.parametrize("missing", ["schema", "table", "owner", "schedule", "script", "reads", "output_columns"])
def test_missing_required_field_rejected(missing):
    raw = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ContractError, match=missing):
        parse_node(raw, "f.yml")


def test_unknown_key_rejected():
    with pytest.raises(ContractError, match="unknown"):
        parse_node({**VALID, "schdule": "daily"} | {"schedule": "daily"}, "f.yml")


def test_bad_criticality_and_policy_and_type():
    with pytest.raises(ContractError, match="criticality"):
        parse_node({**VALID, "criticality": "HIGH"}, "f.yml")
    with pytest.raises(ContractError, match="extra_columns"):
        parse_node({**VALID, "extra_columns": "ignore"}, "f.yml")
    with pytest.raises(ContractError, match="type"):
        parse_node({**VALID, "output_columns": [{"name": "x", "type": "JSONB"}]}, "f.yml")


def test_duplicate_column_rejected():
    cols = [{"name": "id", "type": "INTEGER"}, {"name": "id", "type": "BIGINT"}]
    with pytest.raises(ContractError, match="duplicate column"):
        parse_node({**VALID, "output_columns": cols}, "f.yml")


def test_load_dir_duplicate_relation_across_files_rejected(tmp_path):
    a, b = tmp_path / "a.yml", tmp_path / "b.yml"
    import yaml
    a.write_text(yaml.safe_dump({"nodes": [VALID]}))
    b.write_text(yaml.safe_dump({"nodes": [VALID]}))
    with pytest.raises(ContractError, match="duplicate node analytics.t"):
        load_contract_dir(tmp_path)
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement `loader.py`** — key parts:

```python
_ALLOWED_KEYS = {
    "schema", "table", "description", "owner", "schedule", "criticality",
    "script", "extra_columns", "reads", "output_columns", "content_hash",
}
_TYPE_RE = re.compile(
    r"^(BIGINT|INT|INTEGER|DOUBLE PRECISION|TEXT|TIMESTAMP|DATE|BOOLEAN"
    r"|(NUMERIC|DECIMAL)\(\d+,\s*\d+\)"
    r"|(VARCHAR|CHAR)\(\d+\))$",
    re.IGNORECASE,
)
```

`parse_node` checks unknown keys first (`set(raw) - _ALLOWED_KEYS`), then each rule in the Interfaces block, building `Column`/`Node`. `load_contract_dir` iterates `sorted(path.glob("*.yml")) + sorted(path.glob("*.yaml"))`, requires each document to be a mapping with a `nodes` list, calls `parse_node`, then checks `relation` uniqueness across all files (`ContractError(f"duplicate node {relation} (in {a} and {b})")`). Empty dir / no nodes → `ContractError("no contract files found in ...")`.

- [ ] **Step 4: Run → PASS**; also `uv run ruff check . && uv run mypy continuo_python_runtime`
- [ ] **Step 5: Commit** — `feat: contract v1 loader + validator` — open **PR 3**.

---

### Task 6: content_hash (PR 4)

**Files:**
- Create: `continuo_python_runtime/hashing.py`
- Test: `tests/test_hashing.py`

**Interfaces:**
- Produces:
  - `canonical_entry(entry: dict) -> dict` — deep-copies, drops `content_hash`, whitespace-normalizes every value in `reads` (`" ".join(sql.split())`).
  - `content_hash(entry: dict, script_bytes: bytes) -> str` — `"sha256:" + sha256(json.dumps(canonical_entry(entry), sort_keys=True, separators=(",", ":")).encode() + b"\x00" + script_bytes).hexdigest()`.
  - `entry` is the node's plain-dict wire form (what the merger emits), NOT the `Node` dataclass.

- [ ] **Step 1: Failing tests — the invariants Continuo trusts blindly**

```python
from continuo_python_runtime.hashing import canonical_entry, content_hash

ENTRY = {
    "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
    "criticality": "SECONDARY", "script": "scripts/t.py", "description": "",
    "reads": {"ids": "select id\n  from   analytics.a"},
    "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
}
SCRIPT = b"def run(ctx):\n    return ctx.read('ids')\n"


def test_sql_whitespace_insensitive():
    reformatted = {**ENTRY, "reads": {"ids": "select    id from analytics.a"}}
    assert content_hash(ENTRY, SCRIPT) == content_hash(reformatted, SCRIPT)


def test_key_order_insensitive():
    reordered = dict(reversed(list(ENTRY.items())))
    assert content_hash(ENTRY, SCRIPT) == content_hash(reordered, SCRIPT)


def test_existing_content_hash_field_excluded():
    assert content_hash({**ENTRY, "content_hash": "sha256:stale"}, SCRIPT) == content_hash(ENTRY, SCRIPT)


def test_semantic_edits_change_hash():
    assert content_hash({**ENTRY, "reads": {"ids": "select id2 from analytics.a"}}, SCRIPT) != content_hash(ENTRY, SCRIPT)
    assert content_hash({**ENTRY, "reads": {"renamed": ENTRY["reads"]["ids"]}}, SCRIPT) != content_hash(ENTRY, SCRIPT)
    assert content_hash({**ENTRY, "owner": "other"}, SCRIPT) != content_hash(ENTRY, SCRIPT)


def test_script_is_byte_sensitive_including_whitespace():
    assert content_hash(ENTRY, SCRIPT + b" ") != content_hash(ENTRY, SCRIPT)


def test_prefix_format():
    h = content_hash(ENTRY, SCRIPT)
    assert h.startswith("sha256:") and len(h) == 7 + 64
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement** exactly per Interfaces (≈20 lines; `copy.deepcopy` before mutating).
- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: content_hash reference implementation (spec 13.2)` — open **PR 4**.

---

### Task 7: SQL type grammar → Arrow (PR 5)

**Files:**
- Create: `continuo_python_runtime/types.py`
- Modify: `continuo_python_runtime/contract/loader.py` (swap `_TYPE_RE` for `parse_sql_type`)
- Test: `tests/test_types.py`

**Interfaces:**
- Consumes: `ContractError`.
- Produces:
  - `SqlType(base: str, precision: int | None = None, scale: int | None = None, length: int | None = None)` — frozen dataclass; `base` canonicalized to one of `BIGINT INTEGER DOUBLE_PRECISION NUMERIC VARCHAR CHAR TEXT TIMESTAMP DATE BOOLEAN` (aliases folded: `INT→INTEGER`, `DECIMAL→NUMERIC`).
  - `parse_sql_type(raw: str) -> SqlType` — case-insensitive; raises `ContractError` on anything outside the supported set.
  - `arrow_type(t: SqlType) -> pa.DataType` — `BIGINT→int64`, `INTEGER→int32`, `DOUBLE_PRECISION→float64`, `NUMERIC→decimal128(p, s)`, `VARCHAR/CHAR/TEXT→string`, `TIMESTAMP→timestamp("us")`, `DATE→date32`, `BOOLEAN→bool_`.

- [ ] **Step 1: Failing tests**

```python
import pyarrow as pa
import pytest

from continuo_python_runtime.errors import ContractError
from continuo_python_runtime.types import SqlType, arrow_type, parse_sql_type


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BIGINT", SqlType("BIGINT")),
        ("int", SqlType("INTEGER")),
        ("Integer", SqlType("INTEGER")),
        ("DOUBLE PRECISION", SqlType("DOUBLE_PRECISION")),
        ("NUMERIC(10,2)", SqlType("NUMERIC", precision=10, scale=2)),
        ("decimal(5, 0)", SqlType("NUMERIC", precision=5, scale=0)),
        ("VARCHAR(255)", SqlType("VARCHAR", length=255)),
        ("char(3)", SqlType("CHAR", length=3)),
        ("TEXT", SqlType("TEXT")),
        ("TIMESTAMP", SqlType("TIMESTAMP")),
        ("DATE", SqlType("DATE")),
        ("BOOLEAN", SqlType("BOOLEAN")),
    ],
)
def test_parse_supported(raw, expected):
    assert parse_sql_type(raw) == expected


@pytest.mark.parametrize("raw", ["JSONB", "VARCHAR", "NUMERIC", "NUMERIC(10)", "FLOAT", ""])
def test_parse_unsupported_raises(raw):
    with pytest.raises(ContractError):
        parse_sql_type(raw)


def test_arrow_mapping():
    assert arrow_type(parse_sql_type("NUMERIC(10,2)")) == pa.decimal128(10, 2)
    assert arrow_type(parse_sql_type("VARCHAR(9)")) == pa.string()
    assert arrow_type(parse_sql_type("TIMESTAMP")) == pa.timestamp("us")
    assert arrow_type(parse_sql_type("INT")) == pa.int32()
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement `types.py`**, then replace the loader's `_TYPE_RE` check with `parse_sql_type(col_type)` (keeping the raised error's `ContractError` type and source context).
- [ ] **Step 4: Run full suite → PASS** (loader tests still green)
- [ ] **Step 5: Commit** — `feat: SQL type grammar and Arrow mapping`

---

### Task 8: conform() (PR 5)

**Files:**
- Create: `continuo_python_runtime/conform.py`
- Test: `tests/test_conform.py`

**Interfaces:**
- Consumes: `types.py`, `Column`, `ConformError`, `ScriptError`.
- Produces:
  - `to_arrow(obj) -> pa.Table` — `pa.Table` passthrough; objects with `__arrow_c_stream__` via `pa.table(obj)`; pandas DataFrame via `pa.Table.from_pandas(obj, preserve_index=False)` (import pandas lazily, only if the object looks like one); anything else → `ScriptError("run() returned <type>; expected an Arrow-convertible value")`.
  - `conform(table: pa.Table, columns: Sequence[Column], extra_columns: str = "raise") -> pa.Table` — order: (1) extra-column policy, (2) missing columns, (3) select declared order, (4) strict cast, (5) not-null check, (6) VARCHAR/CHAR length check. All violations → `ConformError`.

- [ ] **Step 1: Failing tests — the full matrix from spec §10**

```python
import pyarrow as pa
import pytest

from continuo_python_runtime.conform import conform, to_arrow
from continuo_python_runtime.contract.model import Column
from continuo_python_runtime.errors import ConformError, ScriptError

COLS = (
    Column("id", "INTEGER", nullable=False),
    Column("amount", "NUMERIC(10,2)"),
    Column("email", "VARCHAR(5)"),
)


def _t(**cols):
    return pa.table(dict(cols))


def test_happy_path_reorders_and_casts():
    out = conform(_t(email=["a"], amount=["1.50"], id=[1]), COLS)
    assert out.column_names == ["id", "amount", "email"]
    assert out.schema.field("id").type == pa.int32()
    assert out.schema.field("amount").type == pa.decimal128(10, 2)


def test_extra_column_raises_by_default():
    with pytest.raises(ConformError, match="undeclared column"):
        conform(_t(id=[1], amount=["1"], email=["a"], tmp=[0]), COLS)


def test_extra_column_warn_drops(caplog):
    out = conform(_t(id=[1], amount=["1"], email=["a"], tmp=[0]), COLS, extra_columns="warn")
    assert "tmp" not in out.column_names
    assert any("dropping undeclared" in r.message for r in caplog.records)


def test_missing_column_raises():
    with pytest.raises(ConformError, match="missing column"):
        conform(_t(id=[1], amount=["1"]), COLS)


def test_lossy_float_to_int_raises():
    with pytest.raises(ConformError):
        conform(_t(id=[3.9], amount=["1"], email=["a"]), COLS)


def test_unparseable_string_raises():
    with pytest.raises(ConformError):
        conform(_t(id=["abc"], amount=["1"], email=["a"]), COLS)


def test_decimal_overflow_raises():
    with pytest.raises(ConformError):
        conform(_t(id=[1], amount=["123456789.12"], email=["a"]), COLS)


def test_varchar_overflow_raises():
    with pytest.raises(ConformError, match="email.*exceeds VARCHAR\\(5\\)"):
        conform(_t(id=[1], amount=["1"], email=["toolong"]), COLS)


def test_null_in_not_null_column_raises():
    with pytest.raises(ConformError, match="id.*null"):
        conform(_t(id=[None], amount=["1"], email=["a"]), COLS)


def test_to_arrow_rejects_non_convertible():
    with pytest.raises(ScriptError):
        to_arrow(object())


def test_to_arrow_passthrough_and_capsule():
    t = _t(id=[1])
    assert to_arrow(t) is t
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement `conform.py`** — core:

```python
logger = logging.getLogger("continuo_python_runtime.conform")


def conform(table, columns, extra_columns="raise"):
    declared = [c.name for c in columns]
    extra = [n for n in table.column_names if n not in declared]
    if extra:
        if extra_columns == "raise":
            raise ConformError(f"dataframe has undeclared column(s): {extra}; declared: {declared}")
        logger.warning("conform: dropping undeclared column(s) %s", extra)
    missing = [n for n in declared if n not in table.column_names]
    if missing:
        raise ConformError(f"dataframe is missing column(s): {missing}")
    table = table.select(declared)
    target = pa.schema(
        [pa.field(c.name, arrow_type(parse_sql_type(c.type)), nullable=c.nullable) for c in columns]
    )
    try:
        table = table.cast(target, safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError) as exc:
        raise ConformError(f"strict cast to declared schema failed: {exc}") from exc
    for col in columns:
        if not col.nullable and table.column(col.name).null_count:
            raise ConformError(f"column {col.name} is declared nullable: false but contains nulls")
        sql_t = parse_sql_type(col.type)
        if sql_t.length is not None:
            max_len = pc.max(pc.utf8_length(table.column(col.name))).as_py()
            if max_len is not None and max_len > sql_t.length:
                raise ConformError(
                    f"column {col.name}: value length {max_len} exceeds {sql_t.base}({sql_t.length})"
                )
    return table
```

(`pc` = `pyarrow.compute`. Note `pa.Table.cast` with `safe=True` raises on lossy conversions; verify the float→int case actually raises in the installed pyarrow — if it silently truncates, pre-check with `pc.floor`/equality comparison and raise `ConformError` explicitly; the test defines the required behavior, the implementation must satisfy it.)

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: strict conform() (Arrow cast + varchar + null checks)` — open **PR 5**.

---

### Task 9: Merger (PR 6)

**Files:**
- Create: `continuo_python_runtime/contract/merge.py`
- Test: `tests/contract/test_merge.py`

**Interfaces:**
- Consumes: `load_contract_dir`, `content_hash`, `CONTRACT_VERSION`.
- Produces:
  - `node_entry(node: Node) -> dict` — the wire form: all contract fields with `reads` as the map and `output_columns` as list-of-dicts (`nullable` always present), NO `content_hash`.
  - `build_wire_contract(contract_dir: Path, repo_root: Path, service: str) -> dict` — loads + validates, resolves each `node.script` against `repo_root` (missing script → `ContractError`), embeds `content_hash(entry, script_bytes)`, returns `{"contract_version": 1, "service": service, "nodes": [...]}` with nodes sorted by `relation`.
  - `write_wire_contract(doc: dict, out: Path) -> None` — `yaml.safe_dump(doc, sort_keys=False)`.

- [ ] **Step 1: Failing test**

```python
import yaml

from continuo_python_runtime.contract.merge import build_wire_contract

def _write_repo(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "t.py").write_text("def run(ctx):\n    return ctx.read('ids')\n")
    (tmp_path / "contracts" / "t.yml").write_text(yaml.safe_dump({"nodes": [{
        "schema": "analytics", "table": "t", "owner": "m", "schedule": "daily",
        "criticality": "SECONDARY", "script": "scripts/t.py",
        "reads": {"ids": "select id from analytics.a"},
        "output_columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
    }]}))
    return tmp_path


def test_wire_contract_shape_and_hash(tmp_path):
    repo = _write_repo(tmp_path)
    doc = build_wire_contract(repo / "contracts", repo, "marketing-py")
    assert doc["contract_version"] == 1
    assert doc["service"] == "marketing-py"
    (entry,) = doc["nodes"]
    assert entry["content_hash"].startswith("sha256:")


def test_hash_stable_across_contract_reformatting(tmp_path):
    repo = _write_repo(tmp_path)
    h1 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]["content_hash"]
    text = (repo / "contracts" / "t.yml").read_text()
    (repo / "contracts" / "t.yml").write_text(text.replace("select id", "select   id"))
    h2 = build_wire_contract(repo / "contracts", repo, "s")["nodes"][0]["content_hash"]
    assert h1 == h2


def test_missing_script_rejected(tmp_path):
    repo = _write_repo(tmp_path)
    (repo / "scripts" / "t.py").unlink()
    import pytest
    from continuo_python_runtime.errors import ContractError
    with pytest.raises(ContractError, match="scripts/t.py"):
        build_wire_contract(repo / "contracts", repo, "s")
```

- [ ] **Step 2: Run → FAIL**  |  **Step 3: Implement per Interfaces**  |  **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: contract merger with embedded content_hash`

---

### Task 10: CLI — validate / merge / hash (PR 6)

**Files:**
- Create: `continuo_python_runtime/cli.py` (replace PR 2 stub)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: loader, merger, hashing.
- Produces: `main(argv: list[str] | None = None) -> int`, argparse subcommands:
  - `continuo-runtime validate <contract_dir>` — exit 0 / print nothing on success; on `ContractError` log to stderr, exit 1.
  - `continuo-runtime merge <contract_dir> --service NAME --repo-root PATH --out FILE`
  - `continuo-runtime hash <contract_dir> --repo-root PATH` — prints `<relation>\t<content_hash>` lines to stdout (a CI/debug command; this stdout use is machine-parsed output, permitted).
  - Configures `logging.basicConfig(stream=sys.stderr, level=logging.INFO)` once, in `main`.

- [ ] **Step 1: Failing tests** (drive via `main([...])`, use the Task 9 `_write_repo` fixture moved to `tests/conftest.py` as `contract_repo` fixture)

```python
from continuo_python_runtime.cli import main


def test_validate_ok(contract_repo):
    assert main(["validate", str(contract_repo / "contracts")]) == 0


def test_validate_bad_dir_exits_1(tmp_path, caplog):
    assert main(["validate", str(tmp_path)]) == 1
    assert any("no contract files" in r.message for r in caplog.records)


def test_merge_writes_artifact(contract_repo, tmp_path):
    out = tmp_path / "contract.yaml"
    rc = main(["merge", str(contract_repo / "contracts"), "--service", "s",
               "--repo-root", str(contract_repo), "--out", str(out)])
    assert rc == 0
    import yaml
    assert yaml.safe_load(out.read_text())["service"] == "s"


def test_hash_prints_relation_and_hash(contract_repo, capsys):
    assert main(["hash", str(contract_repo / "contracts"), "--repo-root", str(contract_repo)]) == 0
    line = capsys.readouterr().out.strip()
    rel, h = line.split("\t")
    assert rel == "analytics.t" and h.startswith("sha256:")
```

- [ ] **Step 2: Run → FAIL**  |  **Step 3: Implement**  |  **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: CLI validate/merge/hash` — open **PR 6**.

---

### Task 11: RunContext (PR 7)

**Files:**
- Create: `continuo_python_runtime/context.py`
- Test: `tests/test_context.py`

**Interfaces:**
- Consumes: `Node`, `ReadError`, `RuntimeAdapter` (typing only).
- Produces: `RunContext(node: Node, adapter)` with `read(name: str) -> pa.Table` — unknown name → `ReadError` listing declared names **without calling the adapter**; adapter exception → `ReadError(f"declared read {name!r} failed: {exc}")` chained; results memoized per name (adapter hit once per name per run).

- [ ] **Step 1: Failing tests**

```python
import pyarrow as pa
import pytest

from continuo_python_runtime.context import RunContext
from continuo_python_runtime.errors import ReadError


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def fetch(self, sql):
        self.calls.append(sql)
        return pa.table({"id": [1]})


def test_read_resolves_declared_sql_and_memoizes(node_fixture):
    ad = FakeAdapter()
    ctx = RunContext(node_fixture, ad)
    t1, t2 = ctx.read("ids"), ctx.read("ids")
    assert t1 is t2
    assert ad.calls == ["select id from analytics.a"]


def test_unknown_name_raises_without_adapter_call(node_fixture):
    ad = FakeAdapter()
    with pytest.raises(ReadError, match="undeclared read 'nope'.*declared: \\['ids'\\]"):
        RunContext(node_fixture, ad).read("nope")
    assert ad.calls == []


def test_adapter_failure_wrapped(node_fixture):
    class Boom:
        def fetch(self, sql):
            raise RuntimeError("db down")
    with pytest.raises(ReadError, match="'ids' failed: db down"):
        RunContext(node_fixture, Boom()).read("ids")
```

(`node_fixture` in `tests/conftest.py`: a `Node` built like Task 4's `_node()`.)

- [ ] **Step 2: Run → FAIL**  |  **Step 3: Implement (≈25 lines)**  |  **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: RunContext with declared-reads-only access`

---

### Task 12: Script lint + CLI `lint` (PR 7)

**Files:**
- Create: `continuo_python_runtime/lint.py`
- Modify: `continuo_python_runtime/cli.py` (add subcommand)
- Test: `tests/test_lint.py`

**Interfaces:**
- Produces:
  - `lint_source(source: str, filename: str) -> list[str]` — AST-based violations, each `"<filename>:<lineno>: <rule>"`. Rules: (L1) import of a warehouse driver — root module in `{"psycopg2", "sqlalchemy", "trino", "snowflake", "pyodbc", "duckdb", "sqlite3", "pymysql", "mysql", "clickhouse_driver", "pyhive"}` via `import` or `from ... import`; (L2) any string constant matching `re.compile(r"(?is)\b(select\s.+?\sfrom\s|insert\s+into\s|update\s.+?\sset\s|delete\s+from\s|create\s+table\s)")`; (L3) any call whose attribute name is `read_sql`, `read_sql_query`, `read_sql_table`, `execute`, or `read_database`.
  - `lint_paths(paths: list[Path]) -> list[str]` — walks `*.py` files (a dir → recursive), aggregates.
  - CLI: `continuo-runtime lint <path>...` — prints violations to stderr via logging, exit 1 if any, else 0.

- [ ] **Step 1: Failing tests**

```python
from continuo_python_runtime.lint import lint_source

GOOD = "def run(ctx):\n    ids = ctx.read('ids')\n    return ids\n"


def test_clean_script_passes():
    assert lint_source(GOOD, "s.py") == []


def test_driver_import_flagged():
    assert any("psycopg2" in v for v in lint_source("import psycopg2\n" + GOOD, "s.py"))
    assert any("sqlalchemy" in v for v in lint_source("from sqlalchemy import text\n" + GOOD, "s.py"))


def test_sql_literal_flagged():
    bad = GOOD + "q = 'select a from analytics.t'\n"
    assert any("SQL string literal" in v for v in lint_source(bad, "s.py"))


def test_read_sql_call_flagged():
    bad = GOOD + "def f(pd, c):\n    return pd.read_sql('x', c)\n"
    assert lint_source(bad, "s.py")


def test_violation_carries_location():
    (v,) = lint_source("import psycopg2\n", "scripts/x.py")
    assert v.startswith("scripts/x.py:1:")
```

- [ ] **Step 2: Run → FAIL**  |  **Step 3: Implement with `ast.walk`**  |  **Step 4: Run → PASS** (incl. a CLI test in `tests/test_cli.py`: lint on the good/bad fixtures returns 0/1)
- [ ] **Step 5: Commit** — `feat: no-handwritten-SQL script lint` — open **PR 7**.

---

### Task 13: Script dispatch + harness core (PR 8)

**Files:**
- Create: `continuo_python_runtime/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: everything prior + `continuo_validation_contract.result.result_block` + `continuo_validation_contract.port.discover_runtime_adapter`.
- Produces:
  - `load_script(node: Node, repo_root: Path)` — imports the script file via `importlib.util.spec_from_file_location`; missing file → `ContractError`; module without a callable `run` → `ScriptError`.
  - `select_node(nodes: list[Node], node_id: str) -> Node` — the trailing two dot-segments of `node_id` are `(schema, table)`; no match → `ContractError` listing available relations.
  - `run_node(env: Mapping[str, str], adapter=None) -> int` — the spec §7 sequence. Env keys: `NODE_ID` (required), `TABLE_NAME` (required), `TARGET_SCHEMA` (required), `CONTRACT_DIR` (default `/app/contracts`), `APP_ROOT` (default: parent of `CONTRACT_DIR`) for script resolution. `adapter=None` → entry-point discovery + `from_env()` (injectable for tests). Script stdout redirected to stderr (`contextlib.redirect_stdout(sys.stderr)`) around the `run()` call. On success prints `result_block("success", message=f"rows={n}", unique_id=node_id)` and returns 0; on `HarnessError` prints `result_block("error", message=err.sentinel_message(), failures=1, unique_id=node_id)` and returns 1; on any other exception wraps in `ScriptError` first. The two `print` calls here are the only stdout writes in the package.
  - Write path: `adapter.ensure_table(target_schema, table_name, [asdict-style col dicts])` then `adapter.load(target_schema, table_name, conformed)`; adapter exceptions in this phase → `LoadError`.

- [ ] **Step 1: Failing tests** — with a full fake adapter in `tests/conftest.py`:

```python
class FakeRuntimeAdapter:
    def __init__(self, tables=None):
        self.tables = tables or {}          # sql -> pa.Table
        self.loaded = None
        self.ensured = None

    def fetch(self, sql):
        return self.tables[sql]

    def ensure_table(self, schema, table, columns):
        self.ensured = (schema, table, columns)

    def load(self, schema, table, data):
        self.loaded = (schema, table, data)

    def close(self):
        pass
```

and a `harness_repo` fixture (tmp dir with `contracts/t.yml` + `scripts/t.py` from Task 9's shapes, script returning `ctx.read("ids")`). Tests:

```python
def _env(repo):
    return {
        "NODE_ID": "python-model.svc.analytics.t",
        "TABLE_NAME": "t",
        "TARGET_SCHEMA": "analytics",
        "CONTRACT_DIR": str(repo / "contracts"),
        "APP_ROOT": str(repo),
    }


def test_success_emits_single_sentinel_block(harness_repo, capsys):
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1, 2]})})
    assert run_node(_env(harness_repo), adapter=ad) == 0
    out = capsys.readouterr().out
    assert out.count("===CONTINUO_VALIDATION_RESULT_BEGIN===") == 1
    body = json.loads(out.split("BEGIN===\n")[1].split("\n===CONTINUO")[0])
    assert body["status"] == "success"
    assert ad.loaded[0:2] == ("analytics", "t")
    assert ad.ensured is not None


def test_script_print_cannot_corrupt_stdout(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text(
        "def run(ctx):\n    print('noise')\n    return ctx.read('ids')\n"
    )
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"id": [1]})})
    run_node(_env(harness_repo), adapter=ad)
    out = capsys.readouterr().out
    assert "noise" not in out


def test_unknown_node_id_is_contract_error(harness_repo, capsys):
    env = _env(harness_repo) | {"NODE_ID": "python-model.svc.analytics.nope"}
    assert run_node(env, adapter=FakeRuntimeAdapter()) == 1
    assert '"message":"ContractError:' in capsys.readouterr().out


def test_conform_violation_is_conform_error(harness_repo, capsys):
    ad = FakeRuntimeAdapter({"select id from analytics.a": pa.table({"wrong": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    assert '"message":"ConformError:' in capsys.readouterr().out
    assert ad.loaded is None


def test_script_exception_is_script_error(harness_repo, capsys):
    (harness_repo / "scripts" / "t.py").write_text("def run(ctx):\n    raise ValueError('x')\n")
    assert run_node(_env(harness_repo), adapter=FakeRuntimeAdapter()) == 1
    assert '"message":"ScriptError:' in capsys.readouterr().out


def test_load_failure_is_load_error(harness_repo, capsys):
    class BadLoad(FakeRuntimeAdapter):
        def load(self, schema, table, data):
            raise RuntimeError("disk full")
    ad = BadLoad({"select id from analytics.a": pa.table({"id": [1]})})
    assert run_node(_env(harness_repo), adapter=ad) == 1
    assert '"message":"LoadError:' in capsys.readouterr().out
```

- [ ] **Step 2: Run → FAIL**  |  **Step 3: Implement `harness.py` per Interfaces**  |  **Step 4: Run → PASS**
- [ ] **Step 5: Commit** — `feat: harness — sole write sink, sentinel envelope, error taxonomy`

---

### Task 14: CLI `run` (PR 8)

**Files:**
- Modify: `continuo_python_runtime/cli.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Produces: `continuo-runtime run` — no positional args; reads `os.environ`, calls `run_node(os.environ)`, returns its exit code. This is the image entrypoint.

- [ ] **Step 1: Failing test** — monkeypatch env with the Task 13 fixture + monkeypatch `discover_runtime_adapter`-based construction by injecting via `continuo_python_runtime.cli`'s seam: implement `run` to call `harness.run_node(os.environ)`, and in the test monkeypatch `harness.build_adapter` (a one-line module function `build_adapter() -> RuntimeAdapter` that `run_node` uses when `adapter is None`) to return `FakeRuntimeAdapter(...)`. Assert exit code 0 and one sentinel block.
- [ ] **Step 2: Run → FAIL**  |  **Step 3: Implement (add `build_adapter()` seam to `harness.py` if Task 13 didn't; wire subcommand)**  |  **Step 4: Full suite + ruff + mypy → PASS**
- [ ] **Step 5: Commit** — `feat: continuo-runtime run entrypoint` — open **PR 8**.

---

### Task 15: Dockerfile.postgres + image CI + smoke test (PR 9)

**Files:**
- Create: `Dockerfile.postgres`, `tests/smoke/node_smoke/` fixture service (contracts + script), `.github/workflows/images.yml`
- Modify: `pyproject.toml` (drop the `[tool.uv.sources]` git override — depend on published `continuo-validation-contract==0.3.0`; requires PR 1 released to PyPI first)

**Interfaces:**
- Produces: image `continuo-python-runtime:<version>-postgres` whose entrypoint is `continuo-runtime run`, with `continuo-runtime-postgres` (from continuo-validation-runners, ext-A) installed as the single runtime adapter.

- [ ] **Step 1: Write `Dockerfile.postgres`**

```dockerfile
FROM python:3.14-slim
RUN pip install --no-cache-dir \
    continuo-python-runtime==0.1.0 \
    continuo-runtime-postgres==0.1.0
ENV CONTRACT_DIR=/app/contracts APP_ROOT=/app
WORKDIR /app
ENTRYPOINT ["continuo-runtime", "run"]
```

(Exact version pins updated at release time; domain images `COPY contracts/ /app/contracts/` and `COPY scripts/ /app/scripts/`.)

- [ ] **Step 2: Write the smoke fixture** — `tests/smoke/node_smoke/contracts/smoke.yml` (one node, `reads: {ids: select id from analytics.src}`, `output_columns: [{name: id, type: INTEGER}]`) and `scripts/smoke.py` (`def run(ctx): return ctx.read("ids")`).

- [ ] **Step 3: Write `.github/workflows/images.yml`** — on tag `v*` and PR touching `Dockerfile.*`: build the image; smoke job runs a `postgres:16` service container, creates `analytics.src` with `psql`, then runs the built image with `NODE_ID=python-model.smoke.analytics.smoke TABLE_NAME=smoke TARGET_SCHEMA=analytics POSTGRES_HOST=... POSTGRES_DB=... POSTGRES_USER=...` and the fixture mounted at `/app`; asserts exit 0 and that stdout contains `"status":"success"`; a follow-up `psql -c "select count(*) from analytics.smoke"` returns the seeded row count. On tag: also `uv build && uv publish` (PyPI) and push the image to the registry.

- [ ] **Step 4: Verify locally** — `docker build -f Dockerfile.postgres .` succeeds; run the smoke sequence against `docker run -d postgres:16` and confirm the sentinel `success` block and the row count.

- [ ] **Step 5: Commit** — `feat: postgres base image + smoke pipeline` — open **PR 9** (merges only after ext-A publishes `continuo-runtime-postgres`).

---

### Task 16: Template (PR 10)

**Files:**
- Create: `template/contracts/example.yml`, `template/scripts/example.py`, `template/Dockerfile`, `template/.github/workflows/release.yml`, `template/README.md`
- Test: `tests/test_template.py`

**Interfaces:**
- Produces: a copy-ready domain repo implementing spec §9's six-step pipeline.

- [ ] **Step 1: Write the template files**

`template/contracts/example.yml`:

```yaml
nodes:
  - schema: analytics
    table: example
    description: "Replace with your node"
    owner: your-team
    schedule: daily
    criticality: SECONDARY
    script: scripts/example.py
    reads:
      ids: select id from analytics.some_upstream
    output_columns:
      - {name: id, type: INTEGER, nullable: false}
```

`template/scripts/example.py`:

```python
def run(ctx):
    """Return the node's output; the harness conforms and writes it."""
    return ctx.read("ids")
```

`template/Dockerfile`:

```dockerfile
FROM ghcr.io/carolsimone/continuo-python-runtime:0.1.0-postgres
COPY contracts/ /app/contracts/
COPY scripts/ /app/scripts/
# Add your dataframe libraries here, e.g.:
# RUN pip install --no-cache-dir polars==1.34.0
```

`template/.github/workflows/release.yml` — full six-step pipeline:

```yaml
name: release
on:
  push: { branches: [main] }
env:
  SERVICE: your-service-name          # one service name per domain repo
jobs:
  release:
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write, id-token: write }
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: Install runtime tooling
        run: uv tool install continuo-python-runtime
      - name: Lint scripts (no hand-written SQL)
        run: continuo-runtime lint scripts/
      - name: Validate contracts
        run: continuo-runtime validate contracts/
      - name: Domain tests
        run: |
          if [ -d tests ]; then uv run --with pytest pytest -v; fi
      - name: Compute release id
        run: echo "RELEASE_ID=${{ github.run_number }}-${GITHUB_SHA::7}" >> "$GITHUB_ENV"
      - name: Merge contracts
        run: continuo-runtime merge contracts/ --service "$SERVICE" --repo-root . --out contract.yaml
      - name: Build and push image
        run: |
          IMAGE="${{ vars.REGISTRY }}/${SERVICE}:${RELEASE_ID}"
          docker build -t "$IMAGE" .
          docker push "$IMAGE"
          echo "IMAGE_TAG=$IMAGE" >> "$GITHUB_ENV"
      - name: Upload contract artifact
        run: aws s3 cp contract.yaml "s3://${{ vars.BUCKET }}/${SERVICE}/${RELEASE_ID}/contract.yaml"
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
      - name: POST /releases            # strictly after build+push and upload
        run: |
          curl --fail-with-body -X POST "${{ vars.RELEASE_ENDPOINT }}/releases" \
            -H 'Content-Type: application/json' \
            -d "{\"service\":\"${SERVICE}\",\"release_id\":\"${RELEASE_ID}\",\"image_tag\":\"${IMAGE_TAG}\",\"repo\":\"${{ github.repository }}\",\"commit_sha\":\"${GITHUB_SHA}\",\"kind\":\"python\"}"
```

`template/README.md` — short: copy this dir, rename service, set repo vars (`REGISTRY`, `BUCKET`, `RELEASE_ENDPOINT`) and secrets (AWS + registry creds) per the provisioning handshake, write contracts + scripts, push to main.

- [ ] **Step 2: Write the golden test**

```python
from pathlib import Path

from continuo_python_runtime.cli import main

TEMPLATE = Path(__file__).parent.parent / "template"


def test_template_passes_lint_validate_merge(tmp_path):
    assert main(["lint", str(TEMPLATE / "scripts")]) == 0
    assert main(["validate", str(TEMPLATE / "contracts")]) == 0
    out = tmp_path / "contract.yaml"
    assert main(["merge", str(TEMPLATE / "contracts"), "--service", "example",
                 "--repo-root", str(TEMPLATE), "--out", str(out)]) == 0
    assert out.exists()
```

- [ ] **Step 3: Run → PASS** (fix template until green — the test is the point: teams never copy a broken start)
- [ ] **Step 4: Commit** — `feat: domain-repo template + golden test`

---

### Task 17: README rewrite (PR 10)

**Files:**
- Modify: `README.md` (currently a verbatim copy of the parent design's §13)

- [ ] **Step 1: Rewrite** — sections: what this repo is (the three artifacts), quickstart for domain teams (copy `template/`, the 3 repo vars + secrets, write a contract + script, push), the script API (`run(ctx)`, `ctx.read(name)`, Arrow currency, dataframe-lib freedom), the conform rules table (strict cast, varchar, null, extra-column policy), the error taxonomy table, engine selection (`FROM …-postgres` / `…-trino`), and a pointer to `docs/superpowers/specs/2026-07-31-python-runtime-design.md` + the parent design for the boundary contract. Keep the §13 boundary text in a `docs/boundary-contract.md` file rather than deleting it.
- [ ] **Step 2: Verify** — `uv run pytest` still green; links resolve.
- [ ] **Step 3: Commit** — `docs: README for domain teams` — open **PR 10**.

---

### Task 18: Dockerfile.trino + matrix (PR 11 — after ext-B publishes)

**Files:**
- Create: `Dockerfile.trino`
- Modify: `.github/workflows/images.yml` (matrix `engine: [postgres, trino]`), `template/Dockerfile` comment noting the trino base tag exists

- [ ] **Step 1: Write `Dockerfile.trino`** — identical to `Dockerfile.postgres` with `continuo-runtime-trino==<version>` as the single adapter.
- [ ] **Step 2: Extend the images workflow** to build/tag both engines (`<version>-postgres`, `<version>-trino`); trino smoke runs against `trinodb/trino` service container with a memory-connector catalog, mirroring the postgres smoke sequence.
- [ ] **Step 3: Verify** — both images build locally; smoke green in CI.
- [ ] **Step 4: Commit** — `feat: trino base image` — open **PR 11**.
