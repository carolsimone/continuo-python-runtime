## What this changes

<!-- One or two sentences. Link the issue this addresses. -->

Closes #

## Why

<!-- What problem does this solve? -->

## How it was verified

<!-- The commands you ran and what they printed. "Tests pass" on its own is not enough. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`) — the `dco` check enforces this
- [ ] Tests added or updated for the behavior changed
- [ ] `uv run ruff check .` and `uv run mypy` (relevant packages) pass
- [ ] `scripts/security-scan.sh` passes (dependency or credential-adjacent changes)
