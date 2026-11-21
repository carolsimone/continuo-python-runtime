# The external python-runtime repository — boundary contract

The runtime lives in its **own repository** (`continuo-python-runtime`),
which also owns the engine contract, the engine adapters, and the validation
runner. Domain teams (marketing, finance, …) template from it: they add their
scripts and one ODCS contract per node, and inherit a CI/CD workflow that
produces everything Continuo needs. Continuo's side of the boundary is exactly
five surfaces — nothing else crosses it.

## 13.1 Surface 1 — the contract bundle (CI → S3, BEFORE the POST)

One bundle per service per release, at the canonical key:

```
s3://<bucket>/<service>/<release_id>/odcs-bundle.json
```

`continuo-runtime pack` writes it and a CI step uploads it. The upload
completes before `POST /api/v1/releases`: Continuo does no existence check, so
a POST that races its own upload fails at the parsing stage.

### The bundle

The bundle is UTF-8 JSON, written as
`json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False)` followed by a
newline:

```json
{
  "bundle_version": 1,
  "image_packages": {
    "continuo-python-runtime": "<version>",
    "pyarrow": "<version>"
  },
  "nodes": [
    {
      "path": "contracts/example.odcs.yaml",
      "shared_code_hash": "<bare hex, or empty>",
      "source_hash": "<bare hex>",
      "text": "apiVersion: v3.2.0\nkind: DataContract\nid: analytics.example\n..."
    }
  ],
  "producer": {
    "continuo_contracts": "<version>",
    "continuo_python_runtime": "<version>"
  },
  "service": "<service>"
}
```

- `bundle_version` is `1`.
- `service` is the service `pack` was given (`--service`).
- `producer` holds the versions of `continuo-python-runtime` and
  `continuo-contracts` that wrote the bundle.
- `image_packages` is the package list of the image the service runs: every
  distribution installed in it, as `{normalised name: version}`.
  `pack --image <ref>` reads it by running
  `docker run --rm --entrypoint continuo-runtime <ref> packages`;
  `pack --image-packages <file>` takes a file that holds that output. It is
  never the package list of the environment `pack` runs in. An image whose
  runtime has no `packages` command is refused: build it on runtime 0.13.0 or
  later.
- `nodes` has one entry per contract file, sorted by `path`:
  - `path` is the POSIX path of the file below the repository root
    (`--repo-root`);
  - `text` is the file's text exactly as written, line endings included;
  - `source_hash` and `shared_code_hash` are the two hash parts only CI can
    compute (§13.2).

Before it writes anything, `pack`:

1. loads the contracts folder exactly as `continuo-runtime validate` does
   (every rule in "The contract files" below), each read query parsed under
   `--dialect`;
2. checks every contract again against `--service`: a `service` property that
   names another service is refused;
3. refuses a `sitecustomize.py` or `usercustomize.py` at the repository root,
   which Python imports before the harness starts;
4. reads each node's script, in-repo import closure and assets, and refuses a
   script or an asset that is missing, is not a file or resolves outside the
   repository, and a contract file outside the repository;
5. lints each node's script and every member of its import closure (§13.2).

`pack` writes the bundle to a temporary file next to `--out` and renames it
over `--out` once it is complete. A refusal writes nothing.

### The contract files

- A contract file is `contracts/<table>.odcs.yaml`, or
  `contracts/<schema>.<table>.odcs.yaml` when two nodes of one service share a
  table name.
- Any other file in the folder whose name ends in `.yml` or `.yaml`, in any
  case (`legacy.YML`, `t.odcs.yml`), is refused, with a message that points
  here ("Converting a contract file" below). Files with any other extension
  are not read.
- Two files that declare the same `<schema>.<table>`, compared in lower case
  (`Py_Daily_Kpis` and `py_daily_kpis` are one table), are refused, and the
  message names both. A folder with no contract file is refused.

Each file holds one ODCS v3.2.0 data contract. The `continuo-contracts`
library (this runtime pins 0.4.1) reads it with its strict YAML reader (YAML
1.2 core scalars; no aliases, no `<<` keys, no duplicate keys, no keys that are
not strings, no tag beyond the core ones and one document per file), checks it
against the ODCS v3.2.0 JSON schema, then checks continuo's own rules. Every
issue it finds is reported, one line each, with the file and the place in it.

The template's script node:

```yaml
apiVersion: v3.2.0
kind: DataContract
id: analytics.example
name: example
version: "0"
description:
  purpose: Replace with your node
team:
  name: your-team
customProperties:
  - {vendor: continuo, property: kind, value: python-node}
  - {vendor: continuo, property: schedule, value: daily}
  - {vendor: continuo, property: criticality, value: SECONDARY}
  - {vendor: continuo, property: script, value: scripts/example.py}
  - vendor: continuo
    property: readQueries
    value:
      orders: select order_id, customer_id, amount from analytics.upstream_orders
      customers: select customer_id, name from analytics.upstream_customers
schema:
  - name: example
    physicalType: table
    properties:
      - {name: order_id, physicalType: INTEGER, required: true}
      - {name: customer_name, physicalType: "VARCHAR(64)"}
      - {name: amount, physicalType: "NUMERIC(10,2)", required: true}
```

The fields:

