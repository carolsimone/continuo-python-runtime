# continuo-python-runtime — Design

Date: 2026-07-31
Status: Approved design for this repository (PR-9 of the unified plan).
Parent: `continuo-python-node-unified-design.md` (§13 is this repo's boundary
contract). Where this document amends the parent, the amendment is called out
explicitly; everything else inherits.

## 1. Purpose

This repository produces everything a domain team needs to ship python nodes
into Continuo's DAG, and everything the executor needs to run them:

1. **A PyPI package** `continuo-python-runtime` — the harness (sole write
   sink), contract loader/validator/merger, `content_hash` reference
   implementation, `conform()`, and the CI lint.
2. **One base Docker image per engine** — `Dockerfile.postgres` and
   `Dockerfile.trino`, each installing the package plus exactly one runtime
   adapter package.
3. **A template** (`template/`) — a minimal domain repo (contracts, scripts,
   Dockerfile, GitHub Actions workflow) teams copy once. Harness fixes reach
   teams by base-image bump, never by template re-copy.

## 2. Decisions of record (this repo)

| # | Question | Decision |
|---|---|---|
| R1 | How do scripts get input data? | Scripts drive: `ctx.read(name)` resolves a **declared** read from the contract and executes it through the runtime adapter. No API accepts raw SQL text. |
| R2 | Read honesty | Enforced at the API (declared reads only) **and** by CI lint (no SQL string literals, no direct driver imports in scripts). Warehouse grant scoping (§7.2 of the parent) becomes defense-in-depth. |
| R3 | Dataframe library | Not governed. Arrow is the neutral currency: `ctx.read()` returns `pyarrow.Table`; `run()` may return anything Arrow-convertible (pandas, polars, pyarrow). Base image ships only pyarrow; domain images add their own libraries. |
| R4 | Runtime adapter port location | `RuntimeAdapter` ABC lives in `continuo/validation-contract` (ships in the 0.3.0 bump alongside `build_empty_from_columns`/`check_binds`). Engine implementations live in the continuo-validation-runners repo under a new entry-point group `continuo_runtime.adapters`. All additive — no breaking changes to existing adapters. |
| R5 | Extra columns in `conform()` | Raise by default; per-node contract flag `extra_columns: raise \| warn` downgrades to drop-with-warning. |
| R6 | Repo shape | Approach A: library + base images + embedded template, out of this one repo (validation-runners precedent). |
| R7 | Engine sequencing | The port and both base-image Dockerfiles support Postgres and Trino from day one; the Postgres adapter implementation lands first (proves the port), Trino follows as its own PR in validation-runners. |
| R8 | Write semantics v1 | Full replace (dbt `table` materialization equivalent): `ensure_table` (typed DDL, create-if-absent) then atomic content replace. Incremental is out of scope and layers on later. |
| A1 | **Amendment to parent §3.1**: `reads` is a **named map**, not a list | Scripts reference reads by name (`ctx.read("joined")`). Wire-visible: manifest-controller iterates the map's *values*; read names participate in `content_hash` (canonical JSON sorts keys). Touches PR-5/PR-6 in the main repo. |

## 3. Repository layout

```
continuo-python-runtime/
├── pyproject.toml                      # continuo-python-runtime (PyPI)
├── continuo_python_runtime/
│   ├── contract/                       # schema v1 model, loader, validator, merger
│   ├── hashing.py                      # content_hash reference impl (parent §13.2)
│   ├── conform.py                      # strict Arrow cast + VARCHAR check + extra-column policy
│   ├── context.py                      # RunContext: ctx.read(name) -> pyarrow.Table
│   ├── harness.py                      # container entrypoint (run one node end-to-end)
│   ├── lint.py                         # no-handwritten-SQL check for CI
│   └── cli.py                          # continuo-runtime {run, merge, lint, hash, validate}
├── tests/
├── Dockerfile.postgres                 # base image: package + continuo-runtime-postgres
├── Dockerfile.trino                    # base image: package + continuo-runtime-trino
└── template/
    ├── contracts/example.yml
    ├── scripts/example.py
    ├── Dockerfile                      # FROM a base image; adds scripts+contracts+libs
    └── .github/workflows/release.yml   # the parent-§13.5 pipeline
```

Conventions match continuo-validation-runners: hatchling, Python ≥3.14, uv,
ruff, mypy, pytest. The pre-design scaffold (`contract/`, `code/`,
`adapter/`, `hashing/`, `contract-loader/` directories) is superseded and
removed; its contract files used the old `py-runner` shape and are rewritten
into `template/` in the v1 schema.

## 4. The runtime adapter port

Defined in `continuo_validation_contract` (0.3.0), implemented per engine in
continuo-validation-runners, discovered via entry points — exactly one
adapter per image, same failure modes as the validation side's
`discover_adapter()`.

