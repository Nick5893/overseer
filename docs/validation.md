# Validation

Checked on 2026-10-20 on Linux ARM64, using Codex 0.163.0, Pi 1.1.0, OMP 18.12.4, OpenCode 2.0.16, Claude Code
2.1.304, and the
configured local Qwen server.

## Automated suite

```sh
python3 -m unittest discover -s tests -v
```

```text
Ran 59 tests
OK
```

These tests use temporary Git repositories and fake stdio harness processes.
They need no GPU, model weights, network connection, or provider credentials.

| Area | Evidence |
| --- | --- |
| Installed Codex path | Clean prefix with spaces, generated profile, edit, report, same-thread follow-up, diff, apply |
| Installed Pi path | Clean prefix with spaces, edit, same-session follow-up, report, tool output, apply |
| Additional installed harnesses | OMP, OpenCode, and Claude edit/resume/apply; native error and exit checks, cancellation and timeout |
| CLI setup | Wizard flow, credential environment isolation, all-harness discovery, side-by-side install preserving existing commands |
| Rename compatibility | Overseer and legacy CLI aliases share saved jobs; explicit skill aliases, existing-project launch, and unrelated-skill overwrite protection |
| Working directory | Deliberately stale operator `PWD` replaced with actual worktree directory for every child |
| Configuration | Register both harnesses, retain Codex default, preserve Pi registration; freeze job settings across source/config changes |
| Permissions | Reject Pi in workspace-write before creating a worktree; no automatic full-access fallback |
| Identity | Reject Codex provider mismatch and Pi model/provider/API/endpoint mismatches before prompting |
| Pi lifecycle | Wait for settled after retry; reject errors, aborts, missing final response, and handled prompts |
| Recovery | Active cancellation, timeout, disconnect, orphan detection, retained work |
| Git boundaries | Dirty snapshot, index preservation, new files, apply conflicts, failed-job refusal |
| Transport | Interleaved events, EOF, literal Unicode separators, Codex freeform/namespaced tool translation |
| Operator workflow | Installed instructions without Codex; distinct operator/worker roles; explicit worktree paths in every worker harness |
| Imported OMP credentials | Pi environment references translated without persisting secrets; missing variables fail before inference |
| Evidence verifier | Help output and summary-only diffs cannot count as reviewing completed implementation work |

Additional checks:

```sh
python3 -m compileall -q .
git diff --check
```

## Live model checks

The harness paths below were driven from the frontier operator through the bridge.
These checks used actual local inference and file tools, not mocked model output.

| Worker | Initial task | Follow-up and integration |
| --- | --- | --- |
| Pi + local Qwen | Implement `clamp`; 5 tests passed | Same persistent session; invalid-bounds correction; 7 tests passed after reviewed apply |
| Codex + local Qwen | Implement `clamp`; 5 tests passed | Same thread; invalid-bounds correction; 8 tests passed after apply |
| OMP + local Qwen | Implement `clamp`; 5 tests passed | Same session; invalid-bounds tests; 11 tests passed after apply |
| Claude Code + local Qwen | Implement `clamp`; 5 tests passed | Same session; invalid-bounds correction; 12 tests passed after apply |
| OpenCode + local Qwen | Implement `clamp`; 5 tests passed | Same session; invalid-bounds correction; 7 tests passed after apply |

The original project stayed unchanged until application. The worker's original
test file and operator notes were preserved. The reusable smoke test passed for
the completed harness checks, verifying that a nonempty staged index and a private operator
config remained unchanged. Pi was also checked in an earlier manual round-trip.
Raw reports, tool events, snapshots, and test output were kept in private local
evidence directories, rather than committed with machine-specific configuration.

The existing Codex profile encountered an incompatible `apply_patch` tool payload;
the model recovered by editing through its command tool and completed the checks.
This is a reminder to validate the specific model/profile/server combination;
a passing bridge suite does not establish compatibility of every tool shape.

A separate terminal-level test also drove `overseer setup` through a real PTY and
verified its generated worker configuration. It did not enable a project.

## Live orchestrator checks

Checked on 2026-10-21 with the harness versions listed above. These checks used
**local Qwen for both operator and worker**, verifying that each CLI can drive
the bridge through its own native tools. They do not test every cloud provider
or login configuration.

