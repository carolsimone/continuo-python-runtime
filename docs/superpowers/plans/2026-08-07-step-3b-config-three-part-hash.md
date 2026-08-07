# Step 3b — physical-layout `config` + three-part content hash

Repo: `continuo-python-runtime`. Branch: `feat/step-3b-config-three-part-hash`.

## Context

Continuo's manifest-controller (continuo PR #403) now parses this repo's wire
artifact with `parse_python_contract()`. That parser **requires** every node
entry to carry four hash fields — `source_hash`, `shared_code_hash`,
`config_hash`, `content_hash` — recomputes
`content_hash = "sha256:" + sha256(f"{source_hash}|{shared_code_hash}|{config_hash}")`
and **rejects the whole artifact on mismatch**. It also accepts an optional flat
`config` mapping per node and **rejects any entry field it does not know**.

This repo's merger emits none of that: it still writes a single `content_hash`
computed by the superseded formula (canonical entry + `\x00` + script bytes).
**Until this step lands, no artifact this repo produces can be parsed.**

Two things ship together here:

1. **The three-part hasher** replacing `continuo_python_runtime/hashing.py`,
   including a static-AST **in-repo import closure** so a byte edit to a shared
   module flips exactly the nodes that reach it.
2. **Physical-layout `config`** — an optional flat per-node mapping in the
   engine's own vocabulary, threaded model → loader → merger → wire entry, and
   applied by `RuntimeAdapter.ensure_table` on create-if-absent, failing closed
   on any key the engine does not recognize.

### The consumer's exact wire-entry key set (do not drift from this)

`parse_python_contract` accepts **exactly** these entry keys and rejects the
artifact on any other:

- required: `schema`, `table`, `owner`, `schedule`, `criticality`, `script`,
  `reads`, `output_columns`, `source_hash`, `shared_code_hash`, `config_hash`,
  `content_hash`
- optional: `description`, `extra_columns`, `config`
- `output_columns` entries accept only `name`, `type`, `nullable`

### Reference implementation this repo's fold must match byte-for-byte

`continuo/manifest-controller/service/content_hash.py`:

```python
def content_hash_fold(source_hash: str, shared_code_hash: str, config_hash: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"{source_hash}|{shared_code_hash}|{config_hash}".encode()
    ).hexdigest()
```

Its dbt-side shared-code fold (`parser.py::_shared_code_hash`), which ours
mirrors:

```python
unit_hashes = sorted(sha256(source.encode()).hexdigest() for each unit)
"" if not unit_hashes else sha256("".join(unit_hashes).encode()).hexdigest()
```

## Global Constraints

- **Never run `git add -A` / `git add .`** — stage every path explicitly.
  `docs/superpowers/2026-08-07-step-3b-kickoff.md` is deliberately untracked and
  local-only; it must never be staged or committed.
- **Bare hex for the three parts, `"sha256:"`-prefixed only for `content_hash`.**
  `shared_code_hash` is the empty string `""` when the closure is empty — not a
  hash of empty input, not `None`.
- Python 3.14. Diagnostics go through the stdlib `logging` module to stderr —
  **never `print`**, except the harness's one sentinel-framed result block on
  stdout.
- Every task ends green on: `uv run ruff check .`,
  `uv run mypy continuo_python_runtime`,
  `uv run --package continuo-python-runtime-postgres mypy python-runtime-postgres/continuo_python_runtime_postgres`,
  `uv run --package continuo-python-runtime-trino mypy python-runtime-trino/continuo_python_runtime_trino`,
  `uv run pytest -q`, and
  `uv run pytest python-runtime-postgres/tests python-runtime-trino/tests -m "not integration" -q`.
  Baseline at branch point: 212 runtime tests + 91 adapter tests, all passing.
- Test module basenames must be unique across the root suite and both adapter
  suites (see the `[tool.pytest.ini_options]` comment in `pyproject.toml`).
- Fail closed everywhere: an unknown `config` key, an unresolvable construct, a
  dynamic import — all reject rather than degrade.
- Test-drive the work: write the failing test first, then the implementation.
  No test that asserts nothing; no assertions on log text as a proxy for
  behavior.
- Do not touch `.github/workflows/publish-pypi.yml` or `images.yml` release
  triggers, and **do not create git tags or publish anything**. Version bumps
  are in scope (Task 9); cutting the release is not.

---

## Task 1: `config` on the contract model and loader

Add the optional flat physical-layout `config` mapping to the authored contract.

### `continuo_python_runtime/contract/model.py`

Add to `Node`, after `extra_columns` and before `content_hash`:

```python
config: dict[str, Any] = field(default_factory=dict)
```

Import `field` from `dataclasses` and `Any` from `typing`. `Node` stays
`@dataclass(frozen=True)`; it already carries a `dict` field (`reads`), so
nothing changes about its hashability contract.

