#!/usr/bin/env python3
"""Overseer: frontier planning, local execution, explicit project opt-in."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
import urllib.parse
import uuid

from worker_harnesses import Error, CodexWorker, PiWorker, ADAPTERS

APP = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("OVERSEER_CONFIG", os.environ.get("SPARK_WORKERS_CONFIG", Path.home() / ".config/frontier-foundry/config.toml"))).expanduser()
try:
    SETTINGS = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.is_file() else {}
except (OSError, tomllib.TOMLDecodeError) as exc:
    raise SystemExit(f"Cannot read Overseer configuration: {type(exc).__name__}")
STATE = Path(os.environ.get("OVERSEER_STATE_DIR", os.environ.get("SPARK_WORKERS_STATE_DIR", SETTINGS.get("state_dir", str(Path.home() / ".local/state/spark-workers"))))).expanduser()
CODEX = Path(os.environ.get("OVERSEER_CODEX", os.environ.get("SPARK_WORKERS_CODEX", SETTINGS.get("codex_binary", shutil.which("codex") or str(Path.home() / ".local/bin/codex"))))).expanduser()
user_codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
default_profile = user_codex_home / "spark.config.toml"
if not default_profile.is_file():
    default_profile = user_codex_home / "local-worker.config.toml"
PROFILE = Path(os.environ.get("OVERSEER_PROFILE", os.environ.get("SPARK_WORKERS_PROFILE", SETTINGS.get("worker_profile", str(default_profile))))).expanduser()
MARKER = Path(".codex/spark-workers.json")
SKILL = Path(".agents/skills/overseer")
LEGACY_SKILL = Path(".agents/skills/spark-workers")
SKILL_FILES = ("SKILL.md", "agents/openai.yaml", "assets/overseer.jpg")
TERMINAL = {"completed", "failed", "cancelled", "orphaned"}
WORKER_INSTRUCTIONS = """You are a local implementation worker managed by an Overseer operator.
Work only on the supplied assignment in your current isolated Git worktree.
Read applicable AGENTS.md instructions. Implement code and run relevant checks.
Do not delegate, launch other agents, invoke overseer, foundry, or spark-workers, change the original
project, or manage live services. Do not commit, reset, clean, or push. Leave all
edits in the worktree. Avoid unrelated changes. Report changed files, commands
actually run and their outcomes, and any unresolved issue. If blocked, report the
concrete error rather than claiming success. Use the provided tools to make real
edits; a suggested patch in your final answer alone does not complete the task.
"""


def model_profile(path=None):
    path = Path(path or PROFILE)
    if not path.is_file():
        raise Error(f"Missing worker profile: {path}. Run overseer configure --profile /path/to/worker.config.toml.")
    profile = tomllib.loads(path.read_text())
    model, provider = profile.get("model"), profile.get("model_provider")
    spec = profile.get("model_providers", {}).get(provider, {})
    if not model or not provider or provider == "openai" or not spec.get("base_url"):
        raise Error("Worker profile must specify model, a custom model_provider, and that provider's base_url.")
    if spec.get("wire_api") != "responses":
        raise Error("The local provider must use wire_api = 'responses'. Chat Completions alone is not sufficient.")
    return profile


def select_worker(name=None, settings=None):
    settings = SETTINGS if settings is None else settings
    name = name or settings.get("default_worker", "codex-local")
    spec = dict(settings.get("workers", {}).get(name, {}))
    if not spec:
        if name != "codex-local":
            raise Error(f"Unknown worker {name!r}. Register it with overseer configure --worker {name}.")
        spec = {"harness": "codex", "profile": str(PROFILE), "binary": str(CODEX)}
        if "health_url" in settings:
            spec["health_url"] = settings["health_url"]
    spec["name"] = name
    if name == "codex-local" and "health_url" in settings:
        spec.setdefault("health_url", settings["health_url"])
    if spec.get("harness") == "codex":
        # Historical environment overrides continue to select the default Codex worker.
        if name == "codex-local" and settings is SETTINGS:
            spec.update(profile=str(PROFILE), binary=str(CODEX))
        profile = model_profile(Path(spec["profile"]).expanduser())
        provider = profile["model_provider"]
        spec.update(model=profile["model"], provider=provider,
                    base_url=profile["model_providers"][provider]["base_url"])
        spec.setdefault("binary", str(CODEX))
    elif spec.get("harness") in {"pi", "omp"}:
        config = json.loads(Path(spec["models"]).expanduser().read_text())
        provider = config.get("providers", {}).get(spec["provider"], {})
        model = next((m for m in provider.get("models", []) if m.get("id") == spec["model"]), None)
        if model is None:
            raise Error("Pi models.json must explicitly define the selected custom provider and model.")
        spec.update(api=model.get("api", provider.get("api")),
                    base_url=model.get("baseUrl", provider.get("baseUrl")))
        if not spec["api"] or not spec["base_url"]:
            raise Error("Pi's custom provider must specify api and baseUrl.")
        spec.setdefault("binary", shutil.which("pi") or "pi")
    elif spec.get("harness") in {"claude", "opencode"}:
        if not spec.get("model") or not spec.get("base_url"):
            raise Error("This worker requires an explicit model and base_url.")
        spec.setdefault("provider", "local" if spec["harness"] == "opencode" else "anthropic-compatible")
        spec.setdefault("binary", spec["harness"])
    else:
        raise Error("Supported worker harnesses: " + ", ".join(ADAPTERS))
    spec["binary"] = str(Path(spec["binary"]).expanduser())
    return spec


def check_policy(spec, sandbox):
    if spec["harness"] != "codex" and sandbox != "danger-full-access":
        raise Error(f"{spec['harness']} has no built-in workspace sandbox supported by this bridge. Explicitly enable --sandbox danger-full-access for this project.")


def task_instructions(task):
    return WORKER_INSTRUCTIONS + f"""
