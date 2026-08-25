# Design doc — BYO engine-adapter packages & deterministic python-node containers

**Status:** Draft for review — not approved, not scheduled.
**Repos:** `continuo-python-runtime` (primary). Doc/link touch-ups only in `continuo`, `continuo-demo`.
**Scope:** publish the engine adapters to PyPI under new names; add a deterministic "own-base" domain template; make both generic across current and future engines.
**Date:** 2026-08-25

---

## 1. Problem

A data/ML engineer can build a python-node container exactly one way today: `FROM` a Continuo engine image.

| Shape | Dockerfile base | Deterministic? | Needs |
| --- | --- | --- | --- |
| **1 — FROM the engine image** | `FROM ghcr.io/carolsimone/continuo-python-runtime-postgres@sha256:…` | Yes (pin by digest) | nothing new |
| **2 — own base + pip** | `FROM python:3.14-slim` + `pip install …` | Only if the adapter is a pinned, hash-locked, published package | the adapter on an index |

Shape 1 is already supported and reproducible (the demo template and the `service-py-ml` node use it). It forces the Continuo base image, though — its OS, Python build, and layers. Teams that must build on their own base image, or reuse the adapter's `WarehouseAdapter` methods in another context, have no deterministic path today:

- The engine **adapters are not installable from a package index.** They ship only as the per-engine container images, built **from source** (`Dockerfile.postgres`/`.trino` `COPY adapters/<engine>` then `pip install` the local path).
- "From source" is not a template we can hand out: users don't have the repo tree, and even with it, a source build tracks repo head, so it is not reproducible over time.

The runtime and contract are already on PyPI (`continuo-python-runtime==0.4.0`, `continuo-engine-contract==0.7.2`); only the adapter is missing. The adapters *were* published once (`continuo-python-runtime-postgres 0.1.0`) and **yanked** — that release predates the validation→runtime merge and references names that no longer exist (`continuo-validation-contract`, entry-point group `continuo_runtime.adapters`), so it is stale, not evidence the idea was rejected.

## 2. Goals & non-goals

**Goals**
- Every engine adapter installable from PyPI as a versioned, **hash-lockable** package.
- The **engine images install the versioned published libraries** (never the in-repo source), so the shipped image equals what is on PyPI.
- A deterministic **Shape-2** domain template (own base + pip).
- **Generic across engines** — postgres, trino, and future (Snowflake, BigQuery): adding an engine is publish-by-addition.

**Non-goals**
- **Renaming the engine container images.** The image *name* `continuo-python-runtime-<engine>` is a frozen cross-repo contract (domain repos `FROM` it; the Helm chart resolves `validation.engine` → that image; the demo documents it). The name is out of scope, permanently. How the image is *built* does change — see D2/D8.
- Changing the runtime or contract packages — already published, unaffected.
- Changing the node-authoring contract (`run(ctx) -> Arrow table`) — unchanged.
- Supporting more than one adapter installed in a single environment — still exactly one per image/env.

## 3. Decisions

