"""Test the domain-repo template."""

import json
import os
import shutil
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from continuo_python_runtime.cli import main

TEMPLATE = Path(__file__).parent.parent / "template"


def test_template_passes_lint_validate_merge(tmp_path):
    """Template must pass lint, validate, and merge as-is."""
    assert main(["lint", str(TEMPLATE / "scripts")]) == 0
    assert main(["validate", str(TEMPLATE / "contracts")]) == 0
    out = tmp_path / "contract.yaml"
    assert main(["merge", str(TEMPLATE / "contracts"), "--service", "example",
                 "--repo-root", str(TEMPLATE), "--out", str(out)]) == 0
    assert out.exists()


def test_template_passes_hash(capsys):
    """Template must also pass the hash subcommand: one tab-separated sha256
    line per contract node — the sql-node example and the csv-node example."""
    assert main(["hash", str(TEMPLATE / "contracts"), "--repo-root", str(TEMPLATE)]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 2
    by_relation = {}
    for line in lines:
        relation, hash_value = line.split("\t")
        assert hash_value.startswith("sha256:")
        by_relation[relation] = hash_value
    assert set(by_relation) == {"analytics.example", "analytics.example_csv"}


def test_template_demonstrates_multiple_named_reads():
    """The example must declare more than one read, and use each by name.

    Node authors copy this file verbatim, so a single-entry `reads:` map reads
    as a limit of the runtime rather than as the minimal case.
    """
    doc = yaml.safe_load((TEMPLATE / "contracts" / "example.yml").read_text())
    reads = doc["nodes"][0]["reads"]
    assert len(reads) >= 2, "template must show the named-read map with 2+ entries"

    script = (TEMPLATE / "scripts" / "example.py").read_text()
    for name in reads:
        assert f'ctx.read("{name}")' in script, f"declared read {name!r} unused by the script"


def _release_job():
    workflow = yaml.safe_load(
        (TEMPLATE / ".github" / "workflows" / "release.yml").read_text()
    )
    return workflow, workflow["jobs"]["release"]


def _step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def _logical_lines(script):
    """The script's lines with backslash continuations joined."""
    return script.replace("\\\n", " ").splitlines()


def test_release_workflow_queues_runs_instead_of_cancelling_them():
    """A run waiting for continuo's verdict must not be cancelled by a newer push."""
    workflow, _ = _release_job()

    assert workflow["concurrency"] == {
        "group": "release",
        "cancel-in-progress": False,
    }


def test_release_workflow_uses_the_public_release_api():
    """The release call is the authenticated public API, preflighted and bounded."""
    workflow, job = _release_job()
    names = [step.get("name") for step in job["steps"]]
    preflight = _step(job, "Preflight continuo API")["run"]
    submit = _step(job, "Submit release to continuo")["run"]

    assert job["permissions"]["id-token"] == "write"
    assert job["timeout-minutes"] == 60
    assert workflow["env"]["RELEASE_ENDPOINT"] == "${{ vars.RELEASE_ENDPOINT }}"
    # The endpoint check and the preflight run before anything is built.
    assert names[:2] == ["Check release endpoint", "Preflight continuo API"]
    assert names.index("Preflight continuo API") < names.index("Build and push image")
    assert names.index("Submit release to continuo") > names.index("Upload contract artifact")
    assert "/api/v1/current-prod" in preflight
    assert "ciAuth.bindings" in preflight
    assert "audience=" in preflight
    assert "/api/v1/releases" in submit
    assert "terminal" in submit
    assert "seq 1 300" in submit  # a poll bounded to 50 minutes
    assert "jq -n" in submit
    # Retries sleep between attempts, never after the last one.
    for script in (preflight, submit):
        assert 'for attempt in 1 2 3' in script
        assert '[ "$attempt" -eq 3 ] || sleep' in script


def test_release_workflow_never_exposes_the_token():
    """No token value is echoed, and tokens reach curl only as a header read from stdin."""
    _, job = _release_job()
    scripts = [step["run"] for step in job["steps"] if "run" in step]
    lines = [line.strip() for script in scripts for line in _logical_lines(script)]

    assert not any(line.startswith("set -x") or " set -x" in line for line in lines)
    # Both token-carrying curl calls read their header from stdin.
    assert "\n".join(lines).count("-H @-") == 2
    token_refs = ("$token", "${token}", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    for line in lines:
        if any(ref in line for ref in token_refs):
            # The only uses: a header printed into a pipe that feeds curl.
            assert "printf 'Authorization:" in line and "| curl" in line, line
        if line.startswith(("echo", "printf")) and "Authorization:" not in line:
            assert not any(ref in line for ref in token_refs), line
        # No curl command line carries an Authorization header literally.
        assert "-H 'Authorization" not in line and '-H "Authorization' not in line, line


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("", False),
        ("continuo.example.com", False),
        ("https://continuo.example.com/api", False),
        ("https://continuo.example.com?x=1", False),
        ("https://continuo.example.com#frag", False),
        ("https://continuo.example.com\n", False),
        ("https://continuo.example.com\nINJECTED=1", False),
        ("https://continuo.example.com\nhttps://other.example.com", False),
        ("https://continuo.example.com/\nINJECTED=1", False),
        ("https://user:secret@continuo.example.com", False),
        ("https://continuo.example.com ", False),
        ("https://continuo.example.com", True),
        ("https://continuo.example.com/", True),
        ("http://localhost:8090", True),
    ],
)
def test_release_workflow_fails_closed_on_a_bad_endpoint(tmp_path, endpoint, ok):
    """The first step rejects an unset or malformed RELEASE_ENDPOINT before any build.

    The whole value must match, so a multi-line value cannot smuggle extra
    lines into $GITHUB_ENV, and the value is never echoed back.
    """
    _, job = _release_job()
    assert job["steps"][0]["name"] == "Check release endpoint"
    github_env = tmp_path / "github_env"
    github_env.touch()
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", job["steps"][0]["run"]],
        env={"PATH": os.environ["PATH"], "RELEASE_ENDPOINT": endpoint, "GITHUB_ENV": str(github_env)},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    if ok:
        assert github_env.read_text() == f"CONTINUO_ORIGIN={endpoint.rstrip('/')}\n"
    else:
        assert github_env.read_text() == ""
        assert "RELEASE_ENDPOINT" in result.stdout
        assert "secret" not in result.stdout and "INJECTED" not in result.stdout