Implementation workspace: {task['workspace']}
Original checkout (do not edit or run implementation commands here): {task['project']}
Your harness is already started in the implementation workspace. Resolve all
assignment file paths relative to that workspace. If an assignment mentions an
absolute path under the original checkout, translate it to the corresponding
relative path inside your workspace. Never follow it back to the original.
Run tests in your workspace, and leave integration to the operator's overseer apply.
"""


def freeze_worker(selected, folder, sandbox, *, instructions=WORKER_INSTRUCTIONS):
    check_policy(selected, sandbox)
    spec = dict(selected)
    if spec["harness"] == "codex":
        profile = folder / "worker.config.toml"
        profile.write_bytes(Path(spec["profile"]).expanduser().read_bytes())
        profile.chmod(0o600)
        spec["profile"] = str(profile)
    elif spec["harness"] in {"pi", "omp"}:
        config = json.loads(Path(spec.pop("models")).expanduser().read_text())
        provider = dict(config["providers"][spec["provider"]])
        provider["models"] = [m for m in provider["models"] if m["id"] == spec["model"]]
        provider.pop("modelOverrides", None)
        # Pi marks environment references with '$'; OMP expects the variable
        # name itself. Keep the reference, never resolve a secret into state.
        reference = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", str(provider.get("apiKey", "")))
        if spec["harness"] == "omp" and reference:
            provider["apiKey"] = reference[1]
            spec["api_key_env"] = reference[1]
        agent_dir = folder / (spec["harness"] + "-home")
        filename = "models.yml" if spec["harness"] == "omp" else "models.json"
        atomic_json(agent_dir / filename, {"providers": {spec["provider"]: provider}})
        if spec["harness"] == "omp":
            atomic_json(agent_dir / "worker.yml", {
                "disabledProviders": ["native", "claude", "claude-plugins", "codex", "gemini", "opencode", "agents-md", "cursor", "github"],
                "enabledProviders": [], "retry": {"fallbackChains": {}},
                "telemetry": {"otlpExportEnabled": False}})
        spec["agent_dir"] = str(agent_dir)
    else:
        agent_dir = folder / (spec["harness"] + "-home")
        agent_dir.mkdir(mode=0o700)
        spec["agent_dir"] = str(agent_dir)
        if spec["harness"] == "claude":
            spec["session_id"] = str(uuid.uuid4())
        else:
            model = spec["provider"] + "/" + spec["model"]
            key = "{env:" + spec["api_key_env"] + "}" if spec.get("api_key_env") else "unused-local-key"
            atomic_json(agent_dir / "opencode.json", {
                "provider": {spec["provider"]: {"npm": "@ai-sdk/openai-compatible", "name": "Foundry worker",
                    "options": {"baseURL": spec["base_url"], "apiKey": key},
                    "models": {spec["model"]: {"name": spec["model"], "limit": {
                        "context": spec.get("context_window", 32768), "output": spec.get("max_tokens", 4096)}}}}},
                "enabled_providers": [spec["provider"]], "model": model, "small_model": model,
                "agent": {"foundry": {"mode": "primary", "model": model, "prompt": instructions}},
                "permission": {"*": "deny", **{name: "allow" for name in (
                    "read", "edit", "bash", "glob", "grep", "list", "external_directory")}},
                "autoupdate": False, "snapshot": False, "share": "disabled", "plugin": [], "mcp": {}})
    return {"schema_version": 2, "worker": spec["name"], "harness": spec["harness"], "worker_config": spec}


def legacy_worker(task):
    # Missing harness metadata always means Codex, regardless of new named defaults.
    spec = select_worker("codex-local", {**SETTINGS, "workers": {}})
    if ((task.get("model") and task["model"] != spec["model"]) or
            (task.get("provider") and task["provider"] != spec["provider"])):
        raise Error("Legacy job's model configuration changed. Restore its original Codex profile before resuming.")
    return spec


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def output(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def run(argv, cwd=None, *, data=None, env=None, check=True):
    p = subprocess.run([str(x) for x in argv], cwd=cwd, input=data,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if check and p.returncode:
        raise Error(p.stderr.decode(errors="replace").strip() or f"Command failed: {argv[0]}")
    return p


def git(root, *args, **kw):
    return run(["git", "-C", root, *args], **kw)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x") as f:
            os.chmod(tmp, 0o600)
            json.dump(value, f, indent=2)
            f.write("\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield f


def project(path=None, *, enabled=True):
    requested = Path(path or os.getcwd()).resolve()
    p = git(requested, "rev-parse", "--show-toplevel", check=False)
    if p.returncode:
        raise Error("Run this command inside a Git project, or use --project /path/to/repo.")
    root = Path(p.stdout.decode().strip()).resolve()
    if enabled:
        marker = root / MARKER
        if not marker.is_file():
            raise Error(f"Spark workers are not enabled for {root}. Run: overseer enable")
        config = json.loads(marker.read_text())
        if config.get("owner") != "spark-workers" or config.get("version") != 1:
            raise Error("Unrecognized Spark worker project configuration.")
    return root


def project_state(root):
    key = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    return STATE / "projects" / key


def state_path(root, task_id):
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        raise Error("Invalid task ID; use overseer status to find it.")
    path = project_state(root) / "tasks" / task_id / "task.json"
    if not path.is_file():
        raise Error(f"Task {task_id} does not exist in this project.")
    return path


def process_token(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def read_task(path):
    with locked(path.with_suffix(".lock")):
        task = json.loads(path.read_text())
        if task["status"] not in TERMINAL and task.get("pid"):
            if process_token(task["pid"]) != task.get("process_token"):
                task.update(status="orphaned", error="Worker supervisor stopped. Follow up to resume.", updated_at=now())
                atomic_json(path, task)
        return task


def update_task(path, **values):
    with locked(path.with_suffix(".lock")):
        task = json.loads(path.read_text())
        task.update(values, updated_at=now())
        atomic_json(path, task)
        return task


def tasks(root):
    return [read_task(p) for p in sorted((project_state(root) / "tasks").glob("*/task.json"))]


def prompt_from(args):
    if args.prompt_file and args.prompt:
        raise Error("Use assignment text or --prompt-file, not both.")
    if args.prompt_file:
        text = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text()
    else:
        text = args.prompt or ""
    if not text.strip():
        raise Error("Provide an assignment or --prompt-file FILE (use - for stdin).")
    return text


def snapshot(root, folder):
    """Capture current files through a temporary index; never touch the user's index."""
    head = git(root, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode:
        raise Error("Create an initial Git commit before using isolated Spark workers.")
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(folder / "snapshot.index")
    try:
        git(root, "read-tree", "HEAD", env=env)
        git(root, "add", "-A", "--", ".", env=env)
        tree = git(root, "write-tree", env=env).stdout.strip().decode()
        commit = git(root, "-c", "user.name=Spark Workers", "-c", "user.email=spark-workers@localhost",
                     "commit-tree", tree, "-p", head.stdout.strip().decode(),
                     data=b"Private Spark worker input snapshot\n", env=env).stdout.strip().decode()
    finally:
        (folder / "snapshot.index").unlink(missing_ok=True)
    workspace = folder / "workspace"
    git(root, "worktree", "add", "--detach", workspace, commit)
    return commit, workspace


def launch(path):
    log = path.parent / "supervisor.log"
    # The child must not read state until its process identity is recorded.
    with locked(path.with_suffix(".lock")):
        task = json.loads(path.read_text())
        with log.open("ab") as f:
            p = subprocess.Popen([sys.executable, str(APP / "spark_workers.py"), "_work", str(path)],
                                 stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                                 start_new_session=True, close_fds=True)
        task.update(pid=p.pid, process_token=process_token(p.pid), updated_at=now())
        atomic_json(path, task)
    return task


def harness_command(task, *, instructions=WORKER_INSTRUCTIONS):
    # Codex 0.163 supports profile files on exec/TUI, but not app-server.
    # Flatten the existing profile into per-process overrides, as codex-spark's
    # shared-server launcher does. No config files or daemon settings change.
    spec = task.get("worker_config", {})
    if spec.get("harness") == "claude":
        command = [spec["binary"], "--bare", "--print", "--verbose", "--output-format", "stream-json",
                   "--input-format", "text", "--model", spec["model"], "--effort", "medium", "--dangerously-skip-permissions",
                   "--tools", "Bash,Read,Edit,Write,Glob,Grep", "--strict-mcp-config",
                   "--mcp-config", '{"mcpServers":{}}', "--append-system-prompt", instructions]
        command.extend(["--resume", task["resume_ref"]] if task.get("resume_ref") else ["--session-id", spec["session_id"]])
        return command
    if spec.get("harness") == "opencode":
        command = [spec["binary"], "run", "--standalone", "--format", "json", "--model",
                   spec["provider"] + "/" + spec["model"], "--agent", "foundry", "--auto"]
        if task.get("resume_ref"):
            command.extend(["--session", task["resume_ref"]])
        return command
    if spec.get("harness") == "omp":
        model = spec["provider"] + "/" + spec["model"]
        command = [spec["binary"], "--mode", "rpc", "--no-ui", "--no-extensions", "--no-skills",
                   "--no-rules", "--no-lsp", "--no-title", "--no-prewalk", "--no-pty",
                   "--config", str(Path(spec["agent_dir"]) / "worker.yml"),
                   "--model", model, "--smol", model, "--slow", model, "--plan", model,
                   "--tools", "read,bash,edit,write,grep,glob", "--thinking", "off",
                   "--append-system-prompt", instructions,
                   "--session-dir", str(Path(task["workspace"]).parent / "sessions")]
        if task.get("resume_ref"):
            command.extend(["--resume", task["resume_ref"]])
        return command
    if spec.get("harness") == "pi":
        command = [spec["binary"], "--mode", "rpc", "--offline", "--no-extensions", "--no-mcp",
                   "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                   "--no-approve", "--provider", spec["provider"], "--model", spec["model"],
                   "--tools", "read,bash,edit,write,grep,find,ls", "--thinking", "off",
                   "--append-system-prompt", instructions,
                   "--session-dir", str(Path(task["workspace"]).parent / "sessions")]
        if task.get("resume_ref"):
            command.extend(["--session", task["resume_ref"]])
        return command
    profile = model_profile(spec.get("profile"))
    command = [spec.get("binary", str(CODEX)), "app-server", "--listen", "stdio://"]

    def flatten(table, prefix=""):
        for key, value in table.items():
            if not prefix and key == "projects":
                continue
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flatten(value, dotted)
            else:
                command.extend(["-c", f"{dotted}={json.dumps(value)}"])

    flatten(profile)
    return command


def harness_environment(task, *, instructions=None):
    # App-server persists project trust on thread/start even with CLI overrides.
    # Give the worker a real, private Codex home for config, history, and caches.
    # This uses CODEX_HOME for its documented purpose, only in the child process.
    spec = task.get("worker_config", {})
    if spec.get("api_key_env") and not os.environ.get(spec["api_key_env"]):
        raise Error(f"Missing credential environment variable: {spec['api_key_env']}")
    if spec.get("harness") in {"pi", "omp"}:
        env = os.environ.copy()
        env.update(PI_CODING_AGENT_DIR=spec["agent_dir"], PI_OFFLINE="1", PI_TELEMETRY="0", OTEL_SDK_DISABLED="true")
        if spec["harness"] == "omp":
            for key in ("OMP_PROFILE", "CLAUDE_CONFIG_DIR", "PI_SMOL_MODEL", "PI_SLOW_MODEL", "PI_PLAN_MODEL"):
                env.pop(key, None)
        return env
    if spec.get("harness") == "claude":
        env = os.environ.copy()
        for key in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        env.update(CLAUDE_CONFIG_DIR=spec["agent_dir"], ANTHROPIC_BASE_URL=spec["base_url"],
                   ANTHROPIC_MODEL=spec["model"], ANTHROPIC_SMALL_FAST_MODEL=spec["model"],
                   ANTHROPIC_API_KEY=os.environ[spec["api_key_env"]] if spec.get("api_key_env") else "unused-local-key",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="1")
        if spec.get("auth_mode") == "bearer":
            env["ANTHROPIC_CUSTOM_HEADERS"] = "Authorization: Bearer " + env["ANTHROPIC_API_KEY"]
        for role in ("HAIKU", "SONNET", "OPUS"):
            env["ANTHROPIC_DEFAULT_" + role + "_MODEL"] = spec["model"]
        return env
    if spec.get("harness") == "opencode":
        env = os.environ.copy()
        directory = Path(spec["agent_dir"])
        for kind in ("DATA", "CONFIG", "CACHE", "STATE"):
            env["XDG_" + kind + "_HOME"] = str(directory / kind.lower())
        config = json.loads((directory / "opencode.json").read_text())
        if instructions is not None:
            config["agent"]["foundry"]["prompt"] = instructions
        env.update(OPENCODE_CONFIG_CONTENT=json.dumps(config),
                   OPENCODE_CONFIG_DIR=str(directory / "config"), OPENCODE_DISABLE_PROJECT_CONFIG="true",
                   OPENCODE_DISABLE_CLAUDE_CODE="true", OPENCODE_DISABLE_AUTOUPDATE="true",
                   OPENCODE_DISABLE_DEFAULT_PLUGINS="true", OPENCODE_DISABLE_EXTERNAL_SKILLS="true")
        return env
    original = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    worker_codex_home = project_state(Path(task["project"])) / "codex-home"
    worker_codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = original / "config.toml"
    (worker_codex_home / "config.toml").write_bytes(config.read_bytes() if config.exists() else b"")
    for name in ("AGENTS.md", "rules", "requirements.toml"):
        source = original / name
        target = worker_codex_home / name
        if source.exists() and not target.exists():
            target.symlink_to(source, target_is_directory=source.is_dir())
    env = os.environ.copy()
    env["CODEX_HOME"] = str(worker_codex_home)
    return env


class Rpc:
    """Asynchronous LF-delimited client; stderr goes to the task log."""
    def __init__(self, command, cwd, log, events, env=None, dialect="codex"):
        self.dialect = dialect
        env = dict(os.environ if env is None else env)
        # OpenCode's headless CLI prefers PWD to process.cwd(). Keep the logical
        # directory consistent with Popen(cwd=...), never the operator checkout.
        env["PWD"] = str(Path(cwd).resolve())
        self.log = log.open("ab")
        self.events = events.open("a", encoding="utf-8")
        self.proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.log, text=True,
                                     bufsize=1, close_fds=True, env=env, encoding="utf-8")
        self.inbox = queue.Queue()
        self.sequence = 0
        self.pending = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                self.inbox.put(json.loads(line))
        except Exception as exc:
            self.inbox.put(exc)
        finally:
            self.inbox.put(EOFError("Worker harness disconnected; inspect app-server.log."))

    def send(self, method, params=None, *, request=True):
        msg = ({"type": method, **(params or {})} if self.dialect == "pi" else
               {"method": method, "params": params or {}})
        if request:
            self.sequence += 1
            msg["id"] = str(self.sequence) if self.dialect == "pi" else self.sequence
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return msg.get("id")

    def receive(self, timeout=0.25):
        msg = self.inbox.get(timeout=timeout)
        if isinstance(msg, Exception):
            raise msg
        self.events.write(json.dumps(msg) + "\n")
        self.events.flush()
        # Never hang unattended on an interactive tool or approval request.
        if self.dialect == "codex" and "method" in msg and "id" in msg:
            self.proc.stdin.write(json.dumps({"id": msg["id"], "error": {
                "code": -32000, "message": "Unattended Spark worker cannot answer interactive requests. Report the blocker."}}) + "\n")
            self.proc.stdin.flush()
        return msg

    def call(self, method, params=None, timeout=60):
        req = self.send(method, params)
        self.last_request_id = req
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.receive(min(0.25, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if msg.get("id") == req and "method" not in msg:
                if self.dialect == "pi":
                    if not msg.get("success"):
                        raise Error(f"{method}: {msg.get('error', 'command failed')}")
                    return msg.get("data", {})
                if "error" in msg:
                    raise Error(f"{method}: {msg['error']}")
                return msg["result"]
            self.pending.append(msg)
        raise Error(f"Timed out waiting for {method}.")

    def close(self):
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self.reader.join(timeout=2)
        self.proc.stdout.close()
        self.log.close()
        self.events.close()


def worker(path):
    os.umask(0o077)
    # Synchronizes with launch(), which records the supervisor's PID.
    task = read_task(path)
    folder = path.parent
    cancelled = folder / "cancel"
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    rpc = None
    slot = None
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        slot = (STATE / "inference.lock").open("a+")
        while True:
            if cancelled.exists() or stopping.is_set():
                update_task(path, status="cancelled", finished_at=now())
                return
            if not (Path(task["project"]) / MARKER).is_file():
                raise Error("Project was disabled before this task started.")
            try:
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.25)
        update_task(path, status="running", started_at=now(), error=None)
        # Legacy records are Codex jobs. New jobs freeze their selected configuration.
        if not task.get("worker_config"):
            task["worker_config"] = legacy_worker(task)
            task.setdefault("sandbox", "workspace-write")
            task.update(freeze_worker(task["worker_config"], folder, task["sandbox"]))
            update_task(path, **{k: task[k] for k in ("worker_config", "harness", "worker", "schema_version")})
        spec = task["worker_config"]
        check_policy(spec, task["sandbox"])
        instructions = task_instructions(task)
        command = harness_command(task, instructions=instructions)
        streaming = spec["harness"] in {"claude", "opencode"}
        kwargs = {"dialect": "pi"} if spec["harness"] in {"pi", "omp"} else {}
        rpc = Rpc(command, task["workspace"], folder / "app-server.log", folder / "events.jsonl",
                  env=harness_environment(task, instructions=instructions), **kwargs)
        adapter = ADAPTERS[spec["harness"]](rpc, task)
        update_task(path, **adapter.start(instructions))
        if cancelled.exists() or stopping.is_set():
            update_task(path, status="cancelled", finished_at=now())
            return
        update_task(path, **adapter.prompt())
        deadline = time.monotonic() + task["timeout_seconds"]
        interrupt_at = None
        reason = None
        report = []
        pending = rpc.pending
        rpc.pending = []
        while True:
            if interrupt_at is None and (cancelled.exists() or stopping.is_set() or time.monotonic() > deadline):
                reason = "cancelled" if cancelled.exists() or stopping.is_set() else "timeout"
                adapter.interrupt()
                interrupt_at = time.monotonic()
            if interrupt_at is not None and time.monotonic() - interrupt_at > 15:
                raise Error(f"Worker {reason}; interrupt did not finish in 15 seconds.")
            try:
                msg = pending.pop(0) if pending else rpc.receive()
                event = adapter.event(msg)
            except queue.Empty:
                continue
            except EOFError:
                if not streaming:
                    raise
                event = adapter.finish()
            if not event:
                continue
            if event.get("resume_ref"):
                update_task(path, resume_ref=event["resume_ref"])
            event.update(timestamp=now(), attempt=task["attempt"], harness=spec["harness"])
            with (folder / "progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            if event["type"] == "report" and event["text"]:
                report.append(event["text"])
                (folder / "report.txt").write_text("\n\n".join(report), encoding="utf-8")
            if event["type"] == "finished":
                if reason == "cancelled":
                    status, error = "cancelled", None
                elif reason == "timeout":
                    status, error = "failed", "Worker exceeded its time limit."
                else:
                    status, error = event["status"], event.get("error")
                update_task(path, status=status, error=error, finished_at=now())
                break
    except Exception as exc:
        update_task(path, status="cancelled" if cancelled.exists() or stopping.is_set() else "failed",
                    error=str(exc), finished_at=now())
    finally:
        if rpc:
            rpc.close()
        if slot:
            slot.close()


def patch_for(task):
    workspace = Path(task["workspace"])
    # Include new and ignored-by-worker files tracked in its index, without staging
    # any files in the user's real checkout. New ignored artifacts remain excluded.
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(workspace.parent / "diff.index")
    try:
        git(workspace, "read-tree", task["snapshot"], env=env)
        git(workspace, "add", "-A", "--", ".", env=env)
        return git(workspace, "diff", "--cached", "--binary", "--no-ext-diff", "--no-renames",
                   task["snapshot"], "--", env=env).stdout
    finally:
        (workspace.parent / "diff.index").unlink(missing_ok=True)


def skill_template(relative, *, legacy=False):
    if relative == "assets/overseer.jpg":
        return (APP / relative).read_bytes()
    data = (APP / "templates" / relative).read_bytes()
    if legacy:
        data = data.replace(b"name: overseer\n", b"name: spark-workers\n").replace(b"$overseer", b"$spark-workers")
    return data


def command_enable(args):
    root = project(args.project, enabled=False)
    if root == Path.home() or root == Path("/"):
        raise Error("Enable a specific project repository, not your home directory or filesystem root.")
    marker = root / MARKER

    existing = {}
    if marker.exists():
        project(root)
        existing = json.loads(marker.read_text())
        if not args.update and args.sandbox is None and not getattr(args, "worker", None):
            output({"project": str(root), "enabled": True, "message": "Already enabled. Use $overseer (or $spark-workers in an older project) explicitly."})
            return
        if "overseer" not in existing.get("skills", []) and (root / SKILL).exists():
            for rel in SKILL_FILES:
                path = root / SKILL / rel
                if path.exists() and path.read_bytes() != skill_template(rel):
                    raise Error(f"{root / SKILL} already exists; refusing to overwrite an unrelated skill.")
    else:
        for relative in (SKILL, LEGACY_SKILL):
            if (root / relative).exists():
                raise Error(f"{root / relative} already exists; refusing to overwrite an existing skill.")
    sandbox = args.sandbox or existing.get("sandbox", "workspace-write")
    worker = getattr(args, "worker", None) or existing.get("worker")
    if worker:
        spec = select_worker(worker)
        check_policy(spec, sandbox)
    for relative in (SKILL, LEGACY_SKILL):
        for rel in SKILL_FILES:
            dest = root / relative / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(skill_template(rel, legacy=relative == LEGACY_SKILL))
    atomic_json(marker, {"owner": "spark-workers", "version": 1, "enabled_at": now(),
                         "explicit_only": True, "sandbox": sandbox, "worker": worker,
                         "skills": ["overseer", "spark-workers"]})
    output({"project": str(root), "enabled": True,
            "invoke": "$overseer <your task>", "legacy_invoke": "$spark-workers <your task>",
            "shell": "overseer run 'your task'",
            "execution": sandbox,
            "note": "Open a new Codex session in this project to discover the skill. Native subagents remain the default."})


def command_disable(args):
    root = project(args.project)
    active = [t["id"] for t in tasks(root) if t["status"] not in TERMINAL]
    if active:
        raise Error(f"Cancel or finish active tasks first: {', '.join(active)}")
    # Check both aliases before removing anything, preserving customized skills.
    for relative in (SKILL, LEGACY_SKILL):
        for rel in SKILL_FILES:
            path = root / relative / rel
            if path.exists() and path.read_bytes() != skill_template(rel, legacy=relative == LEGACY_SKILL):
                raise Error(f"{path} was customized; preserve it manually before disabling.")
    for relative in (SKILL, LEGACY_SKILL):
        for rel in SKILL_FILES:
            (root / relative / rel).unlink(missing_ok=True)
        for sub in ("agents", "assets", ""):
            with contextlib.suppress(OSError):
                (root / relative / sub).rmdir()
    (root / MARKER).unlink()
    output({"project": str(root), "enabled": False, "note": "Existing worker worktrees and logs are retained."})


def command_submit(args):
    root = project(args.project)
    assignment = prompt_from(args)
    config = json.loads((root / MARKER).read_text())
    selected = select_worker(getattr(args, "worker", None) or config.get("worker"))
    check_policy(selected, config.get("sandbox", "workspace-write"))
    task_id = uuid.uuid4().hex[:12]
    folder = project_state(root) / "tasks" / task_id
    folder.mkdir(parents=True, mode=0o700)
    frozen = freeze_worker(selected, folder, config.get("sandbox", "workspace-write"))
    with locked(project_state(root) / "submit.lock"):
        commit, workspace = snapshot(root, folder)
        task = {"id": task_id, "name": args.name, "project": str(root), "workspace": str(workspace),
                "snapshot": commit, "status": "queued", "assignment": assignment,
                "created_at": now(), "updated_at": now(), "timeout_seconds": args.timeout,
                "thread_id": None, "applied": False, "attempt": 1,
                "sandbox": config.get("sandbox", "workspace-write"), **frozen}
        path = folder / "task.json"
        atomic_json(path, task)
        output(launch(path))


def command_followup(args):
    root = project(args.project)
    path = state_path(root, args.task_id)
    text = prompt_from(args)
    with locked(path.parent / "operation.lock"):
        task = read_task(path)
        if task["status"] not in TERMINAL:
            raise Error("Task is active. Wait or cancel it before following up.")
        if task.get("applied"):
            raise Error("This task has already been applied. Submit a new task against the updated project.")
        if not task.get("worker_config"):
            # Freeze legacy Codex jobs before archiving their current report.
            update_task(path, **freeze_worker(legacy_worker(task), path.parent, task.get("sandbox", "workspace-write")))
        (path.parent / "cancel").unlink(missing_ok=True)
        report = path.parent / "report.txt"
        if report.exists():
            report.replace(path.parent / f"report-attempt-{task['attempt']}.txt")
        update_task(path, status="queued", assignment=text, attempt=task["attempt"] + 1,
                    started_at=None, finished_at=None, error=None, turn_id=None,
                    timeout_seconds=getattr(args, "timeout", None) or task["timeout_seconds"])
        output(launch(path))


def command_status(args):
    root = project(args.project)
    if args.task_id:
        output(read_task(state_path(root, args.task_id)))
    else:
        output([{k: t.get(k) for k in ("id", "name", "worker", "harness", "status", "attempt", "applied", "updated_at", "error")}
                for t in sorted(tasks(root), key=lambda t: t["created_at"])])


def command_wait(args):
    root = project(args.project)
    path = state_path(root, args.task_id)
    deadline = time.monotonic() + args.seconds
    while True:
        task = read_task(path)
        if task["status"] in TERMINAL or time.monotonic() >= deadline:
            output(task)
            return
        time.sleep(0.25)


def command_result(args):
    root = project(args.project)
    path = state_path(root, args.task_id)
    task = read_task(path)
    report = path.parent / "report.txt"
    task.update(report=report.read_text() if report.exists() else "",
                events=str(path.parent / "events.jsonl"),
                progress=str(path.parent / "progress.jsonl"),
                log=str(path.parent / "app-server.log"),
                supervisor_log=str(path.parent / "supervisor.log"))
    output(task)


def command_cancel(args):
    root = project(args.project)
    path = state_path(root, args.task_id)
    with locked(path.parent / "operation.lock"):
        task = read_task(path)
        if task["status"] not in TERMINAL:
            (path.parent / "cancel").touch(mode=0o600)
        output({"id": args.task_id, "status": task["status"],
                "cancellation_requested": task["status"] not in TERMINAL})


def command_diff(args):
    root = project(args.project)
    path = state_path(root, args.task_id)
    with locked(path.parent / "operation.lock"):
        task = read_task(path)
        if task["status"] not in TERMINAL:
            raise Error("Wait for the worker to stop before inspecting a consistent diff.")
        patch = patch_for(task)
        if args.stat and patch:
            patch = git(root, "apply", "--stat", data=patch).stdout
        sys.stdout.buffer.write(patch)


def command_apply(args):
    root = project(args.project)
    path = state_path(root, args.task_id)
    with locked(project_state(root) / "apply.lock"), locked(path.parent / "operation.lock"):
        task = read_task(path)
        if task["status"] != "completed":
            raise Error("Only completed tasks can be applied. Inspect failures or follow up first.")
        if task.get("applied"):
            raise Error("This task has already been applied.")
        patch = patch_for(task)
        if patch:
            git(root, "apply", "--check", "--binary", "--whitespace=nowarn", data=patch)
            git(root, "apply", "--binary", "--whitespace=nowarn", data=patch)
        update_task(path, applied=True, applied_at=now())
        output({"id": task["id"], "applied": True, "changed": bool(patch), "project": str(root)})


def command_doctor(args):
    root = project(args.project)
    config = json.loads((root / MARKER).read_text())
    spec = select_worker(getattr(args, "worker", None) or config.get("worker"))
    check_policy(spec, config.get("sandbox", "workspace-write"))
    checks = {"project_enabled": True, "explicit_only": "allow_implicit_invocation: false" in
              (root / (SKILL if (root / SKILL).is_dir() else LEGACY_SKILL) / "agents/openai.yaml").read_text(),
              "harness_binary": bool(shutil.which(spec["binary"])),
              "initial_commit": git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0}
    parsed = urllib.parse.urlsplit(spec["base_url"])
    health_url = spec.get("health_url", urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", "")))
    if health_url:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as response:
                checks["local_endpoint"] = 200 <= response.status < 300
        except (OSError, ValueError):
            checks["local_endpoint"] = False
    output({"project": str(root), "ok": all(checks.values()), "checks": checks,
            "inference_slots": 1, "worker": spec["name"], "harness": spec["harness"],
            "execution": config.get("sandbox", "workspace-write"),
            "model": spec["model"], "provider": spec["provider"],
            "note": "Health checks do not prove tool compatibility. Validate a small real task."})
    if not all(checks.values()):
        raise SystemExit(1)


def command_instructions(args):
    print((APP / "templates/OPERATOR.md").read_text(), end="")


def command_run(args):
    root = project(args.project)
    prompt = prompt_from(args)
    os.chdir(root)
    command = [str(CODEX), "--cd", str(root)]
    if getattr(args, "model", None):
        command.extend(["--model", args.model])
    if args.full_access:
        command.extend(["--sandbox", "danger-full-access", "--ask-for-approval", "never"])
    invocation = "$overseer" if (root / SKILL / "SKILL.md").is_file() else "$spark-workers"
    os.execv(CODEX, [*command, invocation + " " + prompt])


def write_settings(settings):
    def table(values, prefix=()):
        lines = []
        if prefix:
            lines.append("[" + ".".join(json.dumps(key) for key in prefix) + "]\n")
        for key, value in values.items():
            if not isinstance(value, dict):
                lines.append(f"{json.dumps(key)} = {json.dumps(value)}\n")
        for key, value in values.items():
            if isinstance(value, dict):
                lines.extend(table(value, (*prefix, key)))
        return lines
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x") as stream:
            os.chmod(tmp, 0o600)
            stream.write("".join(table(settings)))
        tmp.replace(CONFIG_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def command_configure(args):
    settings = json.loads(json.dumps(SETTINGS))
    harness = "claude" if args.harness == "claude-code" else args.harness
    name = args.worker or (harness + "-local")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise Error("Worker names may contain letters, numbers, underscores and hyphens.")
    key_env = args.api_key_env
    if args.context_window < 1 or args.max_tokens < 1:
        raise Error("Context and output token limits must be positive.")
    if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise Error("--api-key-env takes an environment variable name, never a secret value.")
    if args.base_url:
        parsed = urllib.parse.urlsplit(args.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise Error("Use an HTTP(S) base URL without embedded credentials.")
    binary = args.binary or (args.codex or str(CODEX) if harness == "codex" else args.pi if harness == "pi" else harness)
    resolved = shutil.which(binary)
    binary = resolved or (str(Path(binary).expanduser().resolve()) if "/" in binary else binary)
    spec = {"harness": harness, "binary": binary}
    if harness in {"pi", "omp"}:
        if not args.model or (not args.models and not args.base_url):
            raise Error(f"{harness} requires --model and either --models FILE or --base-url URL.")
        if args.models and args.base_url:
            raise Error("Use --models or --base-url, not both.")
        provider = args.provider or "local"
        models = Path(args.models).expanduser().resolve() if args.models else CONFIG_FILE.parent / "workers" / name / "models.json"
        if not args.models:
            key = ("$" + key_env if harness == "pi" else key_env) if key_env else "unused-local-key"
            atomic_json(models, {"providers": {provider: {"baseUrl": args.base_url,
                "api": "openai-completions", "apiKey": key, "models": [{"id": args.model,
                "name": args.model, "reasoning": False, "input": ["text"],
                "contextWindow": args.context_window, "maxTokens": args.max_tokens,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}})
        spec.update(models=str(models), provider=provider, model=args.model)
    elif harness in {"claude", "opencode"}:
        if not args.base_url or not args.model:
            raise Error(f"{harness} requires --base-url URL and --model ID; cloud defaults are not imported.")
        spec.update(base_url=args.base_url, model=args.model,
                    provider=args.provider or ("local" if harness == "opencode" else "anthropic-compatible"),
                    context_window=args.context_window, max_tokens=args.max_tokens)
        if harness == "claude":
            spec["auth_mode"] = args.auth_mode
    else:
        if not args.profile:
            raise Error("Codex requires --profile FILE.")
        profile = Path(args.profile).expanduser().resolve()
        model_profile(profile)
        spec["profile"] = str(profile)
        if name == "codex-local":
            settings["worker_profile"] = str(profile)
            settings["codex_binary"] = binary
    if key_env:
        spec["api_key_env"] = key_env
    if args.health_url is not None:
        spec["health_url"] = args.health_url
    elif "health_url" in settings.get("workers", {}).get(name, {}):
        spec["health_url"] = settings["workers"][name]["health_url"]
    select_worker(name, {**settings, "workers": {**settings.get("workers", {}), name: spec}})
    settings.setdefault("workers", {})[name] = spec
    write_settings(settings)
    output({"config": str(CONFIG_FILE), "worker": name, "harness": harness,
            "note": "Normal harness settings and delegation are unchanged. Select this worker with enable or submit --worker."})


def command_workers(args):
    rows = []
    names = set(SETTINGS.get("workers", {}))
    if PROFILE.is_file():
        names.add("codex-local")
    for name in sorted(names):
        raw = SETTINGS.get("workers", {}).get(name, {})
        try:
            spec = select_worker(name)
            rows.append({"worker": name, "harness": spec["harness"], "model": spec["model"],
                         "provider": spec["provider"], "binary": spec["binary"],
                         "installed": bool(shutil.which(spec["binary"])), "valid": True})
        except (Error, OSError, ValueError, KeyError) as exc:
            rows.append({"worker": name, "harness": raw.get("harness", "codex"), "valid": False, "error": str(exc)})
    available = [{"harness": harness, "binary": shutil.which(harness),
                  "installed": bool(shutil.which(harness))} for harness in ADAPTERS]
    output({"config": str(CONFIG_FILE), "harnesses": available, "workers": rows})


def command_setup(args):
    if not sys.stdin.isatty():
        raise Error("setup needs an interactive terminal. Use overseer configure --help for scripting.")
    def ask(label, default=""):
        answer = input(label + (f" [{default}]" if default else "") + ": ").strip()
        return answer or default
    args.harness = ask("Harness: codex, pi, omp, opencode, claude", "pi")
    if args.harness not in ADAPTERS:
        raise Error("Unknown harness. Choose " + ", ".join(ADAPTERS))
    args.worker = ask("Worker name", args.harness + "-local")
    args.binary = ask("Harness executable", args.harness)
    if args.harness == "codex":
        args.profile = ask("Local Codex profile", str(PROFILE))
    else:
        args.base_url = ask("Model base URL (Claude needs Anthropic Messages)", "http://127.0.0.1:18888/v1" if args.harness != "claude" else "http://127.0.0.1:18888")
        args.model = ask("Exact served model ID", "local-worker")
        if args.harness != "claude":
            args.provider = ask("Provider name", "local")
        args.api_key_env = ask("Credential environment variable name (blank for no auth)") or None
        if args.harness == "claude":
            args.auth_mode = ask("Endpoint auth header: api-key or bearer", "api-key")
            if args.auth_mode not in {"api-key", "bearer"}:
                raise Error("Choose api-key or bearer authentication.")
    command_configure(args)
    print("Next: overseer workers; then inside your project:")
    print(f"  overseer enable --worker {args.worker}" + (" --sandbox danger-full-access" if args.harness != "codex" else ""))
    print("  overseer doctor")
    print('  overseer run "your task"')


def bounded_seconds(text):
    value = int(text)
    if not 1 <= value <= 3600:
        raise argparse.ArgumentTypeError("Use between 1 and 3600 seconds.")
    return value


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "_work":
        worker(Path(sys.argv[2]))
        return
    parser = argparse.ArgumentParser(description="Overseer: local-model workers for your coding harness.",
                                     epilog="Quick start: overseer enable; give your operator the overseer instructions workflow")
    parser.add_argument("--version", action="version", version="Overseer 0.5.0")
    commands = parser.add_subparsers(dest="command", required=True)
    actions = {
        "setup": (command_setup, "Interactively register a worker"),
        "workers": (command_workers, "List configured workers and executable availability"),
        "instructions": (command_instructions, "Print the operator workflow for any coding harness"),
        "configure": (command_configure, "Configure a worker without changing your harness defaults"),
        "enable": (command_enable, "Enable Overseer for this project; normal delegation stays unchanged"),
        "disable": (command_disable, "Remove this project's integration"),
        "doctor": (command_doctor, "Check project opt-in and local provider health"),
        "run": (command_run, "Open Codex as operator with Overseer explicitly invoked"),
        "submit": (command_submit, "Queue an assignment in an isolated worktree"),
        "followup": (command_followup, "Resume a finished worker with a correction or follow-up"),
        "status": (command_status, "Show project tasks or one task"),
        "wait": (command_wait, "Wait briefly for a task without losing progress"),
        "result": (command_result, "Read the worker's report and log locations"),
        "cancel": (command_cancel, "Interrupt an active or queued task"),
        "diff": (command_diff, "Inspect worker changes, including new files"),
        "apply": (command_apply, "Apply reviewed completed changes to the project"),
    }
    subparsers = {}
    for name, (func, description) in actions.items():
        sub = commands.add_parser(name, help=description, description=description)
        subparsers[name] = sub
        sub.add_argument("--project", help="Project path (defaults to current Git repository)")
        sub.set_defaults(func=func)
        if name in {"followup", "wait", "result", "cancel", "diff", "apply"}:
            sub.add_argument("task_id")
        if name == "status":
            sub.add_argument("task_id", nargs="?")
        if name in {"submit", "followup", "run"}:
            sub.add_argument("prompt", nargs="?", help="Assignment text")
            sub.add_argument("--prompt-file", help="Read assignment from a file; - reads stdin")
        if name == "submit":
            sub.add_argument("--name", default="implementation", help="Human-readable task label")
            sub.add_argument("--timeout", type=bounded_seconds, default=1800, help="Turn time limit in seconds (default: 1800)")
        if name == "followup":
            sub.add_argument("--timeout", type=bounded_seconds, help="Turn time limit in seconds (default: preserve this job's limit)")
        if name == "run":
            sub.add_argument("--full-access", action="store_true", help="Explicitly use full-access permissions for this operator session too (needed when host sandboxing is unavailable)")
            sub.add_argument("--model", help="Frontier operator model; omitted preserves your normal Codex model")
        if name in {"configure", "setup"}:
            sub.add_argument("--harness", choices=(*ADAPTERS, "claude-code"), default="codex")
            sub.add_argument("--binary", help="Harness executable (all harnesses)")
            sub.add_argument("--base-url", help="Model endpoint; Chat Completions for Pi/OMP/OpenCode, Anthropic Messages for Claude")
            sub.add_argument("--api-key-env", help="Credential environment variable NAME; never pass the secret itself")
            sub.add_argument("--auth-mode", choices=("api-key", "bearer"), default="api-key", help="Claude endpoint authentication header (default: api-key)")
            sub.add_argument("--context-window", type=int, default=32768, help="Advertised context for Pi/OMP/OpenCode (default: 32768)")
            sub.add_argument("--max-tokens", type=int, default=4096, help="Output limit for Pi/OMP/OpenCode (default: 4096); Claude uses native defaults")
            sub.add_argument("--pi", default="pi", help="Pi executable name or path")
            sub.add_argument("--models", help="Pi models.json containing the selected custom provider")
            sub.add_argument("--provider", help="Pi custom provider name")
            sub.add_argument("--model", help="Pi model ID")
            sub.add_argument("--profile", help="Path to a Codex TOML profile with a custom local Responses provider")
            sub.add_argument("--codex", help="Codex executable name or path (default: PATH)")
            sub.add_argument("--health-url", help="Optional health endpoint; an empty value disables the health request")
        if name in {"configure", "setup", "enable", "submit", "doctor"}:
            sub.add_argument("--worker", help="Named worker (default: project choice, otherwise codex-local)")
        if name == "enable":
            sub.add_argument("--update", action="store_true", help="Refresh a previously installed skill")
            sub.add_argument("--sandbox", choices=("danger-full-access", "workspace-write"),
                             default=None, help="Worker execution mode (new projects: workspace-write; updates preserve the saved mode)")
        if name == "diff":
            sub.add_argument("--stat", action="store_true", help="Show a diff summary")
        if name == "wait":
            sub.add_argument("--seconds", type=bounded_seconds, default=30, help="Maximum wait (1-60; default: 30)")
    if len(sys.argv) > 1 and sys.argv[1] in subparsers:
        args = subparsers[sys.argv[1]].parse_intermixed_args(sys.argv[2:])
        args.command = sys.argv[1]
    else:
        args = parser.parse_args()
    if args.command == "wait" and args.seconds > 60:
        parser.error("wait --seconds is limited to 60 seconds; use repeated waits")
    try:
        args.func(args)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except (Error, OSError, ValueError) as exc:
        print(f"overseer: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