```python
class RuntimeAdapter(ABC):
    @classmethod
    def required_env(cls) -> list[str]: ...
    @classmethod
    def from_env(cls) -> "RuntimeAdapter": ...
    def fetch(self, sql: str) -> pyarrow.Table: ...          # execute one declared read
    def ensure_table(self, schema: str, table: str,
                     columns: list[Column]) -> None: ...     # CREATE TABLE IF NOT EXISTS, typed DDL
    def load(self, schema: str, table: str,
             data: pyarrow.Table) -> None: ...               # atomic full replace
    def close(self) -> None: ...
```

- The **harness is the only caller** of `fetch` and `load`. Scripts never see
  the adapter or a connection.
- Engine realities stay behind the port: Postgres `load` is
  `TRUNCATE + INSERT` in one transaction; Trino `load` is
  write-to-staging-table → swap (rename or `CREATE OR REPLACE` where the
  connector supports it). `fetch` converts each engine's native results to
  `pyarrow.Table`.
- Engine selection is image composition, never runtime config: a domain repo
  is Postgres- or Trino-backed purely by which base image its Dockerfile
  builds `FROM`. The executor injects engine-native env (`POSTGRES_*` /
  `TRINO_*`) via the same Secret mechanism dbt Jobs use; each adapter
  declares its own `required_env()`.

## 5. Contract schema v1 (authoring)

```yaml
# contracts/table_test.yml — one or many nodes per file
nodes:
  - schema: analytics
    table: table_test
    description: "test data"
    owner: marketing
    schedule: daily
    criticality: SECONDARY          # REGULATORY | CORE | SECONDARY
    script: scripts/table_test.py
    extra_columns: raise            # optional; raise (default) | warn
    reads:                          # named map (amendment A1)
      joined: |
        select a, b from analytics.table_a
        left join analytics.table_b on table_a.a = table_b.a
      ids: select id from analytics.table_a
    output_columns:
      - {name: id,    type: INTEGER, nullable: false}
      - {name: count, type: INTEGER}
      - {name: date,  type: DATE}
```

Rules (inherited from parent §13.1 unless noted):

- Reads are single-statement SELECTs, every table reference schema-qualified;
  CTE names exempt; references outside Continuo's registry allowed (no edge).
- `output_columns` types from the supported set: `BIGINT`, `INT`/`INTEGER`,
  `DOUBLE PRECISION`, `NUMERIC(p,s)`/`DECIMAL(p,s)`,
  `VARCHAR(n)`/`CHAR(n)`/`TEXT`, `TIMESTAMP`, `DATE`, `BOOLEAN`.
- Validator strictness: missing owner/schedule/columns/script rejected;
  unknown types rejected; duplicate `(schema, table)` across a service's
  files rejected; duplicate read names within a node rejected;
  `contract_version` gated.
- Wire artifact per parent §13.1: CI merges all files into one
  `contract.yaml` (`contract_version: 1`, `service`, `nodes: [...]` with
  embedded per-node `content_hash`), uploaded to
  `s3://<bucket>/<service>/<release_id>/contract.yaml` **before**
  `POST /releases`.

## 6. Script API

```python
def run(ctx) -> "anything Arrow-convertible":
    joined = ctx.read("joined")     # pyarrow.Table
    ...
    return result                   # pa.Table | polars | pandas | Arrow C-stream
```

- `ctx.read(name)`: resolves the declared SQL, executes via `adapter.fetch`,
  memoizes per name within the run. Unknown name → `ReadError` without
  touching the adapter.
- Return value normalized through pyarrow (`__arrow_c_stream__` protocol or
  pandas conversion) before `conform()`.

## 7. Harness execution flow

`continuo-runtime run` (image entrypoint) executes exactly one node per Job:

```
1. Read env      NODE_ID, TABLE_NAME, TARGET_SCHEMA (executor-injected)
2. Load contract from CONTRACT_DIR (default /app/contracts) → validate v1
                 → locate node by declared (schema, table): the contract has
                 no node ids; NODE_ID's trailing <schema>.<table> segments
                 (unique_id convention, aligned with executor PR-8) select
                 the node; NODE_ID itself is echoed in the sentinel block
3. Discover      the single installed RuntimeAdapter → from_env()
                 (fail fast on missing required_env)
4. Import        the node's script → resolve run()
5. Execute       run(ctx)
6. Conform       normalize via Arrow → structural check (exactly the declared
                 columns, declared order; extra-column policy R5)
                 → cast(arrow_schema, safe=True), strict, never permissive
                 → VARCHAR(n) length check
7. Write         adapter.ensure_table(TARGET_SCHEMA, TABLE_NAME, columns)
                 adapter.load(TARGET_SCHEMA, TABLE_NAME, conformed)
8. Report        one sentinel result block on stdout
                 (continuo_validation_contract.result, RunStatus vocabulary)
                 as the last line; exit 0/1
```