class _Continuo(BaseHTTPRequestHandler):
    """A stub of the OIDC token endpoint and continuo's /api/v1/current-prod."""

    current_prod_status = 200

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/oidc"):
            status, body = 200, {"value": "stub-jwt"}
        elif self.headers.get("Authorization") != "Bearer stub-jwt":
            status, body = 401, {"error": "bad token", "code": "invalid_token"}
        else:
            status = type(self).current_prod_status
            body = {"current_prod_release_id": "r1"} if status == 200 else {"error": "no", "code": "forbidden"}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.skipif(not (shutil.which("curl") and shutil.which("jq")), reason="needs curl and jq")
@pytest.mark.parametrize(("status", "ok"), [(200, True), (401, False), (403, False)])
def test_release_workflow_preflight_names_the_binding_on_refusal(tmp_path, status, ok):
    """The preflight calls current-prod with a bearer token and explains a refusal."""
    _, job = _release_job()
    handler = type("Handler", (_Continuo,), {"current_prod_status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        result = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", _step(job, "Preflight continuo API")["run"]],
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(tmp_path),
                "CONTINUO_ORIGIN": origin,
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
                "ACTIONS_ID_TOKEN_REQUEST_URL": f"{origin}/oidc?api-version=2.0",
            },
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert "stub-jwt" not in result.stdout + result.stderr
    if not ok:
        assert "ciAuth.bindings" in result.stdout
        assert "auth.publicUrl" in result.stdout


