# Troubleshooting

| Symptom | What to check |
| --- | --- |
| `overseer` is not found | Put the install prefix's `bin` directory on PATH. |
| Installer refuses a command link | Use a separate prefix for a preview, or `--force` only to intentionally replace that link. The installer prechecks all links before copying. |
| Unknown worker | Register it with `configure --worker NAME`, then select it with `enable --worker NAME` or `submit --worker NAME`. |
| Pi workspace sandbox error | Pi requires explicit `enable --sandbox danger-full-access`; choose Codex if you require its workspace sandbox. |
| Pi fails before inference | Check `api`, `baseUrl`, provider, and model ID in the custom models file; the returned identity must match. |
| Pi authentication missing | Its private home does not import interactive login credentials. Use an environment key reference in the selected provider config. |
| Follow-up keeps an old model | Expected: jobs freeze configuration and permissions. Submit a new job to use a changed worker. |
| No workers or a missing executable | Run `overseer setup`, then `overseer workers`; install the chosen harness separately or use `--binary PATH`. |
| Claude repeats authentication errors | Check its Messages endpoint and `--api-key-env NAME`. For bearer-auth servers, use `--auth-mode bearer`. |
| Claude reports an unsupported reasoning effort | The adapter uses `medium`; verify the endpoint accepts that effort and supports Anthropic request fields. |
| OpenCode prints text but job fails | Inspect its error records and process exit; a report alone is insufficient. Check standalone mode and provider setup. |
| Missing worker profile | Run `overseer configure --profile /absolute/path/to/profile.config.toml`. |
| Project is not enabled | Run `overseer enable` in that Git project, then open a fresh Codex session to discover `$overseer`. |
| No initial commit | Commit the project's initial files before submitting a worker task. |
| Health check fails | Start the local backend and adapter; inspect configured ports. Set `configure --health-url URL`, or `--health-url ''` if the server has no health route. |
| HTTP 404 on `/v1/responses` | The backend may only implement Chat Completions. The adapter does not add a Responses implementation. |
| Model/provider mismatch | Check the worker profile and served alias. The bridge deliberately refuses inference with a different returned identity. |
| Unknown catalog fields or protocol methods | The installed Codex version differs from the tested protocol. Inspect generated schemas and regenerate the local catalog from the installed binary. |
| Freeform/namespaced tool error | Use the included adapter if the backend supports only ordinary function tools, and ensure the model emits valid arguments. |
| Tool calls never execute | Verify the model server's tool parser and chat template; inspect the task event log and harness stderr. A text-only model response is insufficient. |
| Bubblewrap/user namespace failure | Fix the host sandbox/container setup, or explicitly choose full-access worker and operator modes if appropriate. There is no automatic escalation. |
| Worker asks an interactive question | Unattended workers cannot answer it. Give a complete assignment, or resolve the requirement in the operator and follow up. |
| Task stays queued | Another bridge worker holds the global inference slot. Other local-model clients are outside this queue. |
| Task is orphaned after a crash/reboot | Inspect logs, then use `followup` to resume the saved thread/worktree. Supervisors do not automatically restart after reboot. |
| Patch application conflicts | Original files changed since the snapshot or another task overlaps. Inspect the diff and integrate deliberately; do not force an overwrite. |
| Newly installed dependency is missing | Ignored environments and dependencies are not copied into worktrees. Give the worker appropriate setup instructions or have the operator arrange the environment. |
| `$overseer` is absent from normal prompt metadata | Expected: implicit invocation is disabled. It should be available in the skill selector in enabled projects. |

Use `overseer result JOB_ID --project /path/to/project` to find `report.txt`,
`events.jsonl`, `progress.jsonl`, `app-server.log`, and the private worktree. Review the terminal
status as well as the report: completed model output is not proof that tests
passed or that every acceptance criterion was met.