### `continuo_python_runtime/contract/loader.py`

1. Add `"config"` to `_ALLOWED_KEYS`.
2. Validate it as *shape only* — engines own the semantics (design §3.3: the
   control plane and this loader stay engine-blind). Add a module-level helper
   and call it from `parse_node` after the `description` check:

```python
_JSON_SCALARS = (str, int, float, bool, type(None))


def _validate_config(raw: Any, label: str) -> dict[str, Any]:
    """Validate the node's physical-layout `config` as a JSON-shaped mapping.

    The engine's adapter — not this loader — owns the vocabulary (§3.3), so the
    only rules here are the ones the hash and the wire format need: it is a
    mapping, every key at every level is a string, and every value is
    JSON-serializable. Non-string keys would make `json.dumps(..., sort_keys=True)`
    raise inside the hasher, and a non-serializable value would break the wire
    artifact — both must surface here, naming the node, not deep in CI.
    """
```

Rules it enforces, each raising `ContractError` prefixed with `label`:
- `config` absent → return `{}`.
- not a `dict` → `f"{label}: 'config' must be a mapping"`.
- any key at any nesting depth not a `str` →
  `f"{label}: 'config' keys must be strings, got {key!r}"`.
- any value that is not a `str`/`int`/`float`/`bool`/`None`/`list`/`dict` →
  `f"{label}: 'config' value for {key!r} is not JSON-serializable: {value!r}"`.
- recurse into nested `dict` values and into `list` elements.

Note: `bool` is a subclass of `int`, which is fine — both are JSON scalars.

3. Pass `config=config` to the `Node(...)` construction.

### Tests — `tests/contract/test_model.py` and `tests/contract/test_loader.py`

Match the existing style in those files (plain `pytest`, `ContractError`
assertions via `pytest.raises` with a message substring). Cover:

- a node with no `config` key loads with `node.config == {}`
- a node with `config: {indexes: [{columns: [id], unique: true}]}` loads with
  that exact nested structure preserved
- `config: "not-a-mapping"` raises `ContractError` mentioning `config`
- `config: {1: "x"}` (non-string key) raises `ContractError`
- a nested non-string key — `config: {indexes: [{1: "x"}]}` — raises
- an unknown *vocabulary* key such as `config: {sortkey: [id]}` **loads fine**
  here (the engine adapter rejects it later, Task 6) — assert this explicitly so
  a future reviewer sees the engine-blindness is deliberate
- `config: {}` loads with `node.config == {}`

Commit message: `feat(contract): optional physical-layout config on the node model`

---

## Task 2: In-repo import-closure resolver

New module `continuo_python_runtime/closure.py`. Pure, filesystem-reading, no
network. This is what closes the shared-module gap: a byte edit to a module the
script imports must flip that script's node.

### Public API

```python
def resolve_closure(script_path: Path, repo_root: Path) -> list[Path]:
    """Return the transitive in-repo import closure of *script_path*.

    Absolute, resolved paths, sorted, with *script_path* itself EXCLUDED (it is
    `source_hash`). Files that do not resolve under *repo_root* — stdlib,
    site-packages, anything installed — are not closure members: external deps
    are the image's concern (`image_tag`), not the hash's.
    """
```

### Algorithm (specified exactly — implement this, do not improvise)

Breadth-first from `script_path`. Maintain `seen: set[Path]` of resolved
absolute paths already visited (starts containing the resolved `script_path`).

For each file visited:

1. `source = path.read_bytes()`; `tree = ast.parse(source)`. On `SyntaxError`
   raise `ContractError(f"{rel}: syntax error: {exc.msg}")` where `rel` is the
   path relative to `repo_root`.
2. **Reject dynamic-import constructs** (fail closed — what static analysis
   cannot see must not exist). Detection lives in a reusable module-level
   helper, because Task 5's CI lint reports the same constructs and there must
   be exactly one definition of the rule:

   ```python
   def dynamic_import_violations(tree: ast.AST) -> list[tuple[int, str]]:
       """Return `(lineno, construct)` for every dynamic-import construct in *tree*.

       `construct` is the offending name as written: "importlib", "__import__",
       "exec", "eval", or "import_module". Sorted by lineno, then construct.
       """
   ```

   `resolve_closure` raises
   `ContractError(f"{rel}:{lineno}: dynamic import construct {construct!r} is not allowed — the content hash cannot see it")`
   for the first violation the helper reports. The constructs are:
   - an `ast.Import`/`ast.ImportFrom` whose root module is `importlib`
   - any `ast.Name` whose `id` is `"__import__"`, `"exec"`, or `"eval"` (catches
     calling them, aliasing them, and passing them as callables). `eval` is in
     the list because it opens exactly the same hole as `exec`.
   - any `ast.Attribute` whose `attr` is `"import_module"` (catches
     `il.import_module(...)` after `import importlib as il` — belt and braces
     with the first rule)
