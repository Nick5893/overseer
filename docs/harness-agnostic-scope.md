# Adding another harness

The first two adapters are Codex app-server and Pi RPC. Their implementation is
in `worker_harnesses.py`. The worker side is adaptable; the shipped operator
integration is still the explicit Codex project skill.

## The small contract

Each adapter implements:

| Method | Responsibility |
| --- | --- |
| `start(instructions)` | Initialize or resume; verify the selected model; return identity and opaque `resume_ref` |
| `prompt()` | Submit an assignment; return native turn metadata when applicable |
| `event(record)` | Convert relevant native records to report, tool, or finished events |
| `interrupt()` | Request cancellation without waiting synchronously for completion |

`Rpc` in `spark_workers.py` supplies continuous LF-delimited JSON reading,
request IDs, correlated replies, buffered interleaved events, raw event logging,
and child shutdown. Its two wire dialects are deliberately small. A harness with
a different transport should get a small dedicated client rather than forcing
all protocols into JSON-RPC.

The supervisor owns inference locking, deadlines, status, normalized progress,
and report persistence. Snapshot, diff, and apply do not depend on a harness.
Configuration resolves a named worker and saves a private copy at submission.
Follow-ups use that copy and the opaque resume reference. They cannot migrate a
conversation between harnesses.

## Before adding an adapter

1. Establish the harness's real unattended protocol and terminal condition.
   An acknowledgment is not completion. Pi is a concrete example: `agent_end`
   can precede automatic retry; `agent_settled` is the completion boundary.
2. Check model identity before inference and propagate provider errors. Never
   fall back to an unrelated provider or frontier model without the user asking.
3. Map permissions honestly. Reject unsupported modes before launch. A worktree
   does not make an unrestricted harness equivalent to a sandboxed one.
4. Isolate configuration and session state. Disable automatic plugins, MCP,
   interactive UI, and resource discovery where the harness supports it.
5. Add process-level tests for fresh install, actual edits, resume, cancellation,
   disconnects, and errors. Run a separate real-model coding smoke test.

## Deliberate limits

There is no adapter SDK, plugin registry, task-routing model, cross-harness
conversation converter, distributed scheduler, or new operator UI. The current
bridge needs ordinary local command execution and endpoint access. Another
operator could run the same CLI, but it would need its own planning/review
instructions; this repository only supplies the Codex skill.

External jobs remain visible through CLI results and saved logs, rather than
pretending to be native Codex subagents. MCP-free transport is useful when an
operator can run approved commands but cannot register MCP servers. It does not
bypass command restrictions or model-access policies.