def _pin(tmp_path, dockerfile: str):
    """Run the "Pin the runtime to the image" step for real against a Dockerfile.

    Returns the exit code, what the step appended to ``$GITHUB_ENV`` and its
    stdout (where the ``::error::`` annotation goes).
    """
    _, job = _release_job()
    (tmp_path / "Dockerfile").write_bytes(dockerfile.encode())
    env_file = tmp_path / "github_env"
    env_file.write_text("")
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         _step(job, "Pin the runtime to the image")["run"]],
        cwd=tmp_path, env={**os.environ, "GITHUB_ENV": str(env_file)},
        capture_output=True, text=True,
    )
    return done.returncode, env_file.read_text(), done.stdout


_POSTGRES = "ghcr.io/carolsimone/continuo-python-runtime-postgres"


@pytest.mark.parametrize(("from_line", "version"), [
    (f"FROM {_POSTGRES}:v0.12.0", "0.12.0"),
    (f"FROM ghcr.io/carolsimone/continuo-python-runtime-trino:v0.12.0@sha256:{'a' * 64}", "0.12.0"),
    (f"FROM {_POSTGRES}:v0.12.0   \t", "0.12.0"),
    ("FROM python:3.14.8-slim", None),
    (f"FROM {_POSTGRES}:latest", None),
    (f"FROM {_POSTGRES}:v0.12", None),
    (f"FROM {_POSTGRES}:v0.12.0 AS base", None),
    (f"FROM --platform=linux/amd64 {_POSTGRES}:v0.12.0", None),
    (f"# FROM {_POSTGRES}:v0.12.0", None),
])
def test_the_pin_step_reads_the_runtime_version_from_the_from_tag(tmp_path, from_line, version):
    code, written, out = _pin(tmp_path, from_line + "\nCOPY contracts/ /app/contracts/\n")
    if version:
        assert code == 0 and written == f"RUNTIME_VERSION={version}\n"
    else:
        # A line the step does not parse fails the job with a clear error and
        # writes nothing, so CI never installs a runtime it guessed.
        assert code == 1 and written == ""
        assert out.startswith("::error::Dockerfile has no FROM ghcr.io/carolsimone/continuo-python-runtime-")


def test_the_pin_step_reads_a_crlf_dockerfile(tmp_path):
    """A Windows checkout's carriage return is whitespace, never part of the version."""
    code, written, _ = _pin(tmp_path, f"FROM {_POSTGRES}:v0.12.0\r\nCOPY contracts/ /app/contracts/\r\n")
    assert code == 0 and written == "RUNTIME_VERSION=0.12.0\n"


def test_the_pin_step_takes_the_first_runtime_line_of_a_multi_stage_build(tmp_path):
    dockerfile = (
        "FROM python:3.14.8-slim\n"
        f"FROM {_POSTGRES}:v0.12.0\n"
        "FROM ghcr.io/carolsimone/continuo-python-runtime-trino:v0.13.0\n"
    )
    code, written, _ = _pin(tmp_path, dockerfile)
    assert code == 0 and written == "RUNTIME_VERSION=0.12.0\n"


def test_the_pin_step_parses_the_templates_own_dockerfile(tmp_path):
    code, written, _ = _pin(tmp_path, (TEMPLATE / "Dockerfile").read_text())
    assert code == 0 and written.startswith("RUNTIME_VERSION=")


def test_ci_installs_the_pinned_runtime_before_it_checks_the_installed_version():
    _, job = _release_job()
    steps = job["steps"]
    checkout = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout"))
    names = [s.get("name") for s in steps]
    assert checkout < names.index("Pin the runtime to the image") < names.index("Install runtime tooling")
    install, _ = _install_and_check()
    assert install == 'uv tool install "continuo-python-runtime==${RUNTIME_VERSION}"'