3. Collect imported dotted module names:
   - `ast.Import`: each `alias.name` verbatim (`import a.b.c` → `"a.b.c"`).
   - `ast.ImportFrom` with `node.level == 0`: `node.module`, **plus**
     `f"{node.module}.{alias.name}"` for each alias — `from pkg import mod` may
     be importing a submodule, and over-inclusion is the safe direction.
   - `ast.ImportFrom` with `node.level > 0` (relative): resolve against the
     importing file's package. The importing file's package is the dotted path
     of its parent directory relative to `repo_root`; walk up `level - 1`
     additional components. Then append `node.module` (when present) and each
     alias name as above. If walking up would escape `repo_root`, skip the
     import (it cannot name an in-repo file).
4. Resolve each dotted name to a file, trying **search roots in this order**:
   `repo_root`, then the importing file's own directory. For a name `a.b.c` and
   a root `R`, the candidates are, in order:
   - `R/a/b/c.py`
   - `R/a/b/c/__init__.py`

   First existing regular file wins; if neither exists under any root, the name
   is external — skip it.
5. When a name resolves, also add the `__init__.py` of every ancestor package
   along the resolved path that exists (`R/a/__init__.py`, `R/a/b/__init__.py`)
   — those files execute on import, so a byte edit to one is a real change.
6. Discard any resolved path that is not under `repo_root` once resolved
   (symlink escape), and any path already in `seen`. Enqueue the rest.

Return `sorted(seen - {resolved script_path})`.

### Why over-inclusion is acceptable (put this in the module docstring)

Rule 4's second search root and rule 3's alias expansion can pull in a file
Python would not actually import. That direction is deliberate: a spurious
closure member causes a spurious revalidation, while a missed one causes a
silently stale node in production. The same trade-off manifest-controller's dbt
parser makes for unknown config keys.

### Tests — new file `tests/test_closure.py`

Use `tmp_path` to build small repos. Cover:

- script with no imports → `[]`
- `import helpers` where `<repo>/helpers.py` exists → `[helpers.py]`
- `import scripts.helpers` from `<repo>/scripts/node.py` where
  `<repo>/scripts/helpers.py` and `<repo>/scripts/__init__.py` exist → both the
  helper **and** `scripts/__init__.py` are members
- `import helpers` from `<repo>/scripts/node.py` where `<repo>/scripts/helpers.py`
  exists (script-directory search root) → resolved
- transitivity: script → `a.py` → `b.py` returns both
- a cycle (`a.py` imports `b.py`, `b.py` imports `a.py`) terminates and returns
  both, exactly once each
- the script itself is never a member, even when a closure member imports it
  back
- `import json` / `import pyarrow` with no such in-repo file → not members
- `from . import sibling` relative import inside a package resolves
- `from .. import other` that would escape `repo_root` is skipped, not an error
- each rejected construct raises `ContractError`: `import importlib`,
  `from importlib import import_module`, `__import__("x")`, `exec("x")`,
  `eval("x")`, and `il.import_module("x")`
- a rejected construct **in a closure member** (not the script) also raises
- a syntax error in a closure member raises `ContractError`
- determinism: the returned list is sorted and stable across two calls

Commit message: `feat(hashing): static in-repo import-closure resolver`

---

## Task 3: Three-part hasher

Replace `continuo_python_runtime/hashing.py` wholesale. The old single-hash
formula (canonical entry + `\x00` + script bytes) is **superseded — delete it**,
do not keep it alongside. `tests/test_hashing.py` is rewritten for the new API.

### New `continuo_python_runtime/hashing.py`

```python
HASH_PART_FIELDS = ("source_hash", "shared_code_hash", "config_hash")
CONTENT_HASH_FIELD = "content_hash"


def canonical_entry(entry: dict) -> dict:
    """Deep-copy *entry*, drop the four hash fields, whitespace-normalize reads."""


def canonical_json(entry: dict) -> str:
    """`json.dumps(canonical_entry(entry), sort_keys=True, separators=(",", ":"))`."""


def source_hash(script_bytes: bytes) -> str:
    """Bare hex `sha256(script_bytes)` — the node's own script, byte-for-byte."""


def shared_code_hash(member_bytes: Iterable[bytes]) -> str:
    """`""` when the closure is empty, else bare hex of the sorted-digest fold."""


def config_hash(entry: dict) -> str:
    """Bare hex `sha256(canonical_json(entry).encode())`."""


def content_hash_fold(source: str, shared: str, config: str) -> str:
    """`"sha256:" + sha256(f"{source}|{shared}|{config}".encode()).hexdigest()`."""


def hash_parts(
    entry: dict, script_bytes: bytes, member_bytes: Iterable[bytes]
) -> dict[str, str]:
    """Return all four fields: the three parts plus their fold."""
```

