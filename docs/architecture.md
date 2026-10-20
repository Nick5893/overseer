# Architecture and lifecycle

## Roles

The operator is your normal frontier-model Codex session. It sees your prompt,
plans work, submits assignments through a shell tool, and decides what to accept.
The local worker is a separate Codex app-server process using the custom model
provider in your worker profile. Both use the Codex harness; the selected model
provider differs. The bridge is a Python supervisor and CLI, not another model.

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
3. The supervisor waits for the bridge-wide inference lock, then starts a
   dedicated Codex app-server over stdio. It flattens the selected TOML profile
   into per-process config overrides. No interactive Spark client is required.
4. It initializes the JSONL protocol and creates or resumes a worker thread with
   native delegation, apps, plugins, skill-list injection, and web search disabled.
   It checks the returned model/provider before starting inference; a mismatch
   fails instead of switching to the operator's provider.
5. `turn/start` sends the assignment. The harness calls the local Responses model,
   executes its tools under the project's chosen worker permission mode, and
   sends the tool outputs back to the local model.
6. The bridge logs received protocol events. Completed agent messages are
   collected in `report.txt`. `turn/completed` sets the task to completed, failed,
   or cancelled with any returned error.
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

By default, state is stored in `~/.local/state/spark-workers/projects/` under a
hash of the enabled project's absolute path:

```text
PROJECT_HASH/
  codex-home/             Private worker config, history, and caches
  tasks/JOB_ID/
    task.json             Assignment, status, model, IDs, timestamps, mode
    report.txt            Agent messages from the latest attempt
    report-attempt-N.txt  Earlier attempt's report
    events.jsonl          Received app-server responses and notifications
    app-server.log        Worker harness stderr
    supervisor.log        Supervisor diagnostics
    workspace/            Detached Git worktree
```

Each worker harness gets a genuine private `CODEX_HOME` in its child environment.
It starts from a copy of the operator's user configuration, while user
instructions, rules, and requirements remain available. Codex's automatic project
trust records and history are written to that private home. The worker profile
overrides model selection in that process. This leaves the normal operator's
saved defaults and original configuration untouched by the worker harness.

The bridge uses a machine-wide file lock within its state directory for one
active worker inference turn across projects. This does not throttle unrelated
interactive local-model clients or other software using the same GPU. It is not
a parallel GPU scheduler, and queue order is not guaranteed to be FIFO.

## Cancellation and recovery

- `cancel` creates a cancellation marker. A queued supervisor exits before
  inference; an active one requests `turn/interrupt`. Files and logs are retained.
- The turn time limit triggers interruption and records failure. The supervisor
  closes its dedicated harness after the turn or an unrecoverable error.
- Interactive approvals or questions cannot be answered by an unattended worker;
  the bridge returns an error so the model can report the blocker.
- If a supervisor dies, status compares its PID and Linux process start time and
  marks the task orphaned. It does not automatically restart it. Use `followup`
  to resume the saved thread/worktree after inspecting the cause.
- Stopping the operator does not automatically cancel detached workers. They can
  finish and retain their results for later collection.

Ignored files are absent from the snapshot unless already tracked; Git submodule
working trees and dependency environments are not copied. Full-access workers
can still access the host outside their workspace. Jobs and snapshots are kept
for review; there is no automatic retention/cleanup policy in this version.
