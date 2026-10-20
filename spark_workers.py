#!/usr/bin/env python3
"""Frontier Foundry: frontier planning, local execution, explicit project opt-in."""
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

APP = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("SPARK_WORKERS_CONFIG", Path.home() / ".config/frontier-foundry/config.toml")).expanduser()
try:
    SETTINGS = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.is_file() else {}
except (OSError, tomllib.TOMLDecodeError) as exc:
    raise SystemExit(f"Cannot read Frontier Foundry configuration: {type(exc).__name__}")
STATE = Path(os.environ.get("SPARK_WORKERS_STATE_DIR", SETTINGS.get("state_dir", str(Path.home() / ".local/state/spark-workers")))).expanduser()
CODEX = Path(os.environ.get("SPARK_WORKERS_CODEX", SETTINGS.get("codex_binary", shutil.which("codex") or str(Path.home() / ".local/bin/codex")))).expanduser()
user_codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
default_profile = user_codex_home / "spark.config.toml"
if not default_profile.is_file():
    default_profile = user_codex_home / "local-worker.config.toml"
PROFILE = Path(os.environ.get("SPARK_WORKERS_PROFILE", SETTINGS.get("worker_profile", str(default_profile)))).expanduser()
MARKER = Path(".codex/spark-workers.json")
SKILL = Path(".agents/skills/spark-workers")
TERMINAL = {"completed", "failed", "cancelled", "orphaned"}
WORKER_INSTRUCTIONS = """You are a local implementation worker managed by a frontier-model Codex operator.
Work only on the supplied assignment in your current isolated Git worktree.
Read applicable AGENTS.md instructions. Implement code and run relevant checks.
Do not delegate, launch other agents, invoke spark-workers, change the original
project, or manage live services. Do not commit, reset, clean, or push. Leave all
edits in the worktree. Avoid unrelated changes. Report changed files, commands
actually run and their outcomes, and any unresolved issue. If blocked, report the
concrete error rather than claiming success. Use the provided tools to make real
edits; a suggested patch in your final answer alone does not complete the task.
"""


class Error(RuntimeError):
    pass


def model_profile(path=None):
    path = Path(path or PROFILE)
    if not path.is_file():
        raise Error(f"Missing worker profile: {path}. Run foundry configure --profile /path/to/worker.config.toml.")
    profile = tomllib.loads(path.read_text())
    model, provider = profile.get("model"), profile.get("model_provider")
    spec = profile.get("model_providers", {}).get(provider, {})
    if not model or not provider or provider == "openai" or not spec.get("base_url"):
        raise Error("Worker profile must specify model, a custom model_provider, and that provider's base_url.")
    if spec.get("wire_api") != "responses":
        raise Error("The local provider must use wire_api = 'responses'. Chat Completions alone is not sufficient.")
    return profile


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
            raise Error(f"Spark workers are not enabled for {root}. Run: spark-workers enable")
        config = json.loads(marker.read_text())
        if config.get("owner") != "spark-workers" or config.get("version") != 1:
            raise Error("Unrecognized Spark worker project configuration.")
    return root


def project_state(root):
    key = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    return STATE / "projects" / key


def state_path(root, task_id):
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        raise Error("Invalid task ID; use spark-workers status to find it.")
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


def harness_command(task):
    # Codex 0.163 supports profile files on exec/TUI, but not app-server.
    # Flatten the existing profile into per-process overrides, as codex-spark's
    # shared-server launcher does. No config files or daemon settings change.
    profile = model_profile()
    command = [str(CODEX), "app-server", "--listen", "stdio://"]

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


