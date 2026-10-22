# Choosing an orchestrator

The operator is the harness that plans work, dispatches jobs, reviews results,
and integrates changes. Worker harnesses implement those assignments. Configure
the operator's model in its own CLI and workers with `overseer configure`; the
two choices are independent.

The operator needs shell execution and access to the same Linux project
filesystem, `overseer` executable, configuration, and state directory. Its shell
permissions must allow the bridge to start the selected worker. No MCP server or
operator adapter is needed.

Export credential variables in the shell that starts the operator, and ensure
its command tools forward the variables needed by the worker. Configure variable
names with `--api-key-env`; keep secret values out of prompts.

## Start a session

Enable the project and check the worker first:

```sh
cd /path/to/project
overseer doctor
overseer instructions
```

Add the output of `overseer instructions` to your harness's project instructions,
or pass it to a session explicitly. These launch examples preserve your harness's
normal model configuration and authentication:

```sh
pi --append-system-prompt "$(overseer instructions)" "Implement CSV export using Overseer."
omp --append-system-prompt "$(overseer instructions)" "Implement CSV export using Overseer."
claude --append-system-prompt "$(overseer instructions)" "Implement CSV export using Overseer."
```

For OpenCode, give the operator an initial instruction to read the CLI workflow:

```sh
opencode run "Run overseer instructions and follow that workflow to implement CSV export."
```

For Codex, use the supplied explicit skill or convenience launcher:

```sh
overseer run "Implement CSV export and verify it."
```

`overseer run` currently launches Codex. Other operators use their native launch
commands. `overseer instructions` only prints instructions; it does not start
inference or select an operator model. Worker permissions saved by `enable` are
separate from operator permissions. Full-access execution requires the user's
explicit choice; worktrees do not provide filesystem sandboxing.

## What comes back

The operator runs `submit`, repeated bounded `wait` calls, `result`, and `diff`.
Reports and diffs enter its conversation through shell tool output. It can resume
the same worker with `followup`, review the correction, and `apply` the result.
It then runs acceptance checks in the original project. External jobs do not
become native subagents or push completion notifications into the session.

Jobs freeze the selected worker's harness, configuration, and permissions. They
cannot change harness halfway through a conversation. Independent jobs may queue
together; dependent jobs must be submitted after prerequisite integration. One
worker turn runs at a time per state directory.

## Repeat the live integration check

The test uses two entries in a private Overseer worker registry as explicit
harness/model definitions: one for the operator, one for the worker. It starts a
real native CLI operator process with the reusable workflow. That model must
execute the actual bridge commands; the test does not drive the worker on its
behalf.

```sh
python3 tools/orchestrator_smoke.py \
  --config /absolute/path/to/private/registry.toml \
  --operator pi-local --worker codex-local \
  --full-access --output /path/to/new-private-evidence
```

Both entries must be configured before the test, and credential environment
variables must be present. This command permits unrestricted tools for both
harnesses in a disposable fixture; it does not change services or enable an
existing project. It installs a fresh copy of the bridge and retains private
configuration, sessions, tool traces, CLI calls, and test output.

The operator must submit one assignment, collect and review it, request a
correction in the same worker session, review again, apply, and run tests. The
verifier independently checks recorded CLI calls, job identity, both reviews,
same-session follow-up, actual code behavior, unchanged acceptance tests, original
files, and the staged index. `verification.json` is written only after these
checks pass. A model's success claim alone cannot pass the test.

The per-worker deadline defaults to 480 seconds (`--worker-timeout`); the overall
deadline defaults to 1200 seconds (`--timeout`). One timed-out correction may be
resumed in the same worker conversation. Other failures stop the check. The
report records actual attempt counts, recovered timeouts, and failed CLI calls;
a recovery must still pass the final result, diff, and acceptance checks.

If the local operator model ends a turn before completing the task, the test can
send a bounded continuation in the same native conversation (up to three turns).
`--operator-turns 1` requires completion in one operator turn. The report records
`operator_turns` and verifies operator session identity. The fixture controller
only supplies continuation prompts and checks evidence; the operator model must
still execute every worker submission, correction, review, and application.

See [validation evidence](validation.md) for tested combinations and limits.