Exact semantics:

- `canonical_entry` drops `content_hash` **and** all three of
  `HASH_PART_FIELDS` from the basis, and whitespace-normalizes every `str` value
  in `reads` via `" ".join(sql.split())`. Everything else is preserved verbatim,
  including `config`.
- `shared_code_hash`: compute `sha256(member).hexdigest()` for each member,
  `sorted()` those hex strings, `"".join(...)`, `.encode()`, `sha256(...)`,
  `.hexdigest()`. Empty input → `""` exactly.
- `hash_parts` returns
  `{"source_hash": s, "shared_code_hash": sh, "config_hash": c, "content_hash": content_hash_fold(s, sh, c)}`.

### Tests — rewrite `tests/test_hashing.py`

**Known vectors pinned to continuo's fold** (these are the exact assertions in
`continuo/manifest-controller/tests/test_content_hash.py`; they must pass here
byte-identically):

```python
assert content_hash_fold("aaa", "bbb", "ccc") == "sha256:" + hashlib.sha256(b"aaa|bbb|ccc").hexdigest()
assert content_hash_fold("aaa", "", "ccc")    == "sha256:" + hashlib.sha256(b"aaa||ccc").hexdigest()
```

plus: changing any one of the three parts flips the fold (three assertions off a
common base, as in the reference test).

Invariant tests (design §10):

- `source_hash(b"x") == hashlib.sha256(b"x").hexdigest()` and carries no
  `"sha256:"` prefix
- `shared_code_hash([]) == ""`
- `shared_code_hash` is order-insensitive: `[a, b]` and `[b, a]` agree
- `shared_code_hash([a])` changes when `a`'s bytes change by one byte
- `config_hash` is insensitive to SQL whitespace runs in `reads` values and to
  yaml key order (build the same entry with `dict(reversed(list(entry.items())))`)
- `config_hash` **is** sensitive to a `config` edit — same entry with
  `config: {}` vs `config: {"indexes": [{"columns": ["id"]}]}` differ
- `config_hash` ignores a stale `content_hash` **and** stale part fields already
  present on the entry
- `canonical_entry` does not mutate its argument
- `hash_parts` returns exactly the four expected keys, and its `content_hash`
  equals `content_hash_fold` over its own three parts

Commit message: `feat(hashing): three-part content hash replacing the single-hash formula`

---

## Task 4: Merger emits `config` and all four hash fields

Wire Tasks 1–3 into `continuo_python_runtime/contract/merge.py` so the artifact
this repo produces is one `parse_python_contract` accepts.

### `node_entry(node)`

Add `"config": dict(node.config)` to the returned dict, after `extra_columns`.
**Always emit it, even when empty** — an absent-vs-`{}` difference would change
`config_hash`, and a stable basis is worth more than a shorter artifact. Update
the docstring: the entry carries no hash fields; `build_wire_contract` adds all
four.

### `build_wire_contract(contract_dir, repo_root, service)`

Per node, replacing the current single `content_hash` call:

```python
script_path = resolve_script_path(node.script, repo_root, context=node.relation)
script_bytes = script_path.read_bytes()
closure = resolve_closure(script_path, repo_root)
member_bytes = [member.read_bytes() for member in closure]
entry.update(hash_parts(entry, script_bytes, member_bytes))
```

`resolve_closure` raises `ContractError` on a dynamic-import construct or a
syntax error in the script or any member — let it propagate; the CLI already
turns `HarnessError` into a logged failure with exit 1.

Sorting by `schema.table` and `write_wire_contract` are unchanged.

### Template

`template/contracts/example.yml`: add a commented-out `config:` block showing the
postgres vocabulary, immediately after `output_columns`, so the shape is
discoverable:

```yaml
    # Optional physical layout, in your ENGINE'S OWN vocabulary. The active
    # engine's adapter rejects any key it does not recognize.
    #   postgres: indexes
    #   trino (iceberg): partitioning, sorted_by, format
    # config:
    #   indexes:
    #     - columns: [order_id]
    #       unique: true
```

### Tests — `tests/contract/test_merge.py`

- every wire entry carries exactly the keys
  `{schema, table, owner, schedule, criticality, script, reads, output_columns,
  description, extra_columns, config, source_hash, shared_code_hash,
  config_hash, content_hash}` — assert the **set** so an extra key that would
  make `parse_python_contract` reject the artifact fails here first
- `content_hash` starts with `"sha256:"`; the three parts do not
- `content_hash` equals `content_hash_fold` over the entry's own three parts
  (this is precisely the check manifest-controller re-runs)
