# Harness support

The operator remains Codex. `--harness` selects the implementation worker's tool
runtime; `--model` selects its model. Jobs still return through `overseer result`
and `overseer diff`, regardless of harness. No MCP transport or agent SDK is needed
by the bridge.

| Harness | Transport | Completion boundary | Resume reference |
| --- | --- | --- | --- |
| Codex | app-server JSONL | Matching `turn/completed` | Thread ID |
| Pi | RPC JSONL | `agent_settled` and successful final assistant message | Session file |
| OMP | RPC JSONL | Correlated `prompt_result` with `sessionSettled`, or subsequent `session_settled` | Session file |
| OpenCode | `run --standalone --format json` | Clean process exit, report, no reported error; validate explicit finish reason if present | Session ID |
| Claude Code | Bare print mode, stream JSON | Successful non-error result, verified model/session, and clean process exit | Session UUID |

All harnesses except Codex require an explicitly selected project permission mode
of `danger-full-access`. The bridge does not claim their permission controls are
equivalent to Codex's workspace sandbox. Git worktrees isolate changes for review,
while tools in full-access processes can reach the host outside the worktree.

## Configure and select

```sh
overseer setup
overseer workers

# Or configure directly; install each harness separately.
overseer configure --harness omp --worker omp-local \
  --base-url http://127.0.0.1:18888/v1 --model local-worker
overseer configure --harness opencode --worker code-local \
  --base-url http://127.0.0.1:18888/v1 --model local-worker
overseer configure --harness claude --worker claude-local \
  --base-url http://127.0.0.1:18888 --model local-worker

# Inside your project:
overseer enable --worker code-local --sandbox danger-full-access
overseer doctor
overseer run "Implement and verify the feature"
```

Add `--api-key-env NAME` for authentication and provide that environment variable
when invoking Overseer. Secrets are resolved in the child environment, not placed
in command arguments or the worker registry. Private native configuration files
can still contain credential references or, when you supply an existing models
file, its literal credentials. Keep task state private.

Use `--binary PATH` when an executable is not on PATH. A missing harness can be
registered before installation; `workers` and `doctor` report its availability.
The interactive wizard writes only Overseer configuration. Project opt-in remains
a separate explicit command, and ordinary Codex delegation remains unchanged.

## OMP / Oh My Pi

OMP is Pi-derived, but its current protocol is different. Pi's `agent_settled`
does not complete an OMP job. Overseer correlates `prompt_result` with the prompt
ID and waits for any background work to settle before closing the harness.

OMP workers use a private agent directory. Overseer writes the selected provider
as JSON-compatible YAML in `models.yml`; no Python YAML dependency is needed.
`--models FILE` accepts a JSON provider definition like the Pi example, or
`--base-url` generates one. General YAML import is not supported by the bridge.
Native and foreign discovery sources are disabled through the job overlay;
extensions, skills, rules, LSP, automatic titles, and prewalk are disabled. Built-in
tools are limited to file operations and bash. Model roles are pinned to the
configured worker model; native task/subagent tools are not exposed.

## OpenCode

Overseer uses standalone mode so worker configuration and credentials do not flow
through an existing background service. A dedicated primary agent receives the
worker instructions. Its provider, main model, and small model are explicit;
permissions deny delegation, interactive questions, and unrelated tools.

XDG configuration, data, state, and cache directories are private to the job.
The bridge sets both the process working directory and `PWD` to the worktree;
OpenCode's CLI gives `PWD` precedence when choosing its execution directory.
Project config and ambient Claude configuration are disabled. The runtime config
has no plugins or MCP servers, disables sharing and updates, and allows only the
configured provider. Managed settings can still apply.

OpenCode 2.0.16 can emit only reconciled text and tool records without a final
`step_finish`. A clean CLI exit is therefore part of the completion contract.
An error record or nonzero exit fails the job even if it printed a report.
OpenCode selects the model using explicit native config and command flags; its
CLI stream does not provide the same pre-inference identity handshake as Codex.

## Claude Code

Claude workers require a Messages API endpoint. A Chat Completions-only endpoint
will not work, and Overseer does not implement a protocol translation gateway.
Use a full model ID accepted by the endpoint, rather than an alias that silently
resolves to another model. Claude's reported model and session must match the job.

Bare mode avoids interactive account credentials and ambient integrations.
`--api-key-env NAME` supplies the key to the private child environment. For a
server requiring HTTP bearer authentication, add `--auth-mode bearer`; the bridge
sets Anthropic's documented custom Authorization header. Otherwise the native
API-key header is used. Worker effort is `medium` for compatible endpoints.

The bridge does not auto-select a fallback model. A CLI acknowledgment, report,
or `subtype: success` with `is_error: true` is not success. Terminal errors,
nonzero exits, and model/session mismatches fail the job. Native model/session
verification occurs when the init record arrives, rather than through a separate
pre-inference RPC handshake. Tool/API compatibility depends on the gateway.
Anthropic does not support routing to non-Claude models, even if a compatible
server can serve them.

## References

- [OMP repository](https://github.com/can1357/oh-my-pi)
- [OMP RPC](https://github.com/can1357/oh-my-pi/blob/main/docs/rpc.md)
- [OpenCode CLI](https://dev.opencode.ai/v2/docs/cli/commands/)
- [OpenCode config](https://opencode.ai/docs/config/)
- [Claude programmatic mode](https://code.claude.com/docs/en/headless)
- [Claude gateways](https://code.claude.com/docs/en/llm-gateway)
