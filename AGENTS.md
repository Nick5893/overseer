# Working on Overseer

These instructions apply to this repository. Start with [README.md](README.md)
for user-facing behavior and [docs/architecture.md](docs/architecture.md) for the
job lifecycle. Keep the bridge small and understandable.

## Purpose and boundaries

Overseer connects an operator coding agent to separate worker harnesses through
ordinary shell commands. The operator plans, submits assignments, reads reports
and full diffs, requests corrections, and integrates reviewed work. Workers use
their configured model endpoints to edit and test in separate Git worktrees.

- Operator and worker harnesses are independent choices. Keep documentation and
  shared behavior harness agnostic. `overseer run` remains a Codex-only launcher.
- Workers support Codex, Pi, OMP, OpenCode, and Claude Code. Harnesses execute
  tools; model servers provide inference. Overseer does not serve model weights.
- Use Python's standard library and Git. Avoid adding frameworks, MCP services,
  brokers, or another daemon when a small function or adapter will do.
- Results return through CLI output. External workers are not native subagents
  and do not push completion messages into the operator's conversation.

## Source map

| Path | Responsibility |
| --- | --- |
| `spark_workers.py` | CLI, configuration, snapshots, job state, supervisor, inference locking, permissions, cancellation, and apply |
| `worker_harnesses.py` | Native harness protocols, identity checks, event normalization, completion, and conversation resume |
| `spark_proxy.py` | Optional Codex tool-payload compatibility proxy |
| `make_model_catalog.py` | Codex model catalog generation |
| `install.py` | Standard-library installer and command aliases |
| `templates/OPERATOR.md` | Shared operator workflow for any harness |
| `templates/SKILL.md`, `templates/agents/` | Explicit Codex project skill integration |
| `tests/` | Offline tests with disposable repositories and fake harnesses |
| `tools/live_smoke.py`, `tools/orchestrator_smoke.py` | Separate live-model integration checks |
| `docs/` | Architecture, setup, compatibility, troubleshooting, and validation evidence |

Historical filenames, storage locations, and aliases are compatibility surfaces.
Use Overseer in public-facing text; do not rename legacy paths or fields casually.

## Invariants to preserve

- Enable workers only through explicit project opt-in. Do not change global
  harness defaults or native delegation as a side effect.
- Never silently switch providers, models, or execution permissions after a
  failure. Non-Codex workers require explicit full access. Worktrees separate
  changes for review; they are not filesystem security sandboxes.
- Snapshot staged, unstaged, and non-ignored untracked files without changing
  the user's index. Workers implement in their worktree, not the original checkout.
- Keep job state and configuration backward compatible. Freeze worker selection
  and permissions per job; follow-ups resume its conversation and worktree.
- Serialize worker inference turns within a shared state directory. Do not
  promise FIFO ordering or control over unrelated clients of the model server.
- Preserve reports, events, and partial work on failure or cancellation. A model
  message alone does not establish successful harness completion.
- Apply only completed, unapplied jobs after conflict checks, without staging.
  Review is the operator's workflow, not an enforced bridge approval gate.
- Keep credentials, private profiles, sessions, and raw model logs out of Git.
  Prefer credential environment-variable names over saved secret values.

## Development and verification

Use Linux and Python 3.11+. Keep shared lifecycle behavior in the supervisor and
protocol differences in the harness adapters. Update relevant documentation when
commands, permissions, supported protocols, or user-visible behavior change.

For code changes, run the relevant tests and the repository checks:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
git diff --check
```

Add meaningful regression coverage for behavior changes. The default suite must
remain offline and require no GPU, model weights, or credentials. For installer
or lifecycle changes, cover a fresh temporary installation, including paths with
spaces. Use `install.py --prefix /temporary/path` instead of replacing a user's
installation while testing.

Run live checks only when the task calls for real inference, using disposable
fixtures and private evidence directories. Do not restart or reconfigure an
existing model service as a routine test step. See [docs/validation.md](docs/validation.md)
and [docs/orchestrators.md](docs/orchestrators.md) for commands and known limits.
Report the checks actually run and distinguish mocked protocol coverage from
live-model evidence; do not imply that every harness pairing was tested.

Use the user's normal delegation workflow unless they explicitly request
Overseer. When requested, follow [templates/OPERATOR.md](templates/OPERATOR.md).
Creating a commit or pushing changes should follow the user's task instructions.