- a node whose script imports nothing in-repo has `shared_code_hash == ""`
- a node whose script imports an in-repo helper has a non-empty
  `shared_code_hash`, and editing **one byte of the helper** changes that node's
  `content_hash` while leaving an unrelated node's `content_hash` untouched
  (the "flips exactly the nodes that reach it" invariant)
- reformatting a read's whitespace leaves `content_hash` unchanged
- adding/editing `config` changes `content_hash` via `config_hash`
- a script containing `exec("...")` makes `build_wire_contract` raise
  `ContractError`

Commit message: `feat(contract): merger emits config and the four hash fields`

---

## Task 5: CI lint rejects dynamic-import constructs

`continuo-runtime lint scripts/` must reject the same constructs Task 2's
resolver rejects, so authors see the failure at the lint step rather than at
merge time.

### `continuo_python_runtime/lint.py`

Add rule **L5** to `lint_source`, documented in its docstring alongside L1–L4:

> L5: dynamic-import construct (`importlib` in any form, `__import__`, `exec`,
> `eval`). Static analysis computes the content hash's import closure; what it
> cannot see must not exist.

Detection is already implemented: import
`dynamic_import_violations` from `continuo_python_runtime.closure` (Task 2) —
do **not** write a second copy of the rule. `closure.py` raises `ContractError`
off its first hit; `lint.py` formats **every** hit as
`f"{filename}:{lineno}: dynamic import construct '{construct}'"` and appends it
to `violations`. The `construct` string is the offending name as written:
`"importlib"`, `"__import__"`, `"exec"`, `"eval"`, or `"import_module"`.

Ordering: `lint_source` returns violations in the order it finds them; keep L5
hits alongside the others rather than segregating them.

### Tests — `tests/test_lint.py`

Add to the existing suite:

- `import importlib` → one L5 violation naming `importlib`
- `import importlib.util` → one L5 violation
- `from importlib import import_module` → one L5 violation
- `__import__("os")` → one L5 violation
- `exec("x = 1")` → one L5 violation
- `eval("1 + 1")` → one L5 violation
- `il.import_module("x")` → one L5 violation naming `import_module`
- a clean script produces no L5 violation (guard against over-matching — e.g. a
  variable named `execute_something` or an attribute `obj.evaluate` must not fire)
- the violation string carries the filename and the line number

Commit message: `feat(lint): reject dynamic-import constructs`

---

## Task 6: `ensure_table` applies `config`, failing closed

Both engine adapters gain physical-layout support on create-if-absent, and the
harness threads the node's `config` through.

### Signature (both adapters)

```python
def ensure_table(
    self,
    schema: str,
    table: str,
    columns: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
) -> None:
```

`config` is keyword-defaulted because the abstract `RuntimeAdapter.ensure_table`
in `continuo-validation-contract==0.4.0` does not declare it — contract 0.5.0
makes it normative (continuo-validation step 3a). The harness passes it as a
keyword **unconditionally**; document that requirement in Task 7's docs update.

**Validate `config` before any DDL runs.** A bad config must not leave a created
table behind. Order in both adapters: validate `config` → validate column types
→ ensure schema → create table → (postgres only) create indexes.

### `python-runtime-postgres/continuo_python_runtime_postgres/adapter.py`

Vocabulary — **`indexes` is the only recognized key**:

```yaml
config:
  indexes:
    - columns: [id]          # required, non-empty list of declared column names
      unique: true           # optional, default false
      name: ix_custom        # optional, default derived
```

Rules, each raising `ValueError` with a message naming the offending key:
- any top-level key other than `indexes` → reject
- `indexes` not a list → reject; each element must be a mapping
- any index key other than `columns`/`unique`/`name` → reject
- `columns` missing, not a list, empty, or containing a non-string → reject
- a column name not present in the `columns` argument's `name` values → reject
  (this is §10's "an index on a nonexistent column rejects, fail closed")
- `unique` present and not a `bool` → reject
- `name` present and not a non-empty string → reject

Index name when `name` is absent: `f"ix_{table}_{'_'.join(cols)}"` truncated to
63 bytes (postgres's identifier limit — truncating here keeps the emitted name
equal to the name postgres would store). Emit:

```sql
CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON <schema>.<table> (<cols>)
```

built with `pg_sql.Identifier` for the index name, schema, table, and every
column — never raw interpolation. Run the index statements in the same
`with self._conn.cursor()` block that created the table, committing once after
the table and all its indexes, rolling back and re-raising on any error. Log
each index at `INFO` with the same phrasing style as the existing
`"ensuring table %s.%s exists"`.

### `python-runtime-trino/continuo_python_runtime_trino/adapter.py`

Vocabulary — Iceberg's own property names (**decision of record**: the design
doc's `partitioned_by` is Hive-connector spelling; this adapter targets the
Iceberg connector, where the property is `partitioning`. Only the names below
are recognized):

