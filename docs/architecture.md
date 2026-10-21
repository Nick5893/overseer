# Architecture and lifecycle

## Roles

The operator is your chosen coding harness with a frontier model, shell execution,
and access to the shared project filesystem and Overseer state. It sees your prompt,
plans work, submits assignments through a shell tool, and decides what to accept.
The local worker is a separate Codex, Pi, OMP, OpenCode, or Claude Code process
using a configured model provider. The bridge is a Python supervisor and CLI.
`worker_harnesses.py` contains the worker adapters; the shared supervisor
owns jobs, inference locking, worktrees, cancellation deadlines, and application.
No MCP server or SDK is needed between the operator and bridge.

Operator and worker harnesses are independent choices. Operators use the same CLI
contract to submit work and collect results, without an operator-specific adapter.
The repository supplies a Codex project skill and Codex-only `overseer run`
launcher; other operators use their own launch command and planning/review
instructions. See the [quick start](../README.md#3-open-your-chosen-orchestrator-and-give-it-work).

The supported topology is a shared Linux filesystem for operator, bridge, Git
checkout, and worker tools. Inference can be local or accessible through a
configured endpoint. This does not implement remote filesystem synchronization,
multi-host worker scheduling, or a remote execution cluster.

## A task's path through the system

1. `submit` requires project opt-in and a committed Git HEAD. It captures the
   current project into a private Git snapshot using a temporary index. Staged,
   unstaged, and non-ignored untracked files become the worker's input without
   changing the user's index or committing onto their branch.
2. A detached worktree is created from that snapshot. The bridge saves task state
   and launches a detached supervisor, so the operator's shell can return.
3. At submission, the bridge freezes the selected worker configuration and
   execution mode. Codex profiles and Pi/OMP provider definitions are copied into
   private task state; OpenCode and Claude configs are generated from saved settings. Binary paths are saved, but binary contents,
   referenced catalogs, credential helpers, and environment values are not locked.
4. The supervisor waits for the bridge-wide inference lock and starts a dedicated
   harness over stdio. Codex uses `initialize` and `thread/start` or `thread/resume`;
   Pi and OMP use `get_state` with a persistent session file. These RPC adapters
   verify model identity before submitting assignments. OpenCode and Claude use
   native headless JSON streams with explicit endpoint/model flags and private
   configuration. Claude also checks the model/session reported by its init event.
   OpenCode has no equivalent pre-inference identity handshake in its CLI stream.
5. The harness calls the model and executes read/edit/test tools. Codex uses the
   saved sandbox mode. Other harnesses require explicit `danger-full-access`;
   this bridge does not provide equivalent workspace sandboxing for them.
6. Raw received records are saved to `events.jsonl`. Adapter events become common
   `identity`, `report`, `tool`, and `finished` records in `progress.jsonl`, tagged with harness,
   attempt, and timestamp. Reports go to `report.txt`. Codex completes on the
   matching `turn/completed`; Pi completes on `agent_settled`, checking the final
   assistant stop reason. Pi's `agent_end` alone is insufficient because retries
   or compaction may continue afterward. OMP waits for its correlated prompt
   result and session quiescence. OpenCode and Claude must exit cleanly as well
   as satisfy their result contracts; acknowledgments and intermediate reports
   do not establish success. See [harness details](harnesses.md).
7. The operator uses `wait`/`status`, then `result` and `diff`. These command outputs
   are ordinary tool results that enter the operator's conversation. The worker
   does not share memory with the operator or push an automatic native subagent
   completion message into that conversation.
8. The operator can `followup` before integration. This resumes the saved local
   conversation and existing worktree. After review, `apply` generates the worker
   delta from its input snapshot, checks it against the original checkout, then
   applies it without staging. Applied tasks cannot be followed up; submit new
   work against the updated checkout instead.

Completion records a finished model turn. Test claims are worker self-reports
until the operator checks the recorded output, code, and acceptance criteria.
Review is guided by the skill; reading a diff is not enforced by an approval gate
in the bridge. Patch application requires a completed, not-yet-applied task and
a successful Git conflict check.

## State and persistence

State is stored in Overseer's state directory, which can be overridden with
`OVERSEER_STATE_DIR`. Its `projects/` directory contains a hash of each enabled
project's absolute path:

```text
PROJECT_HASH/
  codex-home/             Private worker config, history, and caches
  tasks/JOB_ID/
    task.json             Assignment, status, model, IDs, timestamps, mode
    report.txt            Agent messages from the latest attempt
    report-attempt-N.txt  Earlier attempt's report
    worker.config.toml    Frozen Codex worker profile (Codex jobs)
    pi-home/models.json   Selected provider/model (Pi jobs)
    omp-home/             Private OMP models.yml and discovery overlay
    opencode-home/        Private runtime config and XDG directories
    claude-home/          Private Claude sessions/config
    sessions/             Persistent Pi conversation (Pi jobs)
    progress.jsonl        Common report/tool/finished records across attempts
    events.jsonl          Raw received harness records across attempts
    app-server.log        Worker harness stderr
    supervisor.log        Supervisor diagnostics
    workspace/            Detached Git worktree
```

Each Codex worker harness gets a genuine private `CODEX_HOME` in its child
environment. It starts from a copy of the operator's user configuration, while user
instructions, rules, and requirements remain available. Codex's automatic project
trust records and history are written to that private home. The worker profile
overrides model selection in that process. This leaves the normal operator's
saved defaults and original configuration untouched by the worker harness.

Pi receives a private `PI_CODING_AGENT_DIR` per job. Only its selected provider
and model are copied from `models.json`; interactive `auth.json` is not copied.
Extensions, MCP, automatic skills/templates/context files, themes, project resource
approval, and startup network checks are disabled. The worker instructions ask it
to read applicable `AGENTS.md` files itself. Provider requests still use the network.
Credentials may use environment references in the private model configuration.

OMP also gets a private agent directory and a restricted configuration overlay.
OpenCode uses a private standalone server and job-local XDG directories. Claude
uses a private `CLAUDE_CONFIG_DIR`, bare mode, an explicit tool allowlist, and
empty strict MCP configuration. Secrets supplied through `--api-key-env` resolve
in child environments, while only their variable names enter the worker registry.
Claude endpoints can select an explicit bearer-header mode. See
[harness support](harnesses.md) for permission and API limitations.

Version-2 jobs store `worker`, `harness`, `worker_config`, and an opaque
`resume_ref`. Codex keeps the legacy `thread_id` field too. Legacy job records
without harness metadata are treated as Codex jobs and frozen when resumed.
Project marker version 1 and the historical single-worker settings remain valid.
Follow-up uses saved job permissions even if the project default changes; cancel
an existing job if its previously authorized execution mode should no longer run.

The bridge uses a machine-wide file lock within its state directory for one
active worker inference turn across projects. This does not throttle unrelated
interactive local-model clients or other software using the same GPU. It is not
a parallel GPU scheduler, and queue order is not guaranteed to be FIFO.

## Cancellation and recovery

- `cancel` creates a cancellation marker. A queued supervisor exits before
  inference; an active one requests Codex `turn/interrupt` or Pi `clear_queue` and
  `abort`. OMP uses `abort`; headless OpenCode/Claude children receive SIGINT.
  Files and logs are retained.
- The turn time limit triggers interruption and records failure. The supervisor
  closes its dedicated harness after the turn or an unrecoverable error.
- Interactive approvals or questions cannot be answered by an unattended worker;
  Codex receives an error response, while unexpected Pi extension UI fails the job.
- If a supervisor dies, status compares its PID and Linux process start time and
  marks the task orphaned. It does not automatically restart it. Use `followup`
  to resume the saved thread/worktree after inspecting the cause.
- Stopping the operator does not automatically cancel detached workers. They can
  finish and retain their results for later collection.

Ignored files are absent from the snapshot unless already tracked; Git submodule
working trees and dependency environments are not copied. Full-access workers
can still access the host outside their workspace. Jobs and snapshots are kept
for review; there is no automatic retention/cleanup policy in this version.
