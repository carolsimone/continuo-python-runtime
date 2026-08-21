# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/carolsimone/continuo-python-runtime/security/advisories/new).
If you cannot use that form, email carolini.simone@gmail.com instead. Include enough detail to
reproduce the problem: affected component, version or commit, and the steps or
input that trigger it. A proof of concept helps but is not required.

What to expect:

| Stage | Target |
| --- | --- |
| Acknowledgement of your report | 3 working days |
| Initial assessment and severity | 10 working days |
| Fix or documented mitigation | depends on severity, communicated in the assessment |

You will be credited in the release notes for the fix unless you prefer to stay
anonymous. Please give us a chance to ship a fix before disclosing publicly.

## Supported versions

This project is pre-1.0. Only the latest release receives security fixes; there are
no backports to earlier tags.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Everything earlier | No |

## Scope

In scope — anything that lets someone cross a trust boundary this runtime is
supposed to enforce:

- Injection into generated SQL through a domain contract or script.
- A way for a `run(ctx)` script to reach beyond the closure the harness resolved
  for it, or to bypass `check_reads`/the sqlglot read gate.
- Leaking credentials through logs, exceptions, or CLI output.
- A contract that parses or hashes to something other than what it declares.

Out of scope:

- Findings that require an already-compromised engine image or Continuo
  deployment.
- Vulnerabilities in dependencies with no reachable call path from this code.
- Denial of service through resource exhaustion in a deployment you control.

## How we check our own code

These run in CI on every pull request and weekly on a schedule
(`.github/workflows/security.yml`). Each is also runnable locally through the
same script CI uses:

```bash
scripts/security-scan.sh          # everything
scripts/security-scan.sh secrets  # one scanner
```

| Check | Tool | Blocks a merge |
| --- | --- | --- |
| Committed secrets | `gitleaks` | Yes |
| Dependency CVEs | `trivy filesystem` | No — advisory |
| Dependency freshness | Dependabot, weekly grouped PRs | n/a |

The dependency scan reports HIGH and CRITICAL findings without failing the
build. Base-image and transitive CVEs are frequently unfixable upstream, so
gating on them would block every merge behind an ignore-list edit rather than
producing a fix. Dependabot is what actually resolves them.

## Credentials in local development

`tests/smoke/*/docker-compose.yml` bring up throwaway Postgres/Trino stacks
using each image's own documented default credentials (e.g. MinIO's
`minioadmin`/`minioadmin`), scoped to the compose network they define. No real
credentials are needed to develop or test this repository, and none should
ever be committed.