| Field | Rule |
| --- | --- |
| `apiVersion` | `v3.2.0`. |
| `kind` | `DataContract`. |
| `id` | `<schema>.<table>`, in lower case. |
| `name` | The table, or left out. |
| `version` | Required by ODCS, any string. continuo numbers published versions itself and ignores it. |
| `description.purpose` | Optional: the node's description. |
| `team.name` | Required: the node's owner. |
| `customProperties` | continuo's settings: items `{vendor: continuo, property: <name>, value: <value>}` (below). Items of another vendor are not read. |
| `schema` | Exactly one schema object: `name` is the table, `physicalType` is `table`, `physicalName` is the table or left out, and `properties` are the table's columns. |

A Python contract may not carry `servers`, `tenant` or `contractCreatedTs`:
the install decides where the table lives and its tenant, and a clock is not
part of a contract.

The `continuo` properties sit on the contract's own `customProperties`. Each
is written at most once:

| Property | Kinds | Value |
| --- | --- | --- |
| `service` | every kind, optional | The name of the node's service. `pack` and `validate --service` refuse one that is not their `--service`. |
| `kind` | — | `python-node` (the default), `python-csv` or `python-api`. |
| `schedule` | every kind, required | The name of the schedule that runs the node: no whitespace, at most 50 characters. |
| `criticality` | every kind, required | `REGULATORY`, `CORE` or `SECONDARY`. |
| `timeoutMinutes` | every kind, optional | The task timeout, an integer from 1 to 10,080 (one week). |
| `script` | `python-node` and `python-api`, required | The path of the script. |
| `readQueries` | `python-node`, required | A mapping of one or more query names to read queries. |
| `csvSource` | `python-csv`, required | The file's address, `s3://<bucket>/<key>` or `https://<host>[/<path>]`. |
| `extraColumns` | every kind, optional | `raise` (the default) or `warn`. |
| `config` | every kind, optional | A mapping: the table's physical layout, in the engine's own vocabulary. |
| `secretRef` | `python-api` only, optional | The name of a Kubernetes Secret: `continuo-api-` followed by lowercase letters, digits and `-`, at most 253 characters. |
| `modeType` | every kind, optional | `replace` (the default), `append` or `upsert`. A `python-csv` node only `replace`. |
| `assets` | `python-node` and `python-api`, optional | A list of the paths of the files the script reads, each listed once. |

A property that the library does not know is refused, and so is one that
describes how dbt builds a node (`filePath`, `materialization`,
`contractEnforced`, `access`, `group`, `deprecationDate`,
`freshnessBuildAfter`, `snapshot`, `quoteColumns`), `provenance`, and `reads`,
which continuo fills itself from the tables the read queries name. A path
(`script`, an asset) is relative to the repository root and written in its
normalised POSIX form: parts separated by `/`, no leading `/`, `~` or drive
letter, and no empty, `.` or `..` part.

