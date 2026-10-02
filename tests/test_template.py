"""Test the domain-repo template."""

import json
import os
import shutil
import subprocess
import threading
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