- **D1 — Rename the adapter distribution and module.** Dist `continuo-python-runtime-<engine>` → **`continuo-<engine>-adapter`**; import module `continuo_python_runtime_<engine>` → **`continuo_<engine>_adapter`**. Renamed together so `pip install continuo-postgres-adapter` yields `import continuo_postgres_adapter` (no pip-name ≠ import-name trap). Entry-point **group** `continuo_engine.adapters` is unchanged; only the entry-point **target string** moves to the new module.
- **D2 — The engine images install versioned wheels, never source.** `Dockerfile.postgres/.trino` stop `COPY`-ing `contract/ continuo_python_runtime/ adapters/<engine>` and `pip install`-ing the local tree. They install the pinned, published libraries — `continuo-python-runtime==X` + `continuo-<engine>-adapter==Y` (contract pulled transitively, pinned) — so the shipped image equals what is on PyPI. A build `ARG` selects the wheel source: the **release** build installs from PyPI with `--require-hashes -r requirements-<engine>.lock`; **CI/PR** builds install the same versions from a locally-built wheelhouse (`uv build --all-packages -o wheelhouse`, `--find-links wheelhouse`, no hash gate) so an unreleased change is still testable. No path installs source. The D1 rename keeps the workspace-member paths and engine keys (`postgres`/`trino`), so `[tool.uv.workspace].members` and the image name are unchanged.
- **D3 — Publish adapters on the existing `v*` tag** via a workspace-driven matrix in `publish-pypi.yml`. No new tag, no new glob (the repo's non-release tags start with `python-…`, which `v*` never matches; the repo already consolidated *away* from per-package tags).
- **D4 — `skip-existing: true`** on the PyPI publish. Each package publishes at its own pyproject version, independent of the tag; adapters change rarely, so most `v*` tags carry an unchanged adapter version. Without the flag PyPI 400s on the duplicate and fails the whole publish. (This gap already exists latently for `contract` — masked only because contract changes often.)
- **D5 — Image ≠ pip name, on purpose.** The two artifacts serve the two shapes. Cross-link them in both READMEs so nobody treats them as different things.
- **D6 — Retire the old PyPI projects gracefully.** Keep `continuo-python-runtime-<engine>` `0.1.0` yanked; edit its description to point at `continuo-<engine>-adapter`.
- **D7 — Add a Shape-2 template** (own base + pip) alongside the existing Shape-1 one, installing from a **committed hash-locked requirements file** — a `==` pin alone is not determinism.
- **D8 — One tag, ordered jobs — not two PRs.** Because the release image installs the adapter from PyPI, the wheel must be live before the image build pins it. This is handled *within a single `v*` release run*, not by publishing in one PR and pinning in a second: the tag-time image-publish job `needs` the wheel-publish job (merge both into one release workflow, or gate with `workflow_run` on `publish-pypi` success), and the image build opens with a bounded **wait-until-installable** retry to absorb PyPI propagation lag. One PR bumps the package version *and* its pin/lock together; the tag then publishes-then-builds. A wheel-publish failure blocks the image, by design. This retires the current "images build from repo head, no propagation lag" property in favour of image ≡ PyPI. **Hash caveat (the only place a two-step can appear):** `--require-hashes` needs the wheel's hash, which exists only once the wheel is built. Resolve it with **reproducible wheel builds** (`SOURCE_DATE_EPOCH`, hatchling reproducible) so a hash computed in the PR matches the CI-published wheel — keeping it one PR/one tag. If builds are not byte-reproducible, the fallback is publish-then-pin: publish on a `-testN` tag, regenerate the lock's hashes against the index, then release — a genuine two-step, used only if reproducibility can't be achieved.

## 4. Changes by file

### `continuo-python-runtime` (primary)

**Adapter packages** — for each of `adapters/postgres/`, `adapters/trino/`:
- Rename the module directory: `continuo_python_runtime_<engine>/` → `continuo_<engine>_adapter/`.
- `pyproject.toml`: `name = "continuo-<engine>-adapter"`; `[tool.hatch.build.targets.wheel] packages = ["continuo_<engine>_adapter"]`; entry-point target → `continuo_<engine>_adapter.adapter:<Engine>Adapter`.
- Update the adapter's own internal imports and its `tests/` imports to the new module path.
- Version: keep the current `0.2.0` under the new name (same code, honest maturity); the yanked old-name `0.1.0` is a separate project and does not constrain this.

**`pyproject.toml` (root):** no change to `[tool.uv.workspace].members` (paths unchanged). `[tool.uv.sources]` unchanged.

**`Dockerfile.postgres` / `Dockerfile.trino`:** rewritten to install versioned wheels instead of source (D2). Drop the `COPY contract / continuo_python_runtime / adapters/<engine>` + `pip install /src/...`. Replace with an `ARG WHEEL_SOURCE`-driven install of `continuo-python-runtime==X` + `continuo-<engine>-adapter==Y` — from PyPI (`--require-hashes -r requirements-<engine>.lock`) for release, from a local wheelhouse (`--find-links`) for CI/PR. Keep everything below the install unchanged (`ENV CONTRACT_DIR/APP_ROOT/PYTHONPATH`, `WORKDIR /app`, uid 65532, `ENTRYPOINT ["continuo-runtime"]`, `CMD ["run"]`). No system packages needed — psycopg2-binary and trino ship as wheels.

**`requirements-postgres.lock` / `requirements-trino.lock` (new):** hash-locked pins of `continuo-python-runtime` + `continuo-<engine>-adapter` + their transitive closure, consumed by the release image build. Regenerated by a documented `uv pip compile --generate-hashes` command whenever a pinned version bumps; a CI check diffs them against the pyproject versions so a bump can't ship without a matching lock.

**`.github/workflows/publish-pypi.yml`:**
- Add a build step per adapter: `uv build --package continuo-<engine>-adapter -o dist` (matrix or explicit per engine, mirroring how contract + runtime are built into one `dist/`).
- Build wheels **reproducibly** (`SOURCE_DATE_EPOCH` pinned) so the hash in `requirements-<engine>.lock` matches the published wheel (D8 hash caveat).
- Add `skip-existing: true` to **both** publish steps (PyPI and TestPyPI).
- Update the header comment: adapters are now published (remove the "deliberately NOT published" paragraph).

**`.github/workflows/images.yml`:** the tag-time image-publish job now installs from PyPI, so it must run **after** the wheels are published (D8) — merge it into the release/publish workflow or gate it with `workflow_run` on `publish-pypi` success, and add the wait-until-installable retry. The PR smoke jobs build the image from a locally-built wheelhouse (same wheels, `--find-links`) instead of source, so they still exercise the real wheel-install path without needing anything on PyPI.

**`template/`:**
- Add **Shape-2** artifacts: a `Dockerfile.pip` (own base + pip, replicating the base image's `ENV CONTRACT_DIR=/app/contracts APP_ROOT=/app PYTHONPATH=/app`, `WORKDIR /app`, uid `65532`, `ENTRYPOINT ["continuo-runtime"]`, `CMD ["run"]`), a committed `requirements.lock` (hashes) pinning `continuo-python-runtime==X` + `continuo-<engine>-adapter==Y` and their transitive closure, and a documented regen command (`uv pip compile --generate-hashes`).
- `template/README.md`: document Shape 1 vs Shape 2, when to pick which, and how to regenerate the lock.

**`README.md`:** name the adapter packages; add a "build your own container" section covering both shapes; cross-link image ↔ pip package.

**`docs/boundary-contract.md`:** update any adapter package/module names it references.

### `continuo` (doc-only, later)
- `docs/arch/services/*` and README/roadmap mentions of the runtime image stay accurate (image names unchanged); note that pip adapter packages now exist. README/roadmap ML/BYO copy is **deferred** (owner asked to hold).

### `continuo-demo` (optional, later)
- Keep the Shape-1 example. A Shape-2 example service is a possible follow-up, not required here.

## 5. Versioning & determinism

- The `v*` git tag is a **trigger**, not a version; each package publishes at its own pyproject version. `skip-existing: true` makes a mixed changed/unchanged release safe.
- The **engine image is now a normal external consumer** of the same index and pins, so **image ≡ published version** — there is no privileged from-source image build, and a Shape-1 (`FROM` image) and Shape-2 (own base + pip) build of the same tag install byte-identical libraries.
- A domain repo pins `continuo-python-runtime==X` and `continuo-<engine>-adapter==Y`, compiles a **hash-locked** requirements file, and the Shape-2 Dockerfile installs it with `--require-hashes`. Version pins alone are necessary but not sufficient.
- Both runtime and adapter require **Python ≥ 3.14**, so a Shape-2 base must be `python:3.14+`.
- Old-name projects stay yanked; new-name lines start clean. Installers ignore yanked releases unless a build explicitly pins one, so nobody pulls `0.1.0` by accident.

## 6. Testing & verification

- **Rename regression guard:** a test asserting, per engine, that dist name ↔ import module ↔ entry-point target agree (locks D1 so the pip≠import trap can't silently return).
- **Entry-point discovery:** `discover_adapter()` resolves the renamed module (unit test).
- **Shape-2 pip smoke:** build a wheel (`uv build --package continuo-<engine>-adapter`), `pip install` it + the runtime into a clean venv, and run a node end-to-end against a live engine — the pip analogue of the existing `images.yml` image smoke.
- **PR image smoke (wheelhouse):** `images.yml` PR jobs build the image from a locally-built wheelhouse (not source) and run the existing node smoke, so the wheel-install path is exercised on every PR without needing anything published.
- **Reproducible-wheel guard:** build a wheel twice and assert identical hashes (protects the D8 hash caveat; a regression here breaks `--require-hashes` on the release image).
- **Multiple-adapter behavior:** a test that `discover_adapter()` fails **loudly** if more than one adapter is installed (defensive; see Open questions).
- **TestPyPI dry run (full release path):** cut a `v<ver>-testN` tag first; confirm all four projects (runtime, contract, both adapters) land on TestPyPI with `skip-existing` behaving, **and** that the engine image builds from TestPyPI through the publish→wait→build ordering (D8) — before any real `v*`.

## 7. Rollout / ordering

The order matters: the image can't consume the published adapter until publishing exists, so publishing lands before the image switch.

1. Rename modules + dists + entry-point targets; update tests; green CI (images still build from source here + rename guard).
2. Register **pending trusted publishers** for `continuo-postgres-adapter` and `continuo-trino-adapter` on **TestPyPI and PyPI** (one per project × this workflow × environment).
3. Add the adapter publish matrix + `skip-existing: true` + reproducible wheel builds; cut a `-testN` tag; verify all four projects land on TestPyPI.
4. Switch `Dockerfile.postgres/.trino` to the wheel-install (D2), add the `requirements-<engine>.lock` files + reproducible-wheel guard, wire the publish→wait→image ordering (D8), and move PR smoke to the wheelhouse build. Re-run the `-testN` tag to prove the image builds from TestPyPI through the ordering.
5. Cut a real `v*` tag → adapters live on PyPI **and** the release images build from those exact PyPI versions.
6. Add the Shape-2 template + committed lock + template README.
7. Edit the old projects' PyPI descriptions to point at the new names (they stay yanked).

## 8. Risks

- **pip ≠ import trap** if the module is not renamed with the dist → the D1 rename-guard test blocks it.
- **Trusted-publisher misconfig** (workflow filename or environment must match the PyPI registration) → the TestPyPI dry run (step 3) catches it before real PyPI.
- **`skip-existing` can mask a forgotten version bump** — a changed package published without a bump is silently skipped. Accept it (bumps are intentional) but call it out in the workflow comment.
- **Two adapters in one env** → `discover_adapter()` ambiguity. Mitigated by the fail-loud test; the single-adapter rule is unchanged.
- **Image consumers** are unaffected (image names unchanged, discovery by group), but the rename must not touch the entry-point group — it does not.
- **Propagation lag / ordering (D8):** the release image can't build until its wheels are live on the index, and a wheel-publish failure blocks the image. Mitigated by the bounded wait-until-installable retry and the `-testN` dry run. This is exactly the property the old from-source build traded away — accepted deliberately for image ≡ PyPI.
- **Reproducible-wheel hash drift:** if the committed `requirements-<engine>.lock` hash ≠ the CI-published wheel's hash, `--require-hashes` fails the release image build. Mitigated by reproducible wheel builds (`SOURCE_DATE_EPOCH`) + the build-twice guard; fallback is the publish-then-pin two-step (D8).
- **PR vs release build divergence:** PR builds from a local wheelhouse, release from PyPI — same versions and Dockerfile, but hashes enforced only on release. Accepted: PR tests contents, release enforces determinism.

## 9. Open questions

1. **Adapter version under the new name:** keep `0.2.0` (recommended — same code), or reset/realign (e.g. to `1.0.0` to signal a stable public API)? Publishing as a public package is a good moment to decide the API-stability signal.
2. **Fail-loud on multiple adapters:** make `discover_adapter()` raise on >1 installed adapter now, or document-only? (Recommend: raise — cheap, prevents a confusing runtime pick.)
3. **Lock delivery:** commit `requirements.lock` in `template/` (recommended) vs. generate at build time. Committed is reproducible and reviewable; generated drifts.
4. **Reproducible wheels:** confirm hatchling + `SOURCE_DATE_EPOCH` yields byte-identical wheels so the committed `requirements-<engine>.lock` hash matches the published wheel (keeps release at one PR / one tag). If it can't be guaranteed, accept the D8 publish-then-pin fallback as the standing process.