```yaml
config:
  partitioning: ["day(event_ts)", "region"]   # list of non-empty strings
  sorted_by: ["id"]                           # list of non-empty strings
  format: PARQUET                             # one of PARQUET, ORC, AVRO
```

Rules, each raising `ValueError` naming the offending key:
- any key other than `partitioning`/`sorted_by`/`format` → reject
- `partitioning`/`sorted_by` not a list, empty, or containing a non-string or an
  empty string → reject
- `format` not a string, or not in `{"PARQUET", "ORC", "AVRO"}` after
  `.upper()` → reject

Applied as a `WITH (...)` clause on the existing `CREATE TABLE IF NOT EXISTS`
(Iceberg table properties are set at creation):

```sql
CREATE TABLE IF NOT EXISTS <ref> (<cols>) WITH (partitioning = ARRAY['...'], sorted_by = ARRAY['...'], format = 'PARQUET')
```

Property order in the clause: `partitioning`, `sorted_by`, `format` — a fixed
order, so the emitted statement is deterministic and testable. Omit the whole
`WITH (...)` when no recognized key is present. Every value goes through the
existing `_sql_string()` helper (single-quoted, `'` escaped) — partition
transforms like `day(event_ts)` are expressions Trino parses out of the string
literal, so they cannot be identifier-quoted.

### `continuo_python_runtime/harness.py`

```python
active_adapter.ensure_table(target_schema, table_name, columns, config=node.config)
```

The surrounding `try`/`except` already converts a non-`HarnessError` exception
into `LoadError(f"failed to write {target_schema}.{table_name}: {exc}")`, so an
adapter's `ValueError` on a bad config surfaces as a `LoadError` in the sentinel
block. That is the intended runtime fail-closed behavior — add a harness test
proving it.

### Tests

`tests/conftest.py`'s fake adapter `ensure_table` must accept the new keyword;
update it to record the `config` it received.

- `python-runtime-postgres/tests/test_adapter_runtime_postgres.py`: `config=None`
  and `config={}` emit no index DDL; a valid single-column index emits the
  expected `CREATE INDEX IF NOT EXISTS`; `unique: true` emits `CREATE UNIQUE
  INDEX`; a custom `name` is used; the derived name matches
  `ix_<table>_<cols>`; a >63-byte derived name is truncated to 63; each
  rejection rule above raises `ValueError` **and emits no DDL at all**
- `python-runtime-trino/tests/test_adapter_runtime_trino.py`: no recognized key
  → statement has no `WITH`; `partitioning` alone, `sorted_by` alone, and all
  three together produce the exact expected `WITH (...)` text in the fixed
  order; a value containing `'` is escaped; each rejection rule raises
  `ValueError` and emits no DDL
- `tests/test_harness.py`: the harness passes `node.config` through to
  `ensure_table`; an adapter raising `ValueError` from `ensure_table` produces
  an error sentinel block whose message names `LoadError`

Follow each adapter suite's existing fake-connection/statement-capture pattern
rather than introducing a new one.

Commit message: `feat(adapters): ensure_table applies physical-layout config, failing closed`

---

## Task 7: `docs/boundary-contract.md` — hash re-spec, config, dialect rule

`docs/boundary-contract.md` is the **normative** copy of the boundary contract
(the parent design defers to it). It currently documents the superseded
single-hash formula. Update it to describe what this branch actually ships.

### §13.1 — Surface 1

- The node-carries list becomes: `schema`, `table`, `owner`, `schedule`,
  `criticality`, `script`, `reads`, `output_columns`, the four hash fields
  (`source_hash`, `shared_code_hash`, `config_hash`, `content_hash`), and
  optionally `config`.
