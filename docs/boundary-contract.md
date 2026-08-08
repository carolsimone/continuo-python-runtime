# The external python-runtime repository — boundary contract

This is §13 of the parent design (`continuo-python-node-unified-design.md`),
reproduced here unchanged. It previously lived at the top of this repo's
`README.md`; it now lives here, and the README points at it instead of
repeating it.

The runtime lives in its **own repository** (`continuo-python-runtime`),
mirroring the continuo-validation-runners split. Domain teams (marketing,
finance, …) template from it: they add their scripts + contract yaml files
and inherit a CI/CD that produces everything Continuo needs. Continuo's side
of the boundary is exactly five surfaces — nothing else crosses it.

## 13.1 Surface 1 — the S3 artifact (CI → S3, BEFORE the POST)

One merged object per service per release, at the canonical key:

```
s3://<bucket>/<service>/<release_id>/contract.yaml
```

- Schema per §3: `contract_version: 1`, `service`, `nodes: [...]` — every
  node's wire entry carries `schema`, `table`, `owner`, `schedule`,
  `criticality`, `script`, `reads`, `output_columns`, `description`,
  `extra_columns`, the four hash fields (`source_hash`, `shared_code_hash`,
  `config_hash`, `content_hash`), and `config` (physical layout — see
  below), which is present on every wire entry, defaulting to `{}` when
  the author's contract file didn't declare one. "Optional" describes the
  contract *file* an author writes, not the merged wire artifact, where
  every field above is always present.
- **Reads must be single-statement SELECTs with every table reference
  schema-qualified** (`analytics.table_a`, never `table_a`) — the resolver
  raises `UnqualifiedTableReference` and rejects the whole release otherwise
  (CTE names are exempt). References to tables outside Continuo's registry
  are allowed (external sources) and simply produce no edge.
- **Reads are dialect-bound, not dialect-neutral** (continuo PR #400):
  Continuo parses, rewrites, and bind-checks every read in the **install's
  warehouse SQL dialect**, so authors write each read in their own engine's
  own dialect — a read that's valid against postgres can fail
  `InvalidCompiledSql` on an install whose warehouse is Trino, and vice
  versa. `continuo-runtime validate|merge|hash` accept an optional
  `--dialect <name>` flag (e.g. `postgres`, `trino`) so a domain repo can
  check its reads against that dialect locally, catching the failure in its
  own CI instead of at Continuo's parser. Reads are always parsed (via
  `continuo_validation_contract.sql.ensure_single_read`, sqlglot-backed) even
  without `--dialect` — the flag only chooses which dialect's grammar that
  parse is checked against, defaulting to sqlglot's dialect-neutral parser
  when omitted.