def _install_and_check():
    """The "Install runtime tooling" step as (install command, version check)."""
    _, job = _release_job()
    install, check = _step(job, "Install runtime tooling")["run"].split("\n", 1)
    return install, check


def _fixture_wheel(directory: Path, version: str) -> Path:
    """A real wheel of a package named continuo-python-runtime at ``version``.

    It carries one console script and no dependencies, so ``uv tool install``
    needs no index. Its script knows no ``--version`` flag, like a runtime
    release that predates one.
    """
    dist_info = f"continuo_python_runtime-{version}.dist-info"
    files = {
        "continuo_runtime_fixture.py": "def main():\n    raise SystemExit(2)\n",
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: continuo-python-runtime\nVersion: {version}\n",
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/entry_points.txt": "[console_scripts]\ncontinuo-runtime = continuo_runtime_fixture:main\n",
    }
    files[f"{dist_info}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{dist_info}/RECORD"])
    wheel = directory / f"continuo_python_runtime-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


def _run_check(env):
    """Run the step's version check the way GitHub runs a default shell: ``bash -e``, no pipefail."""
    _, check = _install_and_check()
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", check], env=env, capture_output=True, text=True,
    )


def _check_installed_runtime(tmp_path, install_args, runtime_version, extra_env=None):
    """Install with a real ``uv tool install``, then run the step's version check.

    Returns the check's exit code and stdout. The tool directories are private
    to ``tmp_path``, so the host's tools are neither read nor changed.
    """
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "RUNTIME_VERSION": runtime_version,
        **(extra_env or {}),
    }
    subprocess.run(["uv", "tool", "install", *install_args], env=env, check=True, capture_output=True)
    done = _run_check(env)
    return done.returncode, done.stdout


@pytest.mark.parametrize(("installed", "pinned", "ok"), [
    ("0.11.0", "0.11.0", True),
    ("0.12.0", "0.12.0", True),
    ("0.11.0", "0.12.0", False),
    ("0.12.0", "0.11.0", False),
    ("0.12.0", "0.1.0", False),
])
def test_the_version_check_compares_the_installed_tool_with_the_pin(tmp_path, installed, pinned, ok):
    """The check reads the version from ``uv tool list``, which every release supports.

    The fixture's script has no ``--version`` flag, so a check that ran it would fail here.
    """
    wheel = _fixture_wheel(tmp_path, installed)
    code, out = _check_installed_runtime(tmp_path, [str(wheel)], pinned)
    assert (code == 0) is ok, out
    if not ok:
        assert out.strip() == (
            f"::error::CI installed '{installed}', but the image runs continuo-python-runtime {pinned}"
        )


@pytest.mark.parametrize("force_colour", ["FORCE_COLOR", "CLICOLOR_FORCE"])
def test_the_version_check_ignores_forced_colour(tmp_path, force_colour):
    """uv wraps its output in ANSI escapes when colour is forced; the check still reads the version."""
    wheel = _fixture_wheel(tmp_path, "0.12.0")
    code, out = _check_installed_runtime(tmp_path, [str(wheel)], "0.12.0", {force_colour: "1"})
    assert code == 0, out


def test_the_version_check_stops_on_a_uv_failure_and_shows_uvs_error(tmp_path):
    """A failing `uv tool list` ends the step with uv's own message, not a mismatch on ''.

    A regular file where uv expects its tool directory makes uv fail for real.
    """
    not_a_directory = tmp_path / "tools"
    not_a_directory.write_text("")
    done = _run_check({**os.environ, "UV_TOOL_DIR": str(not_a_directory), "RUNTIME_VERSION": "0.12.0"})
    assert done.returncode != 0
    assert "Not a directory" in done.stderr
    assert "CI installed" not in done.stdout + done.stderr