- `TARGET_SCHEMA` wins over the declared schema for the write; read SQL runs
  as-is against prod names (rewriting is a validation-stage concern).
- stdout discipline: harness/adapters log to stderr via stdlib `logging`;
  stdout carries only the sentinel block. Script stdout is redirected to
  stderr during `run()` so a stray `print()` cannot corrupt the envelope.

## 8. Error taxonomy

Every failure exits non-zero with a sentinel block whose error class is
deterministic (remediation prompts key off it):

| Class | Raised when | Typical fix target |
|---|---|---|
| `ContractError` | contract missing/invalid/node not found/script missing | contract yaml |
| `ReadError` | unknown read name, or `fetch()` fails at the warehouse (incl. grant denial = undeclared-read attempt) | contract or upstream |
| `ScriptError` | `run()` raises, wrong signature, non-Arrow-convertible return | script |
| `ConformError` | structural mismatch, strict-cast failure, VARCHAR overflow | script or `output_columns` |
| `LoadError` | DDL/INSERT failure at the warehouse | ops/adapter |

Each class carries structured detail (node id, offending column(s)/read
name, engine message) in the sentinel payload.

## 9. CI/CD

**Template workflow** (`template/.github/workflows/release.yml`, on push to
main — implements parent §13.5):

```
1. lint        continuo-runtime lint scripts/        # no SQL literals, no driver imports
               continuo-runtime validate contracts/
2. test        the domain's own pytest suite
3. compile     continuo-runtime merge contracts/ --service $SERVICE --out contract.yaml
               # merges files, computes per-node content_hash (parent §13.2)
4. build+push  docker build (FROM base image) → registry
5. upload      contract.yaml → s3://$BUCKET/$SERVICE/$RELEASE_ID/contract.yaml
6. release     POST /releases {service, release_id, image_tag, repo,
               commit_sha, kind: "python"}   # strictly after 4 and 5
```

`RELEASE_ID` derived deterministically (`<run_number>-<short_sha>`) so S3
key, POST body, and image tag agree. Credentials arrive as repo secrets per
the parent-§13.5 provisioning handshake. POST is idempotent on `release_id`;
re-runs are safe.

**This repo's CI**: lint/type/test the package → build both base images →
smoke-test each (run the template's example node against a disposable engine
container; postgres now, trino when its adapter lands) → on tag, publish the
package to PyPI and the images to the registry.

## 10. Testing

- **Hashing invariants**: formatting-only edits (SQL whitespace runs, yaml
  indentation, key order) → identical `content_hash`; any semantic edit
  (read text, read *names*, columns, metadata) → changed; script edits
  byte-sensitive (whitespace-only script edits change the hash — Python
  indentation is semantic).
- **conform() matrix**: each lossy cast raises (`3.9→INT`, decimal overflow,
  unparseable string→date); `VARCHAR(n)` overflow raises; missing column
  raises; reordered columns handled; extra column raises by default /
  drops-with-warning under `extra_columns: warn`; nulls in
  `nullable: false` raise; `NUMERIC(p,s)` enforced by the cast.
- **Validator strictness**: per §5 rules, each rejection has a test.
- **Harness e2e with a fake in-memory `RuntimeAdapter`**: happy path emits
  exactly one sentinel `success` block; each §8 row produces its class
  deterministically; script `print()` cannot corrupt stdout; undeclared
  read name raises before touching the adapter.
- **Lint teeth**: bad scripts (embedded SELECT literal, `import psycopg2`,
  `pd.read_sql`) rejected; good scripts pass.
- **Template golden test**: the shipped `template/` example passes
  lint → validate → merge → hash locally as-is.
- Real-adapter integration tests live in continuo-validation-runners
  (docker-compose postgres/trino), matching its existing pattern.

## 11. Cross-repo work this design requires (not built here)

- `continuo/validation-contract` 0.3.0: add `RuntimeAdapter` ABC +
  `continuo_runtime.adapters` discovery (additive; lands with the already
  planned PR-1 bump).
- continuo-validation-runners: `continuo-runtime-postgres` package (first),
  `continuo-runtime-trino` (follow-up PR).
- continuo PR-5/PR-6: parser and validation-spec upload consume `reads` as a
  named map (amendment A1) — iterate values for edges/bind-checks; carry the
  map through canonical hashing.

## 12. Out of scope (deliberately)

- Incremental materialization (needs contract surface; layers on later).
- Warehouse query-audit defense-in-depth (parent open item).
- Snowflake/BigQuery/Redshift runtime adapters (port is engine-neutral;
  engines land in validation-runners when needed).
- Jinja/templating in contracts (would reopen parent D3).