Pi/OMP used explicit [Qwen reasoning-format metadata](local-backend.md#pi-direct-compatible-provider)
in their private model definitions. The checks kept the existing model service
and normal operator configuration unchanged.

| Operator CLI | Worker CLI | Final fixture tests | Operator turns |
| --- | --- | --- | --- |
| Pi | OMP | 9 passed | 1 |
| OMP | OpenCode | 6 passed | 1 |
| OpenCode | Claude Code | 7 passed | 1 |
| Claude Code | Codex | 8 passed | 1 |
| Codex | Pi | 7 passed | 1 |
| Codex | Codex | 6 passed | 1 |

These six pairings cover all five CLIs in both roles; this is not an exhaustive
live test of all 25 possible pairings. A separate Codex → Pi run also passed with
6 fixture tests after a continuation in the same operator conversation (2 turns).
Every passing run used the same worker conversation for its correction and kept
the original implementation untouched until application, with its staged index
and original tests preserved.

The operator model performed every submission, wait, result retrieval, full diff
review, correction, and application. The fixture controller prepared the project
and verified evidence; it did not issue those worker commands on the operator's
behalf. The verifier checks full patches before and after the correction; a
summary-only diff or a model's success statement cannot pass. Independent checks
rerun tests and verify invalid-bounds behavior after integration.

Sanitized [machine-readable results](orchestrator-results.json) include actual
attempt and operator-turn counts, CLI operations, and checks performed. Failed
CLI calls are recorded even when the operator corrected the syntax and completed
the task. Raw provider files, sessions, reports, and tool traces remain private.
The reusable [orchestrator test](orchestrators.md#repeat-the-live-integration-check)
can check another selected pairing or require completion in one operator turn.

The live checks led to three bridge improvements: importing Pi environment-key
references correctly for OMP; giving every worker its actual worktree root and
instructions to translate original-checkout paths; and supporting `--timeout`
on follow-ups. The operator workflow now explicitly requires refreshed reports
and full diffs after every correction and continued waiting until work is done.

## Repeat a live check

The reusable smoke test installs into a fresh prefix, creates a disposable Git
fixture with five acceptance tests, submits actual work, asks for a correction in
the same conversation, checks the bounded diff, applies it, and reruns tests.
It independently checks invalid-bounds behavior, original files, staged changes,
and operator configuration. It retains evidence for inspection.

```sh
# Codex worker: omit --full-access on a host with working workspace sandboxing.
python3 tools/live_smoke.py --harness codex \
  --profile /absolute/path/to/local-worker.config.toml --full-access

# Pi worker: explicit full access is required.
python3 tools/live_smoke.py --harness pi \
  --models /absolute/path/to/models.json \
  --provider local --model local-worker --full-access
```

Use `--binary /absolute/path/to/harness` when it is not on PATH, and
`--output /path/to/new-private-directory` to choose where evidence is retained.
The directory must not already exist. The command uses real inference, permits
the chosen worker tools to execute, and automatically applies only to its own
fixture project. It does not start, stop, or reconfigure model services, or enable
workers in an existing project.

Evidence includes `initial-tests.txt`, `reviewed.diff`, `final-tests.txt`,
`verification.json`, and private task state. Keep the directory private because
worker state contains a copy of the selected provider configuration. Inspect the
reports and raw tool output when diagnosing a failure. Test counts can vary when
a model adds extra regression cases; independent acceptance checks still run.

The same command supports `--harness omp`, `--harness opencode`, and
`--harness claude`. Use `--base-url` and `--model` for these paths, plus
`--api-key-env NAME` when needed. Claude requires a Messages endpoint, and
`--auth-mode bearer` for bearer-auth servers. These are real CLI harnesses against
the configured Qwen endpoint, not an Anthropic-supported non-Claude integration.

## Limits

These are small compatibility tests, not a claim about performance or accuracy
on large repositories. They do not establish filesystem sandboxing for Pi or
benchmark one harness against another. The bridge serializes its own workers;
unrelated clients using the same GPU are outside that lock. Only Linux is
supported, and worker results return through CLI tool output rather than native
agent notifications. Local operator models can end a turn early; the live
test records any bounded continuation in the same native conversation. Directly
launched harnesses retain their own conversation and execution behavior. Worktree
instructions are not filesystem confinement, and live checks do not establish
correctness for arbitrary coding tasks or every plugin/TUI configuration.