def test_the_version_check_passes_for_the_wheel_built_from_this_checkout(tmp_path):
    """A wheel of this source, with its dependencies resolved, is the version the check reads."""
    import tomllib

    root = TEMPLATE.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    wheels = tmp_path / "wheels"
    # The contract wheel sits beside it so an exact `continuo-engine-contract` pin resolves
    # whether or not that version is on the index yet.
    for source in (root, root / "contract"):
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(wheels), str(source)],
            check=True, capture_output=True,
        )
    wheel = next(wheels.glob("continuo_python_runtime-*.whl"))
    code, out = _check_installed_runtime(tmp_path, ["--find-links", str(wheels), str(wheel)], version)
    assert code == 0, out
    code, out = _check_installed_runtime(tmp_path, ["--find-links", str(wheels), str(wheel)], "0.0.1")
    assert code == 1 and out.startswith(f"::error::CI installed '{version}'"), out


def test_readme_and_template_name_images_the_publisher_emits():
    """Engine-selection examples must name images the publisher actually pushes.

    publish-pypi.yml pushes ``continuo-python-runtime-<engine>:<tag>``: the engine is
    part of the image NAME and the tag is the bare version, which is what lets
    Continuo's chart pin ``<name>:vX.Y.Z@sha256:<digest>``. The version is read
    from the root pyproject so neither the README nor the template Dockerfile
    can drift past a version bump unnoticed.
    """
    import tomllib

    root = TEMPLATE.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    readme = (root / "README.md").read_text()
    dockerfile = (TEMPLATE / "Dockerfile").read_text()

    for engine in ("postgres", "trino"):
        assert f"continuo-python-runtime-{engine}:v{version}" in readme
    assert f"continuo-python-runtime-postgres:v{version}" in dockerfile


def _readme_pip_pin_step():
    """The replacement pin step the README's Shape 2 section gives, parsed from its fenced yaml block."""
    readme = (TEMPLATE / "README.md").read_text()
    blocks = readme.split("```yaml\n")[1:]
    block = next(b.split("```", 1)[0] for b in blocks if "GITHUB_ENV" in b)
    (step,) = yaml.safe_load(block)
    return step


def _pin_from_lock(tmp_path, lock: str):
    step = _readme_pip_pin_step()
    (tmp_path / "requirements.lock").write_text(lock)
    env_file = tmp_path / "github_env"
    env_file.write_text("")
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path, env={**os.environ, "GITHUB_ENV": str(env_file)},
        capture_output=True, text=True,
    )
    return done.returncode, env_file.read_text(), done.stdout


def test_the_readme_pip_pin_step_keeps_the_name_of_the_step_it_replaces():
    assert _readme_pip_pin_step()["name"] == "Pin the runtime to the image"


def test_the_readme_pip_pin_step_reads_a_hash_locked_runtime_pin(tmp_path):
    lock = (
        "# This file was autogenerated by uv\n"
        "continuo-postgres-adapter==0.12.0 \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n"
        "continuo-python-runtime==0.12.0 \\\n"
        "    --hash=sha256:" + "a" * 64 + " \\\n"
        "    --hash=sha256:" + "c" * 64 + "\n"
        "    # via continuo-postgres-adapter\n"
    )
    code, written, _ = _pin_from_lock(tmp_path, lock)
    assert code == 0 and written == "RUNTIME_VERSION=0.12.0\n"


def test_the_readme_pip_pin_step_reads_a_pin_without_hashes(tmp_path):
    code, written, _ = _pin_from_lock(tmp_path, "continuo-python-runtime==0.12.0\n")
    assert code == 0 and written == "RUNTIME_VERSION=0.12.0\n"


@pytest.mark.parametrize("lock", [
    "continuo-postgres-adapter==0.12.0 \\\n    --hash=sha256:" + "b" * 64 + "\n",
    "continuo-python-runtime>=0.12.0\n",
    "",
])
def test_the_readme_pip_pin_step_fails_clearly_without_a_runtime_pin(tmp_path, lock):
    code, written, out = _pin_from_lock(tmp_path, lock)
    assert code == 1 and written == ""
    assert out.startswith("::error::requirements.lock has no continuo-python-runtime==X.Y.Z line")
