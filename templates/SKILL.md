---
name: spark-workers
description: Explicitly delegate implementation to configured local-model workers using Codex, Pi, OMP, OpenCode, or Claude Code while the frontier Codex operator plans, reviews, and integrates. Only for projects enabled with foundry enable.
---

Use this workflow only when the user explicitly invokes `$spark-workers` or explicitly requests Foundry/local Spark/Qwen workers. Enabling the project does not change normal Codex delegation. This invocation covers the requested task and its corrections, not unrelated future tasks.

You are the operator. Inspect the project, form a concise plan, identify dependencies and acceptance criteria, then delegate implementation through the `foundry` command. Do not substitute native `spawn_agent` calls for local workers: native agents inherit your provider. Keep planning, review, integration, and final verification in this Codex session.

Run `foundry doctor` and resolve local setup errors before submitting work. Its output identifies the selected worker, harness, model, and permissions. Use the project's default worker unless the user requests a specific configured worker; then use `doctor --worker NAME` and `submit --worker NAME`. Do not automatically enable a different project. All commands accept `--project /absolute/project/path` after the subcommand; use this when your current directory differs from the enabled repo.

Submit a self-contained assignment using a file or stdin, so shell quoting does not change code or instructions:

```sh
foundry submit --name implement --prompt-file /tmp/spark-assignment.txt
foundry status
foundry wait JOB_ID --seconds 30
foundry result JOB_ID
foundry diff JOB_ID --stat
foundry diff JOB_ID
```

Include the intended behavior, relevant files, boundaries, acceptance checks, and expected report in each assignment. Workers start from a private snapshot of tracked and non-ignored untracked files, including existing uncommitted changes. Ignored files (dependencies, secrets, build artifacts) are not copied. They share one global inference slot and run in isolated worktrees. Give workers small, concrete tasks; avoid repeating the entire operator conversation.

Review the actual diff and test evidence. For corrections, write a follow-up assignment and use `foundry followup JOB_ID --prompt-file /tmp/spark-correction.txt`; this resumes the same harness conversation and worktree using the job's saved configuration and permissions. A follow-up requires a finished task. If inference fails, report the failure; do not silently use an OpenAI worker or another harness instead.

Apply only reviewed completed work with `foundry apply JOB_ID`, then run the relevant checks in the original project. Application checks for conflicts and preserves the user's index and pre-existing changes. If it conflicts, inspect and integrate deliberately; do not reset or overwrite files to force application. After applying a task, submit new work against the updated project rather than following up the applied task.

Submit dependent tasks after integrating their prerequisites so they see the updated code. Independent tasks may be queued together. Poll with waits of at most 30 seconds and keep the user informed. `foundry cancel JOB_ID` interrupts the worker; partial work and logs remain available. `foundry result JOB_ID` includes the report, workspace path, events, and error details. Completion means the local turn finished; independently verify the user's acceptance criteria.

Worker execution mode is saved in the project's configuration and frozen at job submission. New projects default to workspace-write sandboxing. Harnesses other than Codex have no workspace sandbox supported by this bridge and require an explicit full-access project choice; never select full access automatically to make another harness work. Full access allows commands outside the worktree. Worktrees isolate edits for review, but are not a filesystem security boundary. Honor the selected mode; do not silently change permissions. Do not run workers recursively or change the operator's model/provider, global skills, or native agent configuration. External workers report through CLI results, not the native subagents panel. The result includes raw harness events and normalized progress log paths for inspecting tool output.
