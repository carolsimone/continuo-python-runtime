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
  node carries `schema`, `table`, `owner`, `schedule`, `criticality`,
  `script`, `reads`, `output_columns`, `content_hash`.
- **Reads must be single-statement SELECTs with every table reference
  schema-qualified** (`analytics.table_a`, never `table_a`) — the resolver
  raises `UnqualifiedTableReference` and rejects the whole release otherwise
  (CTE names are exempt). References to tables outside Continuo's registry
  are allowed (external sources) and simply produce no edge.
- `output_columns` types come from the supported set: `BIGINT`,
  `INT`/`INTEGER`, `DOUBLE PRECISION`, `NUMERIC(p,s)`/`DECIMAL(p,s)`,
  `VARCHAR(n)`/`CHAR(n)`/`TEXT`, `TIMESTAMP`, `DATE`, `BOOLEAN`.
- Ordering is a hard rule: **upload completes before `POST /releases`** —
  Continuo does no existence check (D3); a POST racing its own upload
  fails at the parsing stage.

## 13.2 Surface 2 — `content_hash` (computed by CI, byte-exact algorithm)

```
content_hash = "sha256:" + sha256(
    canonical_json(contract_entry)   # yaml parsed → JSON, sorted keys, no
                                     #   whitespace; each `reads` entry first
                                     #   whitespace-normalized (runs of
                                     #   whitespace → single space, stripped);
                                     #   the content_hash field itself excluded
  + "\x00"
  + script_bytes                     # the node's script file, byte-for-byte
)
```

Invariant: formatting-only edits (spaces in SQL, yaml indentation/key order)
produce an identical hash; any semantic edit to reads, columns, metadata, or
the script changes it. Continuo trusts this value verbatim — it is the sole
change detector driving revalidation and `Changed` tagging, so the runtime
repo's CI owns getting it right (and ships the reference implementation +
tests).

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
- **Warehouse env**: engine-native connection vars (`POSTGRES_HOST`,
  `POSTGRES_DB`, `POSTGRES_USER`, optional `POSTGRES_PORT`/`_PASSWORD`; ditto
  per engine), injected via the same Secret mechanism dbt Jobs use.
- **Sole write sink**: user scripts return a dataframe; the harness runs
  `conform()` (strict Arrow cast per §7.1) and performs the only INSERT.
  Scripts get a read-only connection surface.
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
2. merge contract files → contract.yaml; compute per-node content_hash (§13.2)
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
