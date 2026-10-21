# Connecting a local model

## Choose the worker protocol

Pi, OMP, and OpenCode can connect directly to a Chat Completions endpoint. Codex
requires Responses; Claude Code requires an Anthropic Messages-compatible endpoint.
See [harness setup](harnesses.md) for the additional harnesses.
All paths need working tool calling, result continuation, and the context size you
advertise. A health endpoint alone does not prove compatibility.

### Pi: direct compatible provider

Copy [the Pi model example](../examples/pi-models.json) into a private location,
then change its URL, model ID, and context limit to match your backend:

```sh
overseer configure --worker pi-local --harness pi \
  --models /absolute/path/to/models.json --provider local --model local-worker
```

The example dummy key is for a local server that ignores authentication. For an
authenticated endpoint, prefer `"apiKey": "$LOCAL_MODEL_API_KEY"` in your private
file and provide that environment variable when starting the bridge. Only the
selected custom provider and model are copied into private job state; Pi login
credentials and extensions are not imported. Credential commands in model config
remain executable code and should be trusted. See the official
[Pi model configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md).

Pi workers require explicit full-access project permissions. They use the direct
provider protocol and do not require `overseer-adapter` or `overseer-catalog`.

## What the Codex endpoint must support

The worker harness speaks the Responses API over HTTP/SSE. The local serving
stack must support the chosen model's tool calling and parser, streamed function
call arguments, tool-result continuation, and the context size you advertise.
Serving a model that answers text through `/v1/chat/completions` does not establish
compatibility with this harness.

Overseer deliberately does not download model weights or install a model
server. Start your existing vLLM or other compatible serving stack according to
that stack's documentation. Use a coding/tool-capable local model and its actual
chat/tool/reasoning configuration. The profile's `model` must equal an alias the
server accepts, for example a served alias you choose as `local-worker`.

The reference topology uses:

```text
Codex worker harness -> adapter 127.0.0.1:18991 -> server 127.0.0.1:18888
```

Run the adapter with:

```sh
overseer-adapter --port 18991 --upstream-port 18888
```

Its listener and upstream both use loopback. Keep it local, or use an appropriate
authenticated tunnel if your server runs elsewhere. This is not a public gateway.
The adapter forwards authentication headers supplied by the worker profile,
avoids prompt/credential logging, and cancels upstream work on client disconnect.

## Tool translation

Codex can offer freeform tools and tools in namespaces. Some local Responses
implementations accept ordinary function tools but cannot handle those Codex
shapes directly. The adapter wraps freeform input in JSON, flattens namespace
names, and restores the original shapes in responses and streamed events.

For example, a freeform `exec` call becomes a function whose arguments contain an
`input` string. That string still contains JavaScript for Codex's tool runtime;
it is not interpreted as a shell command by the adapter. The Codex harness owns
tool execution and permissions.

If your server already supports the required Codex tool shapes, point the profile
directly at its Responses base URL and omit the adapter. To generate a direct
profile:

```sh
overseer-catalog --model local-worker --context-window 65536 \
  --base-url http://127.0.0.1:18888/v1 \
  --output /path/to/new-models.json \
  --profile-output /path/to/new-worker.config.toml
```

## Reuse an existing working local profile

```sh
overseer configure --profile /path/to/your/spark.config.toml
```

Existing command-backed provider authentication can remain in that profile. It
must point to helpers that exist on the receiving machine. Do not copy a profile
that embeds another person's home paths, secret values, or token files and assume
it is portable. The repository ships neither personal auth helpers nor real
profiles/catalogs from the original machine.

For environment-based keys, add this under the custom provider in your private
worker profile:

```toml
env_key = "LOCAL_MODEL_API_KEY"
```

Set that environment variable in the shell that starts the operator/bridge using
your preferred secret management. Do not put the value in tracked example files.
Worker supervisors inherit the submitting process's environment. The frontier
operator's own Codex authentication remains separate.

## Catalogs and context

`overseer-catalog` uses `codex debug models --bundled`, taking harness instructions
and metadata from the installed Codex version and generating a local alias. It
does not query an OpenAI model or copy this machine's personal catalog. The
generated profile defaults to a `medium` reasoning setting; adapt it to what your
backend supports. Catalog metadata does not give a model capabilities it lacks.

The example 65,536-token context is illustrative. If you configure the backend
with a smaller actual maximum, use that value in catalog generation. Larger
contexts can increase prefill latency and memory consumption. A small smoke task
is the useful compatibility test; a health endpoint alone is not.

The examples target the Codex 0.163.0 protocol used by the original integration.
If generation or thread startup fails on another version, check that version's
`codex app-server generate-json-schema` output and the official
[app-server documentation](https://learn.chatgpt.com/docs/app-server).

## Small end-to-end check

Use a disposable project before a substantial assignment:

```sh
mkdir /tmp/overseer-smoke
cd /tmp/overseer-smoke
git init
printf '# Overseer smoke test\n' > README.md
git add README.md
git commit -m 'Initialize smoke test'
overseer enable
overseer doctor
overseer run "Create add.py with an add(a, b) function and a small unittest. Run the test, review the worker diff, and apply the verified change."
```

This requires your ordinary Git identity and an authenticated frontier operator.
It performs real inference on both the operator and local worker models. If the
host cannot run Codex's sandbox, make the explicit worker/operator permission
choices described in the main README first; the bridge will not silently change
them. Check `overseer result JOB_ID` and the actual files/tests rather than treating
an endpoint health response as a successful integration test.
