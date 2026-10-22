# Overseer operator workflow

Use this workflow only when the user explicitly requests Overseer. Keep planning,
review, integration, and verification in your current harness and model. Delegate
implementation through the `overseer` CLI, not your harness's native subagent tool.

1. Read the project and applicable instructions. Run `overseer doctor`. Use the
   configured worker and execution permissions; do not change providers or grant
   more access to work around a failure. Use a subcommand's `--help` only when
   its arguments are unclear. All project commands accept `--project /absolute/project/path`.
2. Plan self-contained assignments with relevant files, boundaries, acceptance
   checks, and an expected report. Write each assignment to a file and run
   `overseer submit --name NAME --prompt-file PATH`. Record the returned job ID.
   Use project-relative file paths in assignments. Workers start in a different
   worktree; never tell them to edit the original checkout's absolute paths.
   Independent jobs may queue together. Integrate prerequisites before submitting
   dependent work; a job snapshots the project at submission time.
3. Poll `overseer wait JOB_ID --seconds 30` until the job is terminal. A running
   or queued result is not completion; repeat the wait. `overseer status` lists
   jobs. Work continues when a wait returns. One worker turn runs at a time within
   a shared state directory. Do not end the task merely after starting a job or
   follow-up; keep waiting, collecting, reviewing, and integrating until the
   requested work is complete. Keep the user informed during long runs.
4. Read `overseer result JOB_ID` and `overseer diff JOB_ID`. Check actual files and
   recorded test output; a worker's final claim alone is not proof. Results return
   through your shell tool. Jobs do not send native subagent notifications.
5. For corrections, write a new assignment and run
   `overseer followup JOB_ID --prompt-file PATH`, then wait and review again. This
   resumes the same worker conversation and worktree. A follow-up needs a finished
   job. After every follow-up, fetch both `overseer result JOB_ID` and the full
   `overseer diff JOB_ID` again, including after a timeout recovery. A diff read
   before the latest follow-up does not review its new changes. Report failures
   honestly; do not silently fall back to another provider.
6. Apply reviewed completed work with `overseer apply JOB_ID`. Run the acceptance
   checks in the original project and report real results. Do not reset, clean,
   overwrite conflicts, or change the user's index to force integration. Applied
   jobs cannot be followed up; submit new work against the updated project.

Workers edit in separate Git worktrees. Do not implement their assignments in the
original project yourself. Worktrees isolate changes for review, not filesystem
access. Keep credentials, private configuration, sessions, and logs private. Use
`overseer cancel JOB_ID` to interrupt unwanted work; partial work remains available.