- Add the **read-dialect rule** (continuo PR #400) as its own bullet: reads are
  parsed, rewritten, and bind-checked by Continuo in the **install's warehouse
  SQL dialect**, so authors write reads in their own engine's dialect; a read
  valid on one engine can fail `InvalidCompiledSql` on another. Mention that
  `continuo-runtime validate|merge|hash --dialect <name>` (Task 8) checks reads
  against that dialect locally.

### §13.2 — Surface 2, rewritten

Replace the whole single-hash block with the three-part spec:

```
source_hash      = sha256(script_bytes)                 # bare hex
shared_code_hash = ""  if the in-repo import closure is empty, else
                   sha256(concat(sorted(sha256(member_bytes))))   # bare hex
config_hash      = sha256(canonical_json(entry))        # bare hex
content_hash     = "sha256:" + sha256(source_hash + "|" + shared_code_hash
                                      + "|" + config_hash)
```

State explicitly:

- `canonical_json` = yaml parsed → JSON, sorted keys, no whitespace, `reads`
  values whitespace-normalized (runs → single space, stripped), with
  `content_hash` **and the three part fields** excluded from the basis.
- The fold is byte-identical to manifest-controller's dbt fold, so one formula
  spans runtimes; manifest-controller **recomputes it and fails the release on
  mismatch**.
- **Closure definition and its documented limits** — this subsection is
  required, not optional:
  - closure = transitive repo-internal `.py` files reachable from the script's
    `import` statements, resolved by static AST analysis; the script itself is
    excluded (it is `source_hash`); stdlib and installed packages are excluded
    (external deps are the image's concern, pinned by `image_tag`).
  - resolution is repo-root-relative dotted lookup plus the importing file's own
    directory, with PEP-328 relative imports resolved against the importing
    file's package; ancestor `__init__.py` files along a resolved path are
    members because they execute on import.
  - **limit — over-inclusion:** `from pkg import name` cannot be statically told
    apart from a submodule import, so a same-named in-repo file is included even
    when the real import was an attribute. Cost is a spurious revalidation.
  - **limit — dynamic imports are rejected, not approximated:** `importlib`,
    `__import__`, `exec`, and `eval` are refused in scripts and in every closure
    member, by both `continuo-runtime lint` and the merger. What static analysis
    cannot see must not exist.
  - **limit — data files are not members:** a script reading a non-`.py` file
    from the repo does not fingerprint it; keep such inputs in the warehouse or
    in a declared read.

### New subsection — physical-layout `config`

Add after §13.1, or as §13.1's own subsection, whichever reads better:

- optional flat per-node mapping, written in the **active engine's own
  vocabulary** — deliberately not engine-namespaced, because the contract is
  engine-bound anyway (reads are authored in the engine's dialect).
- the active engine's adapter **fails closed on any key it does not recognize**;
  there is no other namespace to excuse an unknown key.
- recognized keys as shipped: postgres → `indexes`; trino (Iceberg) →
  `partitioning`, `sorted_by`, `format`. Give the same worked examples as
  `template/contracts/example.yml`.
- applied at runtime by `RuntimeAdapter.ensure_table` on create-if-absent, and
  (once continuo-validation step 3a lands) at validation by
  `build_empty_from_columns`, so a bad config fails the release gate rather than
  production.
- `config` lives in the contract entry, so it lands in `config_hash` — adopting
  or editing it flips `content_hash` with no formula special-casing.

### §13.4 — Surface 4

In the "Sole write sink" bullet, note that `ensure_table` applies the node's
`config` physical layout on create. Add a sentence recording that the harness
calls `ensure_table(..., config=...)` as a keyword unconditionally, so a
`RuntimeAdapter` implementation must accept it; `continuo-validation-contract`
0.5.0 makes it part of the port.

### README

`README.md` mentions the hash and the CLI. Grep it for the superseded formula
and for `content_hash`, and update anything that now contradicts
`boundary-contract.md`. Do not duplicate the spec — point at
`docs/boundary-contract.md`.

No tests for this task; it is documentation. Verify by re-reading the changed
sections against `continuo/manifest-controller/service/content_hash.py` and this
branch's `hashing.py`, and confirm the ruff/mypy/pytest gates still pass.

Commit message: `docs: three-part hash re-spec, physical-layout config, read-dialect rule`

---

## Task 8: Adopt `continuo-validation-contract` 0.4.0 and dedupe

Bump the pin and delete this repo's duplicates of what the contract now owns.
0.4.0 is published on PyPI and adds `continuo_validation_contract.types`
(the SQL type grammar) and `continuo_validation_contract.sql.ensure_single_read`
(the single-read gate). It depends on `sqlglot==30.15.0`, which therefore becomes
available to this repo.

### Pin bumps — `0.3.0` → `0.4.0`

- `pyproject.toml`
- `python-runtime-postgres/pyproject.toml`
- `python-runtime-trino/pyproject.toml`
- `Dockerfile.postgres`
- `Dockerfile.trino`
- `.github/workflows/ci.yml` — the comment reading
  `# continuo-validation-contract resolves from PyPI (==0.3.0).`

Run `uv sync --all-packages --all-groups` and commit the resulting `uv.lock`.

### Dedupe 1 — the SQL type grammar

Both adapters carry a byte-identical `_TYPE_RE` + `_validate_column_type`, and
the contract's `types.py` docstring says it is kept byte-identical to them.
Delete both local copies and import the contract's:

```python
from continuo_validation_contract.types import validate_column_type  # type: ignore[import-untyped]
```

Keep trino's `_TRINO_TYPE_ALIASES` / `_trino_type` mapping — that is
engine-specific and not the contract's business. Keep each call site's ordering
(validate before interpolating).

`continuo_python_runtime/types.py::parse_sql_type` stays (it also maps to Arrow,
which the contract does not do), but make the contract the single **acceptance**
authority: call `validate_column_type(raw)` first and convert its `ValueError`
into `ContractError` preserving the message, then parse precision/scale/length.
Add a test asserting the two agree on a vector list covering every grammar
member and several rejects (`FLOAT`, `VARCHAR`, `NUMERIC(10)`, `INT4`, `""`).

### Dedupe 2 — the single-read gate

`continuo_python_runtime/contract/loader.py` hand-rolls the single-statement
check in `_validate_read_shape`, `_mask_string_literals`, and
`_strip_leading_sql_comments` (about 90 lines of literal-aware scanning).
Replace all three with the contract's parser-based gate:

```python
from continuo_validation_contract.sql import ensure_single_read  # type: ignore[import-untyped]
```

- `load_contract_dir` gains a keyword-only `dialect: str | None = None`, passed
  down to `parse_node`, which calls `ensure_single_read(sql, dialect)` per read.
- Convert its `ValueError` into
  `ContractError(f"{label}: 'reads.{name}' must be a single read query ({exc})")`
  — the contract's own message mentions `check_binds`, which is the wrong
  context here, so it is wrapped rather than surfaced bare.
- Delete `_mask_string_literals` and `_strip_leading_sql_comments` and their
  tests; `ensure_single_read` covers the same ground by parsing.

### CLI — `--dialect`

Add an optional `--dialect` argument (default `None`) to the `validate`, `merge`,
and `hash` subcommands in `continuo_python_runtime/cli.py`, threaded into
`load_contract_dir` / `build_wire_contract`. Help text: *"sqlglot dialect the
reads are authored in (e.g. postgres, trino); defaults to sqlglot's dialect-neutral
parser."* This is what lets a domain repo check its reads against the dialect
Continuo will actually use (§2c). `build_wire_contract` gains the same
keyword-only parameter and forwards it.

`template/.github/workflows/release.yml`: leave the existing `validate` and
`merge` steps working without `--dialect`, but add a comment above them showing
how to set it, e.g. `# continuo-runtime validate contracts/ --dialect postgres`.

### Tests

- existing `tests/contract/test_loader.py` read-shape cases keep passing (a
  multi-statement read, a non-SELECT read, and a `;`-inside-a-string-literal
  read are all still correctly classified) — adjust expected message substrings,
  not expected outcomes
- a read that fails to parse under an explicit `--dialect` but passes under the
  neutral default proves the flag threads through (pick any dialect-specific
  syntax that demonstrates this; if none is stable across sqlglot versions,
  assert instead that the dialect argument reaches `ensure_single_read` with a
  monkeypatched spy)
- `tests/test_cli.py`: `--dialect` is accepted by all three subcommands and
  defaults to `None`
- `tests/test_types.py`: the grammar-agreement vector test above
- adapter suites still pass with the shared `validate_column_type`

Commit message: `refactor: adopt validation-contract 0.4.0 type grammar and read gate`

---

## Task 9: Version bumps and release prep

Prepare the release; **do not tag and do not publish**. The human cuts the
release after review.

- `pyproject.toml`: `version = "0.1.0"` → `"0.2.0"`
- `python-runtime-postgres/pyproject.toml`: `0.1.0` → `0.2.0`
- `python-runtime-trino/pyproject.toml`: `0.1.0` → `0.2.0`
- `template/Dockerfile`: base image tag
  `ghcr.io/carolsimone/continuo-python-runtime:v0.1.0-postgres` →
  `:v0.2.0-postgres`, and update the comment's `v0.1.0-trino` example to
  `v0.2.0-trino`
- Grep the repo for any other `0.1.0` / `v0.1.0` reference that names one of
  these three distributions or the base images (`README.md`,
  `template/README.md`, `docs/*`, `Dockerfile.postgres`, `Dockerfile.trino`) and
  update each. Leave unrelated pinned versions (`pyarrow==25.0.0`,
  `PyYAML==6.0.3`, tool pins) alone.
- Re-run `uv sync --all-packages --all-groups` and commit `uv.lock` if it moved.

Sanity check to run and paste into the report:

```bash
uv run continuo-runtime merge template/contracts --service demo --repo-root template --out /tmp/contract.yaml
```

(from the repo root; `template/scripts/example.py` is the script it hashes).
Confirm the output document has `contract_version: 1`, a `service`, and one node
entry carrying all four hash fields plus `config`, and that recomputing
`"sha256:" + sha256(f"{source_hash}|{shared_code_hash}|{config_hash}")` matches
the emitted `content_hash`.

Commit message: `chore: bump runtime and adapter packages to 0.2.0`