- **What changed for contracts written before this** (existing repos, on
  their next release): `validate`/`merge`/`hash` used to accept a read after
  a hand-rolled scan for a leading `SELECT`/`WITH`; they now hand every read
  to a real parser (`ensure_single_read`). SQL a driver would have accepted
  but a parser will not — most commonly a driver-specific bind placeholder
  such as psycopg2's `%(name)s`, since `ctx.read(name)` never takes params —
  now fails `validate` where it previously passed. Such a read was already
  broken at run time; the failure only moved earlier. In the other
  direction, engine-specific syntax the *dialect-neutral* parser rejects
  (postgres `~`, `@>`, …) needs `--dialect <engine>`, which is what a repo
  should be passing anyway, since Continuo parses and bind-checks every read
  in the install's own warehouse dialect. Run `continuo-runtime validate
  contracts/ --dialect <engine>` once against an existing repo before its
  next release: it is exactly the gate CI now applies, and it reports every
  affected read at once. The runtime image does **not** re-run this gate
  (see §13.4), so a read that passes here will not be re-judged under a
  different grammar in production.
- `output_columns` types come from the supported set: `BIGINT`,
  `INT`/`INTEGER`, `DOUBLE PRECISION`, `NUMERIC(p,s)`/`DECIMAL(p,s)`,
  `VARCHAR(n)`/`CHAR(n)`/`TEXT`, `TIMESTAMP`, `DATE`, `BOOLEAN`.
- Ordering is a hard rule: **upload completes before `POST /releases`** —
  Continuo does no existence check (D3); a POST racing its own upload
  fails at the parsing stage.

### Physical-layout `config`

Every node may carry an optional `config` mapping alongside its typed
`output_columns`: the node's physical layout, written directly in the
**active engine's own vocabulary**. It is deliberately *not*
engine-namespaced (no `postgres: {...}` / `trino: {...}` wrapper) — the
contract is already engine-bound, since reads are authored in that same
engine's SQL dialect (see above), so there is no ambiguity to resolve at
parse time.

- The active engine's `RuntimeAdapter` **fails closed on any key it does
  not recognize**, at any nesting level — there is no other namespace to
  excuse an unknown key into.
- Recognized keys as shipped:
  - **postgres** → `indexes`: a list of `{columns: [...], unique: bool,
    name: str}` entries. Every `columns` entry must name a column already
    declared in `output_columns`; an index on an undeclared column raises
    before any DDL runs. `unique` defaults to `false`; `name` defaults to
    `ix_<table>_<col1>_<col2>...` (truncated to 63 bytes if longer).
  - **trino (Iceberg)** → `partitioning` and `sorted_by` (each a
    non-empty list of non-empty strings — column names or Iceberg
    partition transforms like `day(event_ts)`), and `format` (one of
    `PARQUET`/`ORC`/`AVRO`, case-insensitive).
- Worked example (from `template/contracts/example.yml`):
  ```yaml
  config:
    indexes:
      - columns: [order_id]
        unique: true
  ```
- Applied at runtime by `RuntimeAdapter.ensure_table`, per SQL object, on
  **create-if-absent** — this is not gated on whether the table itself was
  newly created. Postgres evaluates each `indexes` entry's `CREATE INDEX
  IF NOT EXISTS` independently, on every `ensure_table` call, so adding a
  new index entry to a node whose table already exists **does** create
  that index — as does changing `columns` on an entry with no explicit
  `name`, since that changes the derived default name. Iceberg's
  properties instead all ride on a single `WITH (...)` clause attached to
  the `CREATE TABLE`'s own `IF NOT EXISTS`, so once the table exists
  nothing in `config` is applied at all. Neither engine is a migration
  mechanism for an index/property that already exists under the same
  name: flipping `unique: false → true` under a fixed index `name` is a
  **silent no-op** on postgres (the name already resolves, so `IF NOT
  EXISTS` skips it), and changing `partitioning` on a trino table that
  already exists is a silent no-op for the reason above — not an applied
  change and not an error in either case. Once continuo-validation step 3a
  lands, the same vocabulary is checked ahead of runtime by
  `build_empty_from_columns`, so a malformed config fails the release gate
  rather than surfacing in production.
- Trino's `format` is case-normalized to uppercase in the emitted DDL, so
  `format: parquet` and `format: PARQUET` produce identical `WITH (...)`
  text — but they are different bytes in the contract entry, so they
  produce different `config_hash` (and therefore `content_hash`) values,
  triggering a revalidation that — per the point above — re-applies
  nothing.
- `config` lives in the contract entry like any other field, so it
  participates in `config_hash` with no special-casing in the hash
  formula: adopting `config` on a previously bare node, or editing an
  existing one, changes `content_hash` exactly as editing `reads` or
  `output_columns` would.

## 13.2 Surface 2 — the hash fields (computed by CI, byte-exact algorithms)

Each node entry in `contract.yaml` carries four hash fields — three
independently-computed parts plus `content_hash`, their fold — all
computed by this repo's `continuo-runtime merge`
(`continuo_python_runtime.hashing`) and shipped on the wire entry. Continuo
never recomputes `source_hash` or `shared_code_hash` from raw bytes over
this surface (it doesn't receive the script or closure here); it
recomputes only the fold (see below).

```
source_hash      = sha256(script_bytes)                            # bare hex
shared_code_hash = ""  if the in-repo import closure is empty, else
                    sha256(concat(sorted(
                        sha256(member_bytes).hexdigest()
                        for member in closure)))                    # bare hex
config_hash      = sha256(canonical_json(entry))                    # bare hex
content_hash     = "sha256:" + sha256(source_hash + "|" + shared_code_hash
                                       + "|" + config_hash)