def harness_environment(task):
    # App-server persists project trust on thread/start even with CLI overrides.
    # Give the worker a real, private Codex home for config, history, and caches.
    # This uses CODEX_HOME for its documented purpose, only in the child process.
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
    """Bounded, asynchronous JSONL app-server client; stderr goes to the task log."""
    def __init__(self, command, cwd, log, events, env=None):
        self.log = log.open("ab")
        self.events = events.open("a")
        self.proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.log, text=True,
                                     bufsize=1, close_fds=True, env=env)
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
            self.inbox.put(EOFError("Codex app-server disconnected; inspect app-server.log."))

    def send(self, method, params=None, *, request=True):
        msg = {"method": method, "params": params or {}}
        if request:
            self.sequence += 1
            msg["id"] = self.sequence
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
        if "method" in msg and "id" in msg:
            self.proc.stdin.write(json.dumps({"id": msg["id"], "error": {
                "code": -32000, "message": "Unattended Spark worker cannot answer interactive requests. Report the blocker."}}) + "\n")
            self.proc.stdin.flush()
        return msg

    def call(self, method, params=None, timeout=60):
        req = self.send(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.receive(min(0.25, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if msg.get("id") == req and "method" not in msg:
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
        # A dedicated stdio harness reuses spark.config.toml and the existing
        # tool adapter/model, without changing settings on either shared daemon.
        command = harness_command(task)
        rpc = Rpc(command, task["workspace"], folder / "app-server.log", folder / "events.jsonl",
                  env=harness_environment(task))
        rpc.call("initialize", {"clientInfo": {"name": "spark_workers", "title": "Spark Workers", "version": "1.0.0"},
                                "capabilities": {"experimentalApi": True}}, timeout=30)
        rpc.send("initialized", request=False)
        sandbox = task.get("sandbox", "workspace-write")
        profile = model_profile()
        model, provider = profile["model"], profile["model_provider"]
        params = {
            "model": model, "modelProvider": provider, "cwd": task["workspace"],
            "approvalPolicy": "never", "sandbox": sandbox,
            "developerInstructions": WORKER_INSTRUCTIONS,
            "config": {"agents.enabled": False, "features.apps": False,
                       "features.plugins": False, "features.memories": False,
                       "skills.include_instructions": False, "web_search": "disabled"},
        }
        if task.get("thread_id"):
            params.update(threadId=task["thread_id"], excludeTurns=True)
            result = rpc.call("thread/resume", params)
        else:
            result = rpc.call("thread/start", params)
        if result.get("modelProvider") != provider or result.get("model") != model:
            raise Error("Worker did not select the configured local model/provider; refusing inference.")
        thread_id = result["thread"]["id"]
        update_task(path, thread_id=thread_id, model=result.get("model"), provider=result.get("modelProvider"))
        if cancelled.exists() or stopping.is_set():
            update_task(path, status="cancelled", finished_at=now())
            return
        sandbox_policy = ({"type": "dangerFullAccess"} if sandbox == "danger-full-access" else
                          {"type": "workspaceWrite", "writableRoots": [task["workspace"]],
                           "networkAccess": False})
        result = rpc.call("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": task["assignment"], "text_elements": []}],
            "sandboxPolicy": sandbox_policy,
        })
        turn_id = result["turn"]["id"]
        update_task(path, turn_id=turn_id)
        deadline = time.monotonic() + task["timeout_seconds"]
        interrupt_at = None
        reason = None
        report = []
        pending = rpc.pending
        rpc.pending = []
        while True:
            if interrupt_at is None and (cancelled.exists() or stopping.is_set() or time.monotonic() > deadline):
                reason = "cancelled" if cancelled.exists() or stopping.is_set() else "timeout"
                rpc.send("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                interrupt_at = time.monotonic()
            if interrupt_at is not None and time.monotonic() - interrupt_at > 15:
                raise Error(f"Worker {reason}; interrupt did not finish in 15 seconds.")
            try:
                msg = pending.pop(0) if pending else rpc.receive()
            except queue.Empty:
                continue
            method = msg.get("method")
            params = msg.get("params", {})
            if params.get("threadId") != thread_id:
                continue
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    report.append(item.get("text", ""))
                    (folder / "report.txt").write_text("\n\n".join(report))
            if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                turn = params["turn"]
                if reason == "cancelled":
                    status, error = "cancelled", None
                elif reason == "timeout":
                    status, error = "failed", "Worker exceeded its time limit."
                else:
                    status = "completed" if turn["status"] == "completed" else "failed"
                    error = turn.get("error") if status == "failed" else None
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


def command_enable(args):
    root = project(args.project, enabled=False)
    if root == Path.home() or root == Path("/"):
        raise Error("Enable a specific project repository, not your home directory or filesystem root.")
    marker = root / MARKER
    skill = root / SKILL
    expected = ["SKILL.md", "agents/openai.yaml"]
    existing = {}
    if marker.exists():
        project(root)
        existing = json.loads(marker.read_text())
        if not args.update and args.sandbox is None:
            output({"project": str(root), "enabled": True, "message": "Already enabled. Use $spark-workers explicitly."})
            return
    elif skill.exists():
        raise Error(f"{skill} already exists; refusing to overwrite an existing skill.")
    sandbox = args.sandbox or existing.get("sandbox", "workspace-write")
    for rel in expected:
        dest = skill / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(APP / "templates" / rel, dest)
    atomic_json(marker, {"owner": "spark-workers", "version": 1, "enabled_at": now(),
                         "explicit_only": True, "sandbox": sandbox})
    output({"project": str(root), "enabled": True,
            "invoke": "$spark-workers <your task>",
            "shell": "foundry run 'your task'",
            "execution": sandbox,
            "note": "Open a new Codex session in this project to discover the skill. Native subagents remain the default."})


def command_disable(args):
    root = project(args.project)
    active = [t["id"] for t in tasks(root) if t["status"] not in TERMINAL]
    if active:
        raise Error(f"Cancel or finish active tasks first: {', '.join(active)}")
    for rel in ("SKILL.md", "agents/openai.yaml"):
        path = root / SKILL / rel
        if path.exists() and path.read_bytes() != (APP / "templates" / rel).read_bytes():
            raise Error(f"{path} was customized; preserve it manually before disabling.")
    for rel in ("SKILL.md", "agents/openai.yaml"):
        (root / SKILL / rel).unlink(missing_ok=True)
    for directory in (root / SKILL / "agents", root / SKILL):
        with contextlib.suppress(OSError):
            directory.rmdir()
    (root / MARKER).unlink()
    output({"project": str(root), "enabled": False, "note": "Existing worker worktrees and logs are retained."})


def command_submit(args):
    root = project(args.project)
    assignment = prompt_from(args)
    task_id = uuid.uuid4().hex[:12]
    folder = project_state(root) / "tasks" / task_id
    folder.mkdir(parents=True, mode=0o700)
    with locked(project_state(root) / "submit.lock"):
        commit, workspace = snapshot(root, folder)
        config = json.loads((root / MARKER).read_text())
        task = {"id": task_id, "name": args.name, "project": str(root), "workspace": str(workspace),
                "snapshot": commit, "status": "queued", "assignment": assignment,
                "created_at": now(), "updated_at": now(), "timeout_seconds": args.timeout,
                "thread_id": None, "applied": False, "attempt": 1,
                "sandbox": config.get("sandbox", "workspace-write")}
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
        (path.parent / "cancel").unlink(missing_ok=True)
        report = path.parent / "report.txt"
        if report.exists():
            report.replace(path.parent / f"report-attempt-{task['attempt']}.txt")
        config = json.loads((root / MARKER).read_text())
        update_task(path, status="queued", assignment=text, attempt=task["attempt"] + 1,
                    sandbox=config.get("sandbox", "workspace-write"),
                    started_at=None, finished_at=None, error=None, turn_id=None)
        output(launch(path))


def command_status(args):
    root = project(args.project)
    if args.task_id:
        output(read_task(state_path(root, args.task_id)))
    else:
        output([{k: t.get(k) for k in ("id", "name", "status", "attempt", "applied", "updated_at", "error")}
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
    profile = model_profile()
    provider = profile["model_provider"]
    base_url = profile["model_providers"][provider]["base_url"]
    parsed = urllib.parse.urlsplit(base_url)
    health_url = SETTINGS.get("health_url", urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", "")))
    checks = {"project_enabled": True, "explicit_only": "allow_implicit_invocation: false" in
              (root / SKILL / "agents/openai.yaml").read_text(),
              "codex_binary": CODEX.is_file(), "worker_profile": PROFILE.is_file(),
              "initial_commit": git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0}
    if health_url:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as response:
                checks["local_endpoint"] = 200 <= response.status < 300
        except (OSError, ValueError):
            checks["local_endpoint"] = False
    output({"project": str(root), "ok": all(checks.values()), "checks": checks,
            "inference_slots": 1, "profile": str(PROFILE),
            "execution": config.get("sandbox", "workspace-write"),
            "model": profile["model"], "provider": provider,
            "note": "Health checks do not run inference or prove tool compatibility. Validate a small real task before larger work."})
    if not all(checks.values()):
        raise SystemExit(1)


def command_run(args):
    root = project(args.project)
    prompt = prompt_from(args)
    os.chdir(root)
    command = [str(CODEX), "--cd", str(root)]
    if getattr(args, "model", None):
        command.extend(["--model", args.model])
    if args.full_access:
        command.extend(["--sandbox", "danger-full-access", "--ask-for-approval", "never"])
    os.execv(CODEX, [*command, "$spark-workers " + prompt])


def command_configure(args):
    profile = Path(args.profile).expanduser().resolve()
    model_profile(profile)
    settings = dict(SETTINGS)
    settings["worker_profile"] = str(profile)
    if args.codex:
        binary = shutil.which(args.codex) or args.codex
        settings["codex_binary"] = str(Path(binary).expanduser().resolve())
    if args.health_url is not None:
        settings["health_url"] = args.health_url
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items())
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x") as f:
            os.chmod(tmp, 0o600)
            f.write(content)
        tmp.replace(CONFIG_FILE)
    finally:
        tmp.unlink(missing_ok=True)
    output({"config": str(CONFIG_FILE), "worker_profile": str(profile),
            "note": "Only Frontier Foundry's own configuration was written. Normal Codex settings are unchanged."})


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
    parser = argparse.ArgumentParser(description="Frontier Foundry: explicit local-model workers for Codex.",
                                     epilog="Quick start: spark-workers enable; then in Codex: $spark-workers <task>")
    parser.add_argument("--version", action="version", version="Frontier Foundry 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    actions = {
        "configure": (command_configure, "Select your local worker profile without changing normal Codex settings"),
        "enable": (command_enable, "Enable this project; native Codex agents stay the default"),
        "disable": (command_disable, "Remove this project's integration"),
        "doctor": (command_doctor, "Check project opt-in and local provider health"),
        "run": (command_run, "Open normal Codex as operator with Spark workers explicitly invoked"),
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
        if name == "run":
            sub.add_argument("--full-access", action="store_true", help="Explicitly use full-access permissions for this operator session too (needed when host sandboxing is unavailable)")
            sub.add_argument("--model", help="Frontier operator model; omitted preserves your normal Codex model")
        if name == "configure":
            sub.add_argument("--profile", required=True, help="Path to a Codex TOML profile with a custom local Responses provider")
            sub.add_argument("--codex", help="Codex executable name or path (default: PATH)")
            sub.add_argument("--health-url", help="Optional health endpoint; an empty value disables the health request")
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
    except (Error, OSError, ValueError) as exc:
        print(f"spark-workers: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