Each column (an item of the schema object's `properties`) has:

- a `name`, used by one column only;
- a `physicalType` from the supported set: `BIGINT`, `INTEGER` (or `INT`),
  `DOUBLE PRECISION`, `NUMERIC(p,s)` (or `DECIMAL(p,s)`), `VARCHAR(n)`,
  `CHAR(n)`, `TEXT`, `TIMESTAMP`, `DATE`, `BOOLEAN`;
- `required: true` when it never holds NULL (a column without it is
  nullable);
- `primaryKey: true` and `primaryKeyPosition` when it is part of an upsert
  node's key (see "Write modes" below).

A table has at least one column. A column does not nest (`items`, `map`,
`properties`), carry `logicalTypeOptions`, have a `physicalName` other than
its name, or take part in a partition.

The runtime keeps four rules of its own on top of the library's:

- each read query of a `python-node` is one read query and names the schema
  of every table it reads (see "Read queries" below);
- a `python-csv` node's `csvSource` is an address its readers can fetch;
- `config` is JSON-shaped: string keys, and values that are JSON scalars,
  lists or mappings;
- the names of an upsert key are unique case-insensitively.

### Ontology bindings

A Python contract may bind its table and its columns to the company's
ontology. The bindings are `continuo` properties, each written at most once in
the list where it sits, and no kind needs them:

| Property | Sits on | Value |
| --- | --- | --- |
| `entity` | the schema object | The name of the thing the table holds: a string with no whitespace, dot or slash. |
| `grain` | the schema object | A list of one or more strings, for a table that aggregates instead of holding a thing. |
| `fixed` | the schema object | A mapping of key part names to a string, a number or a boolean. |
| `filter` | the schema object | A mapping of field names to a string, a number or a boolean. |
| `property` | a column | `<Thing>.<field>`: the thing and the field the column holds. |
| `local` | a column | `true`, for a column that no thing owns. A column that is not local leaves it out. |
| `aggregate` | a column | A mapping with the keys `from` (`<Thing>` or `<Thing>.<field>`), `agg`, `where` and `period`, holding at least `from` or `period`. |

The schema object's bindings sit in its own `customProperties`, and a column's
in the column's:

```yaml
schema:
  - name: example
    physicalType: table
    customProperties:
      - {vendor: continuo, property: entity, value: Order}
    properties:
      - name: order_id
        physicalType: INTEGER
        required: true
        customProperties:
          - {vendor: continuo, property: property, value: Order.order_id}
```

When the ontology has a key part or a property of `Order` named `order_id`, the
check binds that column itself, so the `property` above is written out for
clarity.

A binding at the other place, or with a value of another shape, is refused like
any property the library reads. The bindings change nothing the runtime does:
the loader, `validate`, `pack` and the harness accept them, `pack` writes the
file into the bundle as written, and neither the two hash parts of the bundle
nor the code projection behind `config_hash` reads them, so a node's
`content_hash` is the same with or without them.

`continuo-runtime validate <folder> --service <name> --ontology <file>` checks
every contract against the company's ontology file with the rules of
`continuo-contracts` that need no other service (`contract`, `coverage`,
`quality` and `privacy`): each contract is put in the library's stored form,
`service` set to `--service`, and filled from the ontology before the rules
run. An ontology file that the library's `ontology` rule refuses stops the
check first, whatever the service, with one error line that points to
`continuo-contracts ontology check`. The README ("Checking against the
ontology") gives the lines `validate` logs and its exit codes. `--ontology`
needs `--service`, and `--service` alone refuses a contract whose `service`
names another service.

### Kinds

- **`python-node`** (the default) is a script node: its script reads the
  tables its read queries name, and the harness writes what the script
  returns. Everything in §13.4 applies to it.
- **`python-csv`** is a **contract-only** node with no script: it declares no
  Python to run, only the address of one CSV file in `csvSource`. Because
  there is no script or import closure to hash, `source_hash` is
  `sha256(uri_bytes)` over that address and `shared_code_hash` is `""`, so a
  `python-csv` node's `content_hash` changes when the address changes, never
  when the data at that address changes. The harness reads the kind and, for
  a `python-csv` node, skips script execution and instead fetches and parses
  the CSV (`csv_loader.produce_csv` in this repo). It conforms the result to
  the declared columns exactly as it would a script node's returned
  dataframe (§13.4's "Sole write sink" paragraph, unchanged from that point
  on). The validation runner mirrors the same idea at release-validation
  time: its `build_from_columns` op checks the CSV's header line — fetched
  via a `csv_source` key on the candidate spec, without downloading the full
  object — against the declared columns instead of bind-checking a SELECT,
  so a `python-csv` node fails the release gate on a missing declared column
  exactly as a script node fails it on a missing bind.
- **`python-api`** is a script node, like `python-node`, that declares no read
  queries and may name a Kubernetes Secret in `secretRef`; only this kind may
  set it. Its script and import closure are hashed and linted exactly as for
  `python-node`. `secretRef` is part of `config_hash`, so `content_hash`
  changes when the referenced Secret name changes. `source_hash` does not.

### Read queries

- **Each read query is one statement starting with `SELECT` or `WITH`, and
  every table it reads names its schema** (`analytics.table_a`, never
  `table_a`). The one name that goes without a schema is a reference to a CTE
  that is in scope where the name is used. In a `WITH` list a CTE is in scope
  in the CTEs defined after it, in the query that carries the clause and in
  the subqueries of those, and not in its own body: in
  `WITH a AS (SELECT * FROM a) SELECT * FROM a` the `a` inside the CTE is a
  table and must name its schema. In a `WITH RECURSIVE` list every CTE is in
  scope in every body of the list, itself and the CTEs defined after it
  included, and in the query that carries the clause. A table function that
  names its schema (`pg_catalog.generate_series(1, 3)`) reads no relation and
  is not checked. `validate`, `pack` and `hash` refuse a
  read query that breaks either rule, naming the file, the query and the
  first table without a schema. Continuo refuses, when it resolves a release,
  a read query that names a table without its schema (it raises
  `UnqualifiedTableReference` and rejects the whole release), so CI refuses at
  least what the release refuses.
  Tables outside Continuo's registry are allowed (external sources) and
  produce no edge.
- **Read queries are dialect-bound, not dialect-neutral**: Continuo parses,
  rewrites and bind-checks every read query in the **install's warehouse SQL
  dialect**, so authors write each query in their own engine's dialect. A
  query that is valid against postgres can fail `InvalidCompiledSql` on an
  install whose warehouse is Trino, and the other way round.
  `continuo-runtime validate`, `pack` and `hash` accept an optional
  `--dialect <name>` flag (`postgres`, `trino`, `duckdb`) so a domain repo
  checks its queries against that dialect in its own CI instead of at
  Continuo's parser. The queries are always parsed (through
  `continuo_engine_contract.sql.ensure_single_read`, backed by sqlglot), with
  or without `--dialect`: the flag only chooses the grammar, and sqlglot's
  dialect-neutral grammar is used when it is left out. An unknown dialect
  name is refused as a bad flag before any file is read.
- SQL a driver accepts but a parser refuses fails these checks, most commonly
  a driver's bind placeholder such as psycopg2's `%(name)s`, which
  `ctx.read(name)` never fills, since it takes no parameters. Engine-specific
  syntax the dialect-neutral grammar refuses (postgres `~`, `@>`, …) needs
  `--dialect <engine>`. The runtime image does **not** run these checks again
  (§13.4), so a query that passes in CI is not judged again under a different
  grammar in production.

### Physical-layout `config`

Every node may carry a `config` property alongside its typed columns: the
node's physical layout, written directly in the **active engine's own
vocabulary**. It is deliberately *not* engine-namespaced (no
`postgres: {...}` / `trino: {...}` wrapper) — the contract is already
engine-bound, since read queries are written in that same engine's SQL
dialect (see above), so there is no ambiguity to resolve at parse time.

- The active engine's `WarehouseAdapter` **fails closed on any key it does
  not recognize**, at any nesting level — there is no other namespace to
  excuse an unknown key into.
- Recognized keys as shipped:
  - **postgres** → `indexes`: a list of `{columns: [...], unique: bool,
    name: str}` entries. Every `columns` entry must name a declared column;
    an index on an undeclared column raises before any DDL runs. `unique`
    defaults to `false`; `name` defaults to `ix_<table>_<col1>_<col2>...`
    (truncated to 63 bytes if longer).
  - **trino (Iceberg)** → `partitioning` and `sorted_by` (each a non-empty
    list of non-empty strings — column names or Iceberg partition transforms
    like `day(event_ts)`), and `format` (one of `PARQUET`/`ORC`/`AVRO`,
    case-insensitive). An `upsert` node cannot set `format_version: 1`:
    Trino's `MERGE` needs Iceberg format version 2 (the default), and
    `ensure_table` rejects the combination before any DDL.
  - **duckdb (DuckLake)** → `partitioned_by` and `sorted_by`.
    `partitioned_by` is a non-empty list whose entries are a declared column
    name (identity partitioning) or `{column, transform, buckets}` with
    `transform` one of `identity`, `bucket` (needs `buckets`, a positive
    integer), `year`, `month`, `day`, `hour` (these four need a `DATE` or
    `TIMESTAMP` column). `sorted_by` is a non-empty list of declared column
    names or `{column, direction: asc|desc, nulls: first|last}`; free-form
    expressions are rejected. DuckLake has no indexes, so postgres's
    `indexes` is an unrecognized key on this engine.
- Worked example (commented out in `template/contracts/example.odcs.yaml`):
  ```yaml
  customProperties:
    - vendor: continuo
      property: config
      value:
        indexes:
          - columns: [order_id]
            unique: true
  ```
- Applied at runtime by `WarehouseAdapter.ensure_table`, per SQL object, on
  **create-if-absent** — this is not gated on whether the table itself was
  newly created. Postgres evaluates each `indexes` entry's `CREATE INDEX IF
  NOT EXISTS` independently, on every `ensure_table` call, so adding a new
  index entry to a node whose table already exists **does** create that
  index — as does changing `columns` on an entry with no explicit `name`,
  since that changes the derived default name. Iceberg's properties instead
  all ride on a single `WITH (...)` clause attached to the `CREATE TABLE`'s
  own `IF NOT EXISTS`, so once the table exists nothing in `config` is
  applied at all. DuckLake behaves like Iceberg here: `partitioned_by` and
  `sorted_by` are applied only when `ensure_table` creates the table, so
  changing them on an existing table is a silent no-op; the release gate's
  `build_empty_from_columns` always rebuilds with the new layout. None of the
  three engines is a migration mechanism for an index or property that
  already exists under the same name: flipping `unique: false → true` under a
  fixed index `name` is a **silent no-op** on postgres (the name already
  resolves, so `IF NOT EXISTS` skips it), and changing `partitioning` on a
  trino table that already exists is a silent no-op for the reason above —
  not an applied change and not an error in any of these cases. The same
  vocabulary is checked ahead of runtime by `build_empty_from_columns`, so a
  malformed config fails the release gate rather than surfacing in
  production.
- Trino's `format` is case-normalized to uppercase in the emitted DDL, so
  `format: parquet` and `format: PARQUET` produce identical `WITH (...)`
  text — but they are different values in the contract, so they produce
  different `config_hash` (and therefore `content_hash`) values, triggering a
  revalidation that — per the point above — re-applies nothing.
- `config` is part of the code projection like any other field, so it
  participates in `config_hash` with no special-casing in the hash formula:
  adding `config` to a bare node, or editing an existing one, changes
  `content_hash` exactly as editing a read query or a column would.

### Write modes

`modeType` (`replace`, the default; `append`; `upsert`) says how the harness
writes the node's table on each run; `python-csv` allows only `replace`.

- An `upsert` node needs a key: `primaryKey: true` on each key column. Every
  key column is `required: true` and is not `DOUBLE PRECISION`. A key of
  several columns gives each column a `primaryKeyPosition`, a whole number
  from 1, and the key's order is the order of those numbers. The key's names
  are unique case-insensitively.
- On a `replace` or `append` node a key only describes the table: the harness
  passes no key to the adapter, and the code projection leaves it out.
- `modeType` is always part of the code projection; the key is part of it for
  `upsert` only, in key order.

### Assets

`assets` is optional on `python-node` and `python-api`, and refused on
`python-csv`, which has no script: a list of the repository paths of the
files the node's script reads, each in its normalised form (see the path rule
above) and listed once. `pack` and `hash` fail with a `ContractError` for an
asset that is missing, is not a file or resolves outside the repository.
`assets` is part of the code projection (sorted by code point there), so
declaring one changes `config_hash`, and the files' bytes are shared units
(§13.2).

### Task timeout

`timeoutMinutes` is optional on every kind: an integer from 1 to 10,080 (one
week), the node's task timeout. Any other value (zero, above the bound, a
string, a float, a boolean) is refused. It is **outside the code
projection**, so changing it alone changes none of the four hashes and is not
a code change.

### Converting a contract file

The loader refuses any YAML file in the contracts folder that is not a
`*.odcs.yaml` contract, and its message points here. A contract named
`<table>.odcs.yml` is renamed `<table>.odcs.yaml`. A file of `nodes:` entries
converts to one ODCS file per entry, named `contracts/<table>.odcs.yaml`,
that starts with `apiVersion: v3.2.0`, `kind: DataContract` and
`version: "0"`. Each key of an entry moves to one place:

| Key of a `nodes:` entry | Place in the ODCS contract |
| --- | --- |
| `schema`, `table` | `id: <schema>.<table>` (in lower case), `name: <table>`, and a schema object with `name: <table>` and `physicalType: table` |
| `owner` | `team.name` |
| `description` | `description.purpose` |
| `schedule`, `criticality`, `kind`, `script`, `config`, `assets` | `continuo` custom properties of the same names (`criticality` is required) |
| `reads` | `readQueries`; on a `python-csv` node, `{csv: <uri>}` becomes `csvSource: <uri>` |
| `extra_columns`, `secret_ref`, `mode_type`, `timeout_minutes` | `extraColumns`, `secretRef`, `modeType`, `timeoutMinutes` |
| `output_columns` | the schema object's `properties`, in the same order: `type` becomes `physicalType`, and `nullable: false` becomes `required: true` |
| `primary_key` | `primaryKey: true` on each key column, with `primaryKeyPosition` 1, 2, … in the key's order |
| `content_hash` | left out: a contract carries no hash |

`template/contracts/example.odcs.yaml` and
`template/contracts/example_csv.odcs.yaml` show the result. The hash reads a
node's fields, not the file's layout, so a faithful conversion keeps the
node's `content_hash`.

## 13.2 Surface 2 — the hash parts `pack` computes (byte-exact algorithms)

Each node entry of the bundle carries two hash parts, `source_hash` and
`shared_code_hash`. They need the repository's files, so CI computes them:
`continuo-runtime pack` (`continuo_python_runtime.hashing`, through the
continuo-contracts library). The bundle's reader, Continuo's
topology-controller, computes the third part, `config_hash`, from the
contract text with the same library, and folds the three into
`content_hash`. The runtime computes all four itself, with the same code:
`continuo-runtime hash` prints each node's `content_hash`, and the harness
recomputes it at start (§13.4).

```
source_hash      = sha256(script_bytes)                            # bare hex
                   (sha256(csvSource) for python-csv)
shared_code_hash = continuo_contracts.hashing.shared_hash(
                       {repo-relative POSIX path: file bytes})      # bare hex,
                   over the in-repo import closure and the declared assets,
                   "" when there are none (always "" for python-csv)
config_hash      = continuo_contracts.python.projection_hash(
                       continuo_contracts.python.node_from_odcs(contract))
                                                                    # bare hex
content_hash     = "sha256:" + sha256(source_hash + "|" + shared_code_hash
                                       + "|" + config_hash)
```

- `script_bytes` — the node's own script file, byte-for-byte.
- the shared units — the byte content of every file in the node's in-repo
  import closure (defined below) and of every file the node lists in
  `assets` (§13.1), each keyed by its repo-relative POSIX path; a file that is
  both is one unit. The script itself is never a closure member, so
  `source_hash` and `shared_code_hash` never double-count it. The part folds
  one `<path> NUL <sha256 of the bytes>` line per unit, sorted by path, so two
  files that swap their contents change it.
- the code projection — the twelve keys `schema`, `table`, `kind`, `script`,
  `reads`, `output_columns`, `extra_columns`, `config`, `secret_ref`,
  `mode_type`, `primary_key` and `assets` of the library's `PythonNode`,
  written as RFC 8785 canonical JSON. The contract fills them: `schema` and
  `table` from `id` and the schema object's `name`; `reads` from `readQueries`
  (`{csv: <csvSource>}` on a `python-csv` node); `output_columns` from the
  columns (name, `physicalType`, and nullable unless `required`);
  `extra_columns`, `secret_ref` and `mode_type` from `extraColumns`,
  `secretRef` and `modeType`; `primary_key` from the key columns, on an
  `upsert` node only; and `kind`, `script`, `config` and `assets` from the
  properties of those names. Column types are written in their canonical
  spelling (`INT` is `INTEGER`, `DECIMAL(12, 2)` is `NUMERIC(12,2)`), and each
  read query is hashed as written except for LF line endings and the
  whitespace around it. `team.name`, `schedule`, `criticality`, the
  description, `timeoutMinutes`, `service` and `version` are outside the
  projection. The strict YAML reader refuses a value canonical JSON cannot
  write (an integer beyond 2**53 - 1, NaN, an infinity, nesting deeper than 99
  levels, a lone surrogate), so no contract file holds one.

Invariant: editing a node's owner, schedule, criticality, description or
timeout, the order of its keys or of its custom properties, the file's layout,
or the spelling of a column type leaves all four hashes unchanged; any edit to
its script, closure, read queries, columns, assets or `config` changes at
least one part and therefore `content_hash`.

The fold (`content_hash = "sha256:" + sha256(source|shared|config)`) is the
three-part fold of continuo-contracts. topology-controller computes
`config_hash` from each contract's text and folds it with the two parts the
bundle carries. The harness recomputes the same fold from the image's files
and refuses to run a node whose image gives a different `content_hash`
(§13.4), so CI and the image must run the same runtime.

### Closure definition and its documented limits

`shared_code_hash` covers the node's **in-repo import closure**: the
transitive set of repo-internal `.py` files the script reaches through its
own `import` statements, resolved by static AST analysis
(`continuo_python_runtime.closure.resolve_closure`).

- The script itself is excluded (it is `source_hash`); stdlib and installed
  packages are excluded — external dependencies are the image's concern,
  pinned by `image_tag` and recorded in the bundle's `image_packages`, not
  the hash's.
- Resolution tries two search roots per import — the repo root first, then
  the importing file's own directory — with PEP-328 relative imports
  (`from . import x`, `from ..pkg import y`) resolved against the importing
  file's own package. Ancestor `__init__.py` files along a resolved dotted
  path are closure members too, because they execute on import.
- **Limit — over-inclusion by design.** `from pkg import name` cannot be
  statically distinguished from a submodule import, so a same-named in-repo
  file (`pkg/name.py`) is pulled into the closure even when the real import
  was an attribute of `pkg`. The cost is a spurious revalidation, never a
  missed one: under-inclusion would be a correctness bug (a stale node
  silently running in production), while over-inclusion only costs one extra
  revalidation — the tradeoff `closure.py`'s own module docstring documents
  as deliberate.
- **Limit — dynamic imports are rejected, not approximated.** `importlib`
  (in any form), `builtins` (the module, in any form — `import builtins`,
  `from builtins import ...`), `__builtins__` (the name every module's
  globals are injected with at import time; `__builtins__['exec'](...)`
  needs no preceding `import builtins` at all), `__import__`, `exec`, `eval`,
  and any `.import_module(...)` attribute access (on any receiver — the
  predicate doesn't care what object it's called on) are refused wherever
  `continuo-runtime lint` is pointed (typically `scripts/`), and —
  regardless of what lint was pointed at — unconditionally in the script
  **and every closure member** by `resolve_closure` itself, which raises
  `ContractError` on the first one found. `pack`, `hash` and the harness's
  start check all run it, so even a shared helper module outside the linted
  path cannot smuggle one in. What static analysis cannot see must not
  exist. The one plausible legitimate casualty is `from builtins import str`
  (the `python-future` compatibility shim): it is rejected exactly like any
  other `ImportFrom` whose root module is `builtins`, with no exemption.
- **Every closure member is held to the full lint rule set, not only
  dynamic-import rejection.** `continuo-runtime lint` (typically pointed at
  `scripts/` in CI, for fast feedback) checks only the files given to it — a
  helper imported from outside that path, e.g. `lib/shared.py` via
  `from lib.shared import ...`, is invisible to that step.
  `continuo-runtime pack` closes that gap on its own: before computing a
  node's hash parts, it runs the same rules CI's lint applies — L1 no
  warehouse driver imports, L2 no SQL string literals, L3 no forbidden
  data-access calls, L4 no private-attribute access, L5 no dynamic-import
  constructs, L6 no constructs that run code the hash cannot see — over the
  node's script **and every resolved closure member**, and raises
  `ContractError` naming every offending file if any produces a violation.
  Because `pack` is the thing that produces the release bundle, this cannot
  be bypassed by a stale or hand-edited CI workflow file: a helper outside
  `scripts/` can never become a side channel around the harness's sole
  write sink.
- **Data files are not closure members.** A file the script reads (a CSV, a
  JSON lookup, a model) is hashed only when the node lists it in `assets`.

## 13.3 Surface 3 — the release call

```
POST <continuo-origin>/api/v1/releases
Authorization: Bearer <GitHub Actions OIDC token>
{
  "service":    "marketing-py",          # one service name per domain repo
  "release_id": "<unique, matches the S3 key path>",
  "image_tag":  "<registry>/<image>:<tag>",   # the image the executor will run
  "kind":       "python-odcs"
}
```

The kind `python-odcs` tells Continuo the release carries a contract bundle
at `s3://<bucket>/<service>/<release_id>/odcs-bundle.json`. The token's
audience is the origin of the continuo install, and the repository must be
bound to the service in continuo's `ciAuth.bindings`
([Releasing from CI](https://github.com/carolsimone/continuo/blob/main/deploy/README.md#releasing-from-ci-github-actions)).
A CI token supplies `repo` and `commit_sha` (the repository and commit the
workflow runs in), so the body leaves them out; they must point at the actual
source of the scripts and contract files, because the remediation agent
fetches them from GitHub to propose fix PRs. 202 Accepted
`{"release_id": …, "status": …}`; 400 on a missing or unknown field.
Idempotent on `release_id` with the same body — safe to retry. The workflow
then polls `GET /api/v1/releases/{release_id}` with a fresh token until
`terminal` is true: `promoted` is success, `rejected` and `superseded` are
failures. The first release of a service is an operator bootstrap
(`"bootstrap": true`), which a CI binding may send only with
`allowBootstrap`.

## 13.4 Surface 4 — the runtime image

`image_tag` must be pullable by the cluster and behave as follows when the
executor runs it as a Kubernetes Job:

- **Contracts**: the image holds the service's contract files, one
  `*.odcs.yaml` file per node, in `CONTRACT_DIR` (`/app/contracts`), and the
  repository files the nodes run below `APP_ROOT` (`/app`). They are the same
  files CI validated and packed.
- **Selection env**: the executor sets `NODE_ID`, `TABLE_NAME`, and the
  target schema env; the harness loads the contracts in `CONTRACT_DIR`, picks
  the node and runs it. One image serves all of the service's nodes.
- **Start check**: when continuo sets `CONTENT_HASH`, the harness recomputes
  the node's `content_hash` from the files in the image, with the helpers
  `continuo-runtime hash` uses, before it builds the adapter. A different
  value, or a file the release hashed that the image lacks, fails the node
  with `ImageContractMismatch` (details in the env table below).
  `content_hash` is what ties the promoted topology to the image actually
  running.
- **Script import path**: before executing the script the harness prepends
  `APP_ROOT` and the script's own directory to `sys.path`, in that order —
  the same two search roots the closure resolver folds into
  `shared_code_hash` (§13.2) — and leaves them there for the process
  lifetime, so a deferred `import` inside `run()` resolves too. Both forms a
  script can use work: sibling (`import helpers`) and package-qualified
  (`import scripts.helpers`, `from lib.shared import ...`). The image must
  still `COPY` every directory the scripts import from or read their `assets`
  from: a helper or an asset is folded into the hash whether or not it is in
  the image, so one left out yields a valid release bundle and a node that
  fails the start check, or, when `CONTENT_HASH` is unset, dies with
  `ModuleNotFoundError` or `FileNotFoundError`.
- **Warehouse env**: engine-native connection vars (`POSTGRES_HOST`,
  `POSTGRES_DB`, `POSTGRES_USER`, optional `POSTGRES_PORT`/`_PASSWORD`; ditto
  per engine), injected via the same Secret mechanism dbt Jobs use.
- **Sole write sink**: user scripts return a dataframe; the harness runs
  `conform()` (a strict Arrow cast to the declared columns), then calls
  `WarehouseAdapter.ensure_table` — applying the node's `config` physical
  layout on create, see §13.1, and for an `upsert` node its key — and
  performs the only write, chosen by `modeType`: `load` (replace), `append`
  or `upsert`. `append` and `upsert` need the adapter to declare
  `AppendCapable` / `UpsertCapable`; the harness checks that right after
  selecting the node and fails with `LoadError` otherwise. Before an `append`
  or `upsert` it compares the live target table (`TARGET_SCHEMA.TABLE_NAME`)
  with the declared columns — names case-insensitively, nullability exactly,
  order and types ignored — and fails with `LoadError` on any difference. An
  `upsert` whose rows repeat a key fails with `ConformError` and writes
  nothing. Scripts get a read-only connection surface. The harness calls
  `ensure_table(..., config=...)` as a keyword unconditionally, and the
  abstract `WarehouseAdapter` port in `continuo-engine-contract` declares it:
  `ensure_table(self, schema, table, columns, *, config, primary_key=())` —
  `config` required and keyword-only; `primary_key` optional and
  keyword-only, passed by the harness only for upsert nodes. Adapters
  implement exactly that signature; callers pass `{}` when a node declares
  no physical layout.
- **Early `config` tripwire**: `ensure_table` is called only *after* the
  script has run and its result has been conformed, so a bad `config` would
  otherwise burn the entire node run before failing. The harness therefore
  also calls `WarehouseAdapter.validate_config(config, column_names)` — a
  classmethod, no connection needed — immediately after selecting the node,
  and surfaces a rejection as `LoadError`. The adapters shipped here
  implement it by reusing the exact logic `ensure_table` validates with, so
  the two cannot disagree. `validate_config` is not part of the abstract
  port; the harness *skips* the call for an adapter that does not provide
  it, because `ensure_table` remains the enforcement point and such an
  adapter loses only earliness, never fail-closed behavior. Third-party
  adapters should implement it.
- **The read-query checks do not run at run time.** `continuo-runtime run`
  loads the baked-in contracts with `check_reads=False`: every other rule
  (the library's checks of each contract, the shape of each kind's reads,
  `config`, an upsert key's names, duplicate relations) still runs, but each
  read query is neither parsed (`ensure_single_read`) nor checked for tables
  without a schema. The harness has no `--dialect` of its own, so re-parsing
  would judge the queries against the dialect-neutral grammar — a different,
  in places stricter grammar than the one CI used (§13.1) — and could only
  disagree with the gates already passed (CI's `validate` and `pack`, then
  Continuo's own parser and bind-check), never catch a new problem:
  `ctx.read` resolves declared queries by name and never parses them.
- **Result envelope**: stdout is reserved for exactly one sentinel-framed
  result block as the last line — reuse `continuo_engine_contract.result`
  (built in this repo, published to PyPI) rather than reimplementing the
  markers. That wire format is frozen against Continuo's Go parser
  (`pkg/validationresult`). All diagnostics go to stderr via stdlib
  `logging`, including the failure the result block reports, which is
  logged at ERROR. Non-zero exit on failure. The block may carry one
  optional extra key, `reason`, after `unique_id`, only on a failure with a
  machine-readable code (`image_contract_mismatch`); the markers,
  `schema_version` and the five standard keys stay frozen.
- **Packages**: `continuo-runtime packages` prints every distribution
  installed in the image as one JSON object, `{normalised name: version}`,
  sorted by name, and nothing else on stdout. `pack --image` runs it to fill
  the bundle's `image_packages` (§13.1).

### Python-node env contract (normative)

§13.4's "the executor sets `NODE_ID`, `TABLE_NAME`, and the target schema
env" is spelled out loosely above; this subsection pins the exact
environment variable names `continuo-python-runtime`'s harness
(`continuo_python_runtime.harness.run_node`) requires. These names are
**not** the same as the dbt Job env — continuo's executor-controller injects
`SCHEMA`/`DBT_TARGET_SCHEMA` for dbt Jobs and passes a dbt-style `UniqueID`
verbatim; the python dispatch path injects the names below instead:

| Variable | Required? | Meaning |
| --- | --- | --- |
| `NODE_ID` | **required** | Any identifier whose trailing two dot-separated segments MUST be `<schema>.<table>` of the declared node (e.g. `python-node.<service>.<schema>.<table>`); the harness splits on `.` and matches the last two segments against the nodes the contracts declare. |
| `TABLE_NAME` | **required** | The target table name; must match the `table` of the node selected via `NODE_ID`. |
| `TARGET_SCHEMA` | **required** | The target schema name; must match the `schema` of the node selected via `NODE_ID`. There is no fallback — `SCHEMA` and `DBT_TARGET_SCHEMA` are **not** recognized. |
| `TASK_ATTEMPT` | set by the executor, not read by the harness | The 0-based number of the task's attempt: `0` on a task's first run, `1` on its first retry, and so on. The executor sets it on every production Job, dbt and python alike, so a node script can read it; the harness neither requires nor interprets it. |
| `CONTENT_HASH` | optional | The `content_hash` the release promoted for the node, set by continuo; absent or empty means no check. When set, the harness recomputes the node's hash from the files in the image (the helpers `continuo-runtime hash` uses) before it builds the adapter, and a different value, a value that is not byte-for-byte the recomputed string (no `sha256:` prefix, upper-case hex, surrounding whitespace) or a file the release hashed that the image lacks fails the node with `ImageContractMismatch`: one result block with `"reason":"image_contract_mismatch"` and exit 1. The message names the node, `CONTENT_HASH`, the image's `content_hash`, its three parts, the sorted unit paths and the runtime version, and says to rebuild the image from the released commit. Unset or empty logs a WARNING and runs. |
| `CONTRACT_DIR` | optional | The folder of the node contracts baked into the image, one `*.odcs.yaml` file per node. Defaults to `/app/contracts`. |
| `APP_ROOT` | optional | Repository root the node's `script` path is resolved against. Defaults to `CONTRACT_DIR`'s parent directory. |
| engine-native vars | **required**, per adapter | Whatever the installed `WarehouseAdapter.required_env()` declares (e.g. `POSTGRES_HOST`/`POSTGRES_DB`/`POSTGRES_USER`, optionally `POSTGRES_PORT`/`POSTGRES_PASSWORD`) — missing any of these is a `LoadError`. |

The executor's python dispatch injects `NODE_ID`/`TABLE_NAME`/`TARGET_SCHEMA`
under exactly these names; they intentionally differ from the dbt job env
(`SCHEMA`/`DBT_TARGET_SCHEMA`/dbt `UniqueID`) because the python-node harness
has a different selection model (contracts keyed by `schema.table`, not a
dbt manifest node). It also sets `TASK_ATTEMPT` on the same Jobs, under the
same name as on a dbt Job.

## 13.5 Surface 5 — what the domain repo's CI/CD does on each push to main

```
1. install the continuo-python-runtime version of the image's FROM tag (fail when they differ)
2. lint:     continuo-runtime lint scripts/
3. validate: continuo-runtime validate contracts/ [--dialect <engine>]
             (then the domain's own tests, when it has any)
4. build:    docker build the image (scripts + contracts + harness baked in)
5. push:     docker push the image
6. pack:     continuo-runtime pack contracts/ --service <service> --repo-root .
                 --image <the pushed image> [--dialect <engine>] --out odcs-bundle.json
7. upload:   odcs-bundle.json → s3://<bucket>/<service>/<release_id>/odcs-bundle.json
8. POST /api/v1/releases {…, "kind": "python-odcs"}   # only after 5 and 7 succeed; then poll to a terminal status
```

`pack` runs after the push because it reads the package list of the pushed
image (§13.1). `template/.github/workflows/release.yml` runs these steps.

Provisioning Continuo hands each domain repo, once: S3 write credentials
scoped to `s3://<bucket>/<service>/*`, the release endpoint URL and the
repository's binding in `ciAuth.bindings`, registry push credentials, and a
warehouse role limited to `SELECT` on declared upstreams plus the harness's
write path.

---

## Bottom line

The contract split — **declared read queries for the graph and the
bind-checked inputs, declared columns for the shape** — keeps validation
image-free and portable (a bare typed `CREATE TABLE`), and threads python
nodes through the same release and promotion machinery as dbt nodes. It
leaves the runtime two obligations: **output conformance** (a strict Arrow
cast in a sole-sink harness, backstopped by the typed table) and **read
honesty** (grant-scoped, fail-closed). dbt and python nodes are peers in one
DAG; dbt-core remains an ingest dialect, not a dependency.