```

- `script_bytes` — the node's own script file, byte-for-byte.
- `member_bytes` — the byte content of every file in the node's in-repo
  import closure (defined below); the script itself is never a closure
  member, so `source_hash` and `shared_code_hash` never double-count it.
  `shared_code_hash`'s `sorted(...)` sorts the per-member **hex digest
  strings**, not the raw bytes, and `concat` joins those sorted hex
  strings before the outer `sha256`.
- `canonical_json(entry)` — the node's wire entry, yaml-parsed then
  re-serialized to JSON with sorted keys and no whitespace
  (`json.dumps(..., sort_keys=True, separators=(",", ":"))`); every
  `reads` value is first whitespace-normalized (runs of whitespace
  collapse to a single space, then stripped); `content_hash` **and all
  three part fields** (`source_hash`, `shared_code_hash`, `config_hash`)
  are excluded from the basis — `config_hash` cannot include itself, and
  none of the four hash fields participates in another's basis. Every
  other field, including `config`, participates verbatim.

Invariant: formatting-only edits (spaces in SQL, yaml indentation/key
order) leave all four hashes unchanged; any semantic edit to reads,
columns, metadata, `config`, or the script changes at least one part and
therefore `content_hash`.

The fold (`content_hash = "sha256:" + sha256(source|shared|config)`) is
byte-identical to manifest-controller's dbt-side fold — one formula spans
both runtimes. Continuo's side **recomputes the fold from the three part
fields already on the wire entry and rejects the release if it doesn't
match the submitted `content_hash`**.

### Closure definition and its documented limits

`shared_code_hash` covers the node's **in-repo import closure**: the
transitive set of repo-internal `.py` files the script reaches through its
own `import` statements, resolved by static AST analysis
(`continuo_python_runtime.closure.resolve_closure`).

- The script itself is excluded (it is `source_hash`); stdlib and
  installed packages are excluded — external dependencies are the image's
  concern, pinned by `image_tag`, not the hash's.
- Resolution tries two search roots per import — the repo root first, then
  the importing file's own directory — with PEP-328 relative imports
  (`from . import x`, `from ..pkg import y`) resolved against the
  importing file's own package. Ancestor `__init__.py` files along a
  resolved dotted path are closure members too, because they execute on
  import.
- **Limit — over-inclusion by design.** `from pkg import name` cannot be
  statically distinguished from a submodule import, so a same-named
  in-repo file (`pkg/name.py`) is pulled into the closure even when the
  real import was an attribute of `pkg`. The cost is a spurious
  revalidation, never a missed one: under-inclusion would be a
  correctness bug (a stale node silently running in production), while
  over-inclusion only costs one extra revalidation — the tradeoff
  `closure.py`'s own module docstring documents as deliberate.
- **Limit — dynamic imports are rejected, not approximated.**
  `importlib` (in any form), `builtins` (the module, in any form —
  `import builtins`, `from builtins import ...`), `__builtins__` (the name
  every module's globals are injected with at import time — see §13.4's
  loader note; `__builtins__['exec'](...)` needs no preceding `import
  builtins` at all), `__import__`, `exec`, `eval`, and any
  `.import_module(...)` attribute access (on any receiver — the predicate
  doesn't care what object it's called on) are refused wherever
  `continuo-runtime lint` is pointed (typically `scripts/`), and —
  regardless of what lint was pointed at — unconditionally in the script
  **and every closure member** by the merger itself (`resolve_closure`
  raises `ContractError` on the first one found), so even a shared helper
  module outside the linted path cannot smuggle one in. What static
  analysis cannot see must not exist. The one plausible legitimate casualty
  is `from builtins import str` (the `python-future` compatibility shim):
  it is rejected exactly like any other `ImportFrom` whose root module is
  `builtins`, with no exemption.
- **Every closure member is held to the full lint rule set, not only
  dynamic-import rejection.** `continuo-runtime lint` (typically pointed at
  `scripts/` in CI, for fast feedback) checks only the files given to it —
  a helper imported from outside that path, e.g. `lib/shared.py` via `from
  lib.shared import ...`, is invisible to that step. `continuo-runtime
  merge` closes that gap independently: before computing a node's hash
  parts, it runs the same rules CI's lint applies — L1 no warehouse driver
  imports, L2 no SQL string literals, L3 no forbidden data-access calls,
  L4 no private-attribute access, L5 no dynamic-import constructs — over
  the node's script **and every resolved closure member**, and raises
  `ContractError` naming every offending file if any produces a violation.
  Because the merger is the thing that produces the release artifact, this
  cannot be bypassed by a stale or hand-edited CI workflow file: a helper
  outside `scripts/` can never become a side channel around the harness's
  sole write sink.
- **Limit — data files are not members.** A script reading a non-`.py`
  file from the repo (a CSV, a JSON fixture) does not fingerprint that
  file — such inputs belong in the warehouse or in a declared `reads`
  entry, not on disk next to the script.

## 13.3 Surface 3 — the release call

```
POST /releases
{
  "service":    "marketing-py",          # one service name per domain repo
  "release_id": "<unique, matches the S3 key path>",
  "image_tag":  "<registry>/<image>:<tag>",   # the image the executor will run
  "repo":       "owner/name",            # where the source lives (remediation)
  "commit_sha": "<full sha>",            # must contain scripts + contracts
  "kind":       "python"
}
```

202 Accepted `{"release_id": …, "status": "received"}`; 400 on any missing
field. Idempotent on `release_id` — safe to retry. `repo` + `commit_sha`
must point at the actual source of the scripts and contract files, because
the remediation agent fetches them from GitHub to propose fix PRs.

## 13.4 Surface 4 — the runtime image

`image_tag` must be pullable by the cluster and behave as follows when the
executor runs it as a Kubernetes Job:

- **Selection env**: the executor sets `NODE_ID`, `TABLE_NAME`, and the
  target schema env; the harness looks the node up in the contract files
  **shipped inside the image** and dispatches its `script`. One image serves
  all of the service's nodes.
- **Script import path**: before executing the script the harness prepends
  `APP_ROOT` and the script's own directory to `sys.path`, in that order —
  the same two search roots the closure resolver folds into
  `shared_code_hash` (§13.2) — and leaves them there for the process
  lifetime, so a deferred `import` inside `run()` resolves too. Both forms a
  script can use work: sibling (`import helpers`) and package-qualified
  (`import scripts.helpers`, `from lib.shared import ...`). The image must
  still `COPY` every directory the scripts import from: a helper is folded
  into the hash whether or not it is in the image, so one left out yields a
  valid release artifact and a node that dies with `ModuleNotFoundError`.
- **Warehouse env**: engine-native connection vars (`POSTGRES_HOST`,
  `POSTGRES_DB`, `POSTGRES_USER`, optional `POSTGRES_PORT`/`_PASSWORD`; ditto
  per engine), injected via the same Secret mechanism dbt Jobs use.
- **Sole write sink**: user scripts return a dataframe; the harness runs
  `conform()` (strict Arrow cast per §7.1), then calls
  `RuntimeAdapter.ensure_table` — applying the node's `config` physical
  layout on create, see §13.1 — and performs the only INSERT. Scripts get
  a read-only connection surface. The harness calls `ensure_table(...,
  config=...)` as a keyword unconditionally, so every `RuntimeAdapter`
  implementation must accept it today even though no released
  `continuo-validation-contract` version's abstract `ensure_table`
  declares the parameter (the `0.4.0` port pinned by this repo still
  reads `ensure_table(self, schema, table, columns)`); a future contract
  release is expected to add `config` to the port itself.
- **Early `config` tripwire**: `ensure_table` is called only *after* the
  script has run and its result has been conformed, so a bad `config` would
  otherwise burn the entire node run before failing. The harness therefore
  also calls `RuntimeAdapter.validate_config(config, column_names)` — a
  classmethod, no connection needed — immediately after selecting the node,
  and surfaces a rejection as `LoadError`. Both adapters shipped here
  implement it by reusing the exact logic `ensure_table` validates with, so
  the two cannot disagree. Like `config` on `ensure_table`, the abstract
  port in the pinned `continuo-validation-contract` does not declare it;
  unlike `config`, the harness *skips* the call for an adapter that does not
  provide it, because `ensure_table` remains the enforcement point and such
  an adapter loses only earliness, never fail-closed behavior. Third-party
  adapters should implement it.
- **The read-shape gate is not re-run at run time.** `continuo-runtime run`
  loads and validates the baked-in contract files with `check_reads=False`:
  every other rule (required fields, criticality, the `reads` map's shape,
  output-column types and uniqueness, `config`, duplicate relations) still
  runs, but the per-read `ensure_single_read` parse does not. The harness has
  no `--dialect` of its own, so re-parsing would judge the reads against the
  dialect-neutral grammar — a different, in places stricter grammar than the
  one CI used (§13.1) — and could only disagree with the gates already
  passed (CI's `validate`, then Continuo's own parser and bind-check), never
  catch a new problem: `ctx.read` resolves declared reads by name and never
  parses them.
- **Result envelope**: stdout is reserved for exactly one sentinel-framed
  result block as the last line — reuse `continuo_validation_contract.result`
  (already on PyPI) rather than reimplementing the markers. All diagnostics
  go to stderr via stdlib `logging`. Non-zero exit on failure.
- The image embeds the same contract files CI merged — `content_hash` is
  what ties the promoted topology to the image actually running.

### Python-node env contract (normative)

§13.4's "the executor sets `NODE_ID`, `TABLE_NAME`, and the target schema
env" is deliberately spelled out loosely above; this subsection pins the
exact environment variable names `continuo-python-runtime`'s harness
(`continuo_python_runtime.harness.run_node`) requires. These names are
**not** the same as the dbt Job env — continuo's existing
executor-controller injects `SCHEMA`/`DBT_TARGET_SCHEMA` for dbt Jobs and
passes a dbt-style `UniqueID` verbatim; the python-kind dispatch path must
inject the names below instead:

| Variable | Required? | Meaning |
| --- | --- | --- |
| `NODE_ID` | **required** | Any identifier whose trailing two dot-separated segments MUST be `<schema>.<table>` of the declared node (e.g. `python-model.<service>.<schema>.<table>`); the harness splits on `.` and matches the last two segments against the merged contract's nodes. |
| `TABLE_NAME` | **required** | The target table name; must match the `table` of the node selected via `NODE_ID`. |
| `TARGET_SCHEMA` | **required** | The target schema name; must match the `schema` of the node selected via `NODE_ID`. There is no fallback — `SCHEMA` and `DBT_TARGET_SCHEMA` are **not** recognized. |
| `CONTRACT_DIR` | optional | Path to the directory of merged contract YAML files baked into the image. Defaults to `/app/contracts`. |
| `APP_ROOT` | optional | Repository root the node's `script:` path is resolved against. Defaults to `CONTRACT_DIR`'s parent directory. |
| engine-native vars | **required**, per adapter | Whatever the installed `RuntimeAdapter.required_env()` declares (e.g. `POSTGRES_HOST`/`POSTGRES_DB`/`POSTGRES_USER`, optionally `POSTGRES_PORT`/`POSTGRES_PASSWORD`) — missing any of these is a `LoadError`. |

The executor's python-kind dispatch (continuo PR 8) **must** inject
`NODE_ID`/`TABLE_NAME`/`TARGET_SCHEMA` under exactly these names; they
intentionally differ from the dbt job env (`SCHEMA`/`DBT_TARGET_SCHEMA`/dbt
`UniqueID`) because the python-node harness has a different selection model
(a merged contract keyed by `schema.table`, not a dbt manifest node).

## 13.5 Surface 5 — what the domain repo's CI/CD does per merge

```
1. lint/test the scripts; validate contract files against the v1 schema
2. merge contract files → contract.yaml; compute the per-node hash fields (§13.2)
3. build + push the image (scripts + contracts + harness baked in)
4. upload contract.yaml → s3://<bucket>/<service>/<release_id>/contract.yaml
5. POST /releases {…, kind: "python"}          # only after 3 and 4 succeed
```

Provisioning Continuo hands each domain repo, once: S3 write credentials
scoped to `s3://<bucket>/<service>/*`, the release endpoint URL, registry
push credentials, and (per §7.2) a warehouse role limited to `SELECT` on
declared upstreams + the harness's write path.

---

## Bottom line

The contract split — **declared reads for the graph + bind-checked inputs,
declared columns for the shape** — keeps validation image-free and maximally
portable (bare typed `CREATE TABLE`), threads python nodes through the
*existing* release/promotion machinery with exactly one new wire field
(`kind`) and one new validation op (`build_from_columns` + `check_binds`),
and pushes exactly two obligations to runtime: **output conformance** (strict
Arrow cast in a sole-sink harness, backstopped by the typed table) and
**read honesty** (grant-scoped, fail-closed). dbt and python nodes are peers
in one DAG; dbt-core remains an ingest dialect, not a dependency.
