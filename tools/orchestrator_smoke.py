#!/usr/bin/env python3
"""Live operator -> Overseer -> worker check in a disposable project.

Both names select entries in an existing Overseer configuration. All inference
uses those entries' explicit endpoints and models. No live services are changed.
Evidence contains private provider configuration; do not commit the directory.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import spark_workers as bridge
from install import install
from worker_harnesses import ADAPTERS


def run(command, **kwargs):
    return subprocess.run([str(arg) for arg in command], capture_output=True,
                          text=True, check=True, timeout=30, **kwargs)


ORIGINAL = "def clamp(value, low, high):\n    raise NotImplementedError\n"
TESTS = '''import unittest
from clamp import clamp
class Tests(unittest.TestCase):
    def test_inside(self): self.assertEqual(clamp(4, 0, 10), 4)
    def test_below(self): self.assertEqual(clamp(-4, 0, 10), 0)
    def test_above(self): self.assertEqual(clamp(40, 0, 10), 10)
    def test_float(self): self.assertEqual(clamp(1.5, 0, 2), 1.5)
    def test_equal_bounds(self): self.assertEqual(clamp(40, 2, 2), 2)
'''


def audit_wrapper(binary, base, project):
    """Record actual CLI calls and whether the original was untouched at apply."""
    directory = base / "audit-bin"
    directory.mkdir()
    wrapper = directory / "overseer"
    wrapper.write_text("#!/usr/bin/env python3\n" +
        "import json,subprocess,sys,time\nfrom pathlib import Path\n" +
        f"binary={str(binary)!r}\nbase=Path({str(base)!r})\nproject=Path({str(project)!r})\n" +
        f"original={ORIGINAL!r}\n" + '''
args=sys.argv[1:]
record={"args":args,"started":time.time()}
if args and args[0]=="apply":
    record["original_untouched_before_apply"]=(project/"clamp.py").read_text()==original
    (base/"apply-started").touch()
p=subprocess.run([binary,*args],capture_output=True,text=True)
if args and args[0]=="apply" and p.returncode:
    (base/"apply-started").unlink(missing_ok=True)
record.update(returncode=p.returncode,stdout=p.stdout,stderr=p.stderr,finished=time.time())
with (base/"cli-events.jsonl").open("a") as stream:
    stream.write(json.dumps(record)+"\\n")
sys.stdout.write(p.stdout);sys.stderr.write(p.stderr)
raise SystemExit(p.returncode)
''')
    wrapper.chmod(0o700)
    return directory


def operation_events(events):
    return [e for e in events if e["returncode"] == 0 and e["args"]
            and not any(flag in e["args"] for flag in ("--help", "-h", "--version"))]


def verify(base, project, operator, worker, index, elapsed):
    assert not (base / "original-mutated-before-apply").exists(), "Original implementation changed before apply"
    events = [json.loads(line) for line in (base / "cli-events.jsonl").read_text().splitlines()]
    successful = operation_events(events)
    verbs = [e["args"][0] for e in successful]
    for required in ("doctor", "submit", "wait", "result", "diff", "followup", "apply"):
        assert required in verbs, f"Operator did not successfully call {required}"
    tasks = list((base / "state/projects").glob("*/tasks/*/task.json"))
    assert len(tasks) == 1, f"Expected one delegated job, found {len(tasks)}"
    job = json.loads(tasks[0].read_text())
    assert job["harness"] == worker["harness"] and job["model"] == worker["model"]
    assert 2 <= job["attempt"] <= 3 and job["applied"] and job["status"] == "completed", job["status"]
    sessions = []
    failed_attempts = set()
    for e in successful:
        if e["args"][0] in {"wait", "result"}:
            result = json.loads(e["stdout"])
            if result["status"] == "completed":
                sessions.append((result["attempt"], result["resume_ref"]))
            elif result["status"] == "failed":
                failed_attempts.add(result["attempt"])
                assert result["attempt"] == 2 and any(word in str(result.get("error", "")).lower()
                    for word in ("time limit", "timeout")), "Only one timed-out correction may be resumed"
    assert {1, job["attempt"]}.issubset({attempt for attempt, _ in sessions}), "Operator did not collect initial and final attempts"
    assert len({session for _, session in sessions}) == 1, "Follow-up changed worker conversation"
    followup = verbs.index("followup")
    last_followup = max(i for i, verb in enumerate(verbs) if verb == "followup")
    application = verbs.index("apply")
    full_diffs = [i for i, event in enumerate(successful) if event["args"][0] == "diff"
                  and "--stat" not in event["args"] and "diff --git" in event["stdout"]]
    assert "result" in verbs[:followup] and any(i < followup for i in full_diffs), "First attempt was not reviewed"
    assert "result" in verbs[last_followup:application] and any(last_followup < i < application for i in full_diffs), "Correction was not reviewed"
    assert all(e.get("original_untouched_before_apply", True) for e in successful), "Operator edited original implementation before apply"
    assert (project / "test_clamp.py").read_text() == TESTS, "Original acceptance tests were changed"
    assert (project / "operator-notes.txt").read_text() == "Preserve this staged note.\n"
    assert run(["git", "diff", "--cached", "--binary"], cwd=project).stdout == index, "User index changed"
    workspace = Path(job["workspace"])
    changed = run(["git", "diff", "--name-only", job["snapshot"]], cwd=workspace).stdout.splitlines()
    new = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace).stdout.splitlines()
    assert set(changed + new) == {"clamp.py", "test_invalid_bounds.py"}, "Unexpected worker files"
    tests = run([sys.executable, "-m", "unittest", "-v"], cwd=project)
    (base / "final-tests.txt").write_text(tests.stdout + tests.stderr)
    count = re.search(r"Ran (\d+) tests?", tests.stdout + tests.stderr)
    assert count and int(count[1]) >= 6, "The correction did not add a discovered regression test"
    run([sys.executable, "-c", "from clamp import clamp\ntry: clamp(1, 5, 0)\nexcept ValueError: pass\nelse: raise AssertionError('invalid bounds accepted')"], cwd=project)
    progress = [json.loads(line) for line in (base / "operator.progress.jsonl").read_text().splitlines()]
    identity = json.loads((base / "operator-identity.json").read_text())
    operator_sessions = {e["resume_ref"] for e in [identity, *progress] if e.get("resume_ref")}
    assert len(operator_sessions) == 1, "Operator session identity was missing or changed"
    operator_turns = sum(e["type"] == "finished" for e in progress)
    assert 1 <= operator_turns <= 3, "Operator exceeded the bounded conversation check"
    report = {"operator": operator["harness"], "worker": worker["harness"],
              "operator_model": operator["model"], "worker_model": worker["model"],
              "passed": True, "same_worker_session": True, "worker_attempts": job["attempt"],
              "recovered_timeout_attempts": sorted(failed_attempts),
              "failed_cli_calls": sum(e["returncode"] != 0 for e in events),
              "operator_turns": operator_turns, "same_operator_session": True,
              "reviewed_both_attempts": True, "applied": True,
              "original_untouched_until_apply": True, "index_preserved": True,
              "acceptance_checks_passed": True, "tests_run": int(count[1]),
              "elapsed_seconds": round(elapsed, 1),
              "cli_calls": verbs}
    (base / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Private Overseer worker registry")
    parser.add_argument("--operator", required=True, help="Registry entry used as the operator's harness/model")
    parser.add_argument("--worker", required=True, help="Registry entry for the delegated worker")
    parser.add_argument("--full-access", action="store_true", help="Explicitly permit both harnesses to execute tools without a workspace sandbox")
    parser.add_argument("--output", type=Path, help="New private evidence directory")
    parser.add_argument("--timeout", type=int, default=1200, help="Overall deadline in seconds")
    parser.add_argument("--worker-timeout", type=int, default=480, help="Per-worker turn deadline, 1-3600 seconds (default: 480)")
    parser.add_argument("--operator-turns", type=int, default=3, choices=(1, 2, 3), help="Maximum turns in the same operator conversation; 1 requires one-turn completion")
    args = parser.parse_args()
    if not args.full_access:
        parser.error("This live operator test requires explicit --full-access")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if not 1 <= args.worker_timeout <= 3600:
        parser.error("--worker-timeout must be between 1 and 3600")
    os.umask(0o077)
    settings = tomllib.loads(args.config.expanduser().read_text())
    operator = bridge.select_worker(args.operator, settings)
    worker = bridge.select_worker(args.worker, settings)
    base = args.output.expanduser().resolve() if args.output else Path(tempfile.mkdtemp(prefix="overseer-operator-"))
    if args.output:
        base.mkdir(mode=0o700)
    print(f"Evidence: {base}", flush=True)
    started = time.monotonic()
    install(base / "install")
    binary = base / "install/bin/overseer"
    project = base / "project"
    project.mkdir()
    run(["git", "init", "-q", project])
    for name, content in {"clamp.py": ORIGINAL, "test_clamp.py": TESTS, ".gitignore": "__pycache__/\n"}.items():
        (project / name).write_text(content)
    run(["git", "add", "."], cwd=project)
    run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"], cwd=project)
    (project / "operator-notes.txt").write_text("Preserve this staged note.\n")
    run(["git", "add", "operator-notes.txt"], cwd=project)
    index = run(["git", "diff", "--cached", "--binary"], cwd=project).stdout
    (base / "initial-index.patch").write_text(index)
    # Resolve source configuration in the caller's context before private homes
    # are introduced. Credentials remain private and retain their env references.
    settings = {"workers": {args.worker: worker}}
    if worker["harness"] == "codex" and args.worker == "codex-local":
        settings.update(worker_profile=worker["profile"], codex_binary=worker["binary"])
    bridge.CONFIG_FILE = base / "settings.toml"
    bridge.write_settings(settings)
    bridge.STATE = base / "state"
    audit_bin = audit_wrapper(binary, base, project)
    env = os.environ.copy()
    for name in ("SPARK_WORKERS_CONFIG", "SPARK_WORKERS_STATE_DIR", "SPARK_WORKERS_PROFILE", "SPARK_WORKERS_CODEX",
                 "OVERSEER_CONFIG", "OVERSEER_STATE_DIR", "OVERSEER_PROFILE", "OVERSEER_CODEX"):
        env.pop(name, None)
    env.update(OVERSEER_CONFIG=str(base / "settings.toml"), OVERSEER_STATE_DIR=str(base / "state"),
               PATH=str(audit_bin) + os.pathsep + env.get("PATH", ""))
    run([binary, "enable", "--worker", args.worker, "--sandbox", "danger-full-access"], cwd=project, env=env)
    instructions = run([binary, "instructions"], env=env).stdout
    assignments = base / "assignments"
    assignments.mkdir()
    prompt = f"""You are the OPERATOR, not the implementation worker. This is a live Overseer integration check.
Project: {project}
Use {audit_bin / 'overseer'} for ALL job operations. Login shells may reset PATH;
the absolute executable above is the fresh audited installation, not the global command.
Save assignments only under {assignments}.
Do not implement clamp yourself or edit original project files. You may write assignment text files.
Use relative file paths in worker assignments, never the original checkout's absolute paths.
Run doctor, then submit exactly ONE job using the project's configured worker ({args.worker}).
Initial assignment: Implement clamp(value, low, high) in clamp.py with inclusive bounds.
Keep test_clamp.py and operator-notes.txt unchanged. Actually edit code, run python3 -m unittest -v,
report real results, and do not commit. Change no other files.
Wait for completion (repeat waits while queued/running). Read result and diff before proceeding.
If the initial worker attempt fails, stop and report its concrete error. Never apply failed work.
Then ALWAYS follow up the SAME job, even if the initial implementation already handles invalid bounds:
Also raise ValueError for low > high and add test_invalid_bounds.py with a unittest for it.
Keep test_clamp.py and operator-notes.txt unchanged. Run tests and report real results. Do not commit.
Wait again; read result and diff again. Apply only after the second completed attempt is reviewed.
If the correction times out, you may resume that SAME job once, preserving its
worker and worktree. Read the timeout report, allow a longer turn if needed, and
collect and review the final successful result and diff before apply. Stop on
other failures or after that one recovery. Report any timeout/recovery honestly.
Run python3 -m unittest -v in the original project after application. Summarize the real outcome.
Do not submit another job, change workers, run native subagents, or manage services.
Pass --timeout {args.worker_timeout} to submit. Follow-up keeps that limit unless explicitly changed.
"""
    (base / "operator-prompt.txt").write_text(prompt)
    folder = base / "operator"
    folder.mkdir()
    frozen = bridge.freeze_worker(operator, folder, "danger-full-access", instructions=instructions)
    task = {**frozen, "workspace": str(project), "project": str(project), "sandbox": "danger-full-access", "assignment": prompt}
    previous = os.environ.copy()
    rpc = None
    status = "failed"
    error = None
    try:
        os.environ.clear()
        os.environ.update(env)
        if operator["harness"] == "codex":
            # The operator's private home must be separate from the bridge's
            # per-project Codex worker home, including for Codex -> Codex.
            home = folder / "codex-home"
            home.mkdir(mode=0o700)
            operator_env = dict(env, CODEX_HOME=str(home))
        else:
            operator_env = bridge.harness_environment(task)
        streaming = operator["harness"] in {"claude", "opencode"}
        kwargs = {"dialect": "pi"} if operator["harness"] in {"pi", "omp"} else {}
        deadline = time.monotonic() + args.timeout
        last_update = 0
        for operator_turn in range(1, args.operator_turns + 1):
            if rpc is None or streaming:
                if rpc:
                    rpc.close()
                command = bridge.harness_command(task, instructions=instructions)
                rpc = bridge.Rpc(command, project, base / "operator.stderr.log", base / "operator.events.jsonl", env=operator_env, **kwargs)
                adapter = ADAPTERS[operator["harness"]](rpc, task)
                identity = adapter.start(instructions)
                if identity.get("resume_ref"):
                    task["resume_ref"] = identity["resume_ref"]
                (base / "operator-identity.json").write_text(json.dumps(identity, indent=2) + "\n")
            if hasattr(adapter, "last_message"):
                adapter.last_message = None
            adapter.prompt()
            pending = rpc.pending
            rpc.pending = []
            while time.monotonic() < deadline:
                if not (base / "apply-started").exists() and (project / "clamp.py").read_text() != ORIGINAL:
                    (base / "original-mutated-before-apply").write_text("Original implementation changed before reviewed apply.\n")
                    raise AssertionError("Original implementation changed before reviewed apply")
                if time.monotonic() - last_update > 20:
                    print(f"{operator['harness']} -> {worker['harness']}: operator turn {operator_turn} ({round(time.monotonic()-started)}s)", flush=True)
                    last_update = time.monotonic()
                try:
                    record = pending.pop(0) if pending else rpc.receive()
                    event = adapter.event(record)
                except queue.Empty:
                    continue
                except EOFError:
                    if not streaming:
                        raise
                    event = adapter.finish()
                if event:
                    if event.get("resume_ref"):
                        if task.get("resume_ref") and task["resume_ref"] != event["resume_ref"]:
                            raise AssertionError("Operator changed its native conversation")
                        task["resume_ref"] = event["resume_ref"]
                    event["operator_turn"] = operator_turn
                    with (base / "operator.progress.jsonl").open("a") as stream:
                        stream.write(json.dumps(event) + "\n")
                    if event["type"] == "finished":
                        status, error = event["status"], event.get("error")
                        break
            else:
                adapter.interrupt()
                raise TimeoutError("Operator exceeded its deadline")
            if status != "completed":
                raise RuntimeError(f"Operator failed: {error}")
            jobs = [bridge.read_task(p) for p in (base / "state/projects").glob("*/tasks/*/task.json")]
            if any(job.get("applied") for job in jobs):
                break
            if any(job["status"] in {"failed", "cancelled", "orphaned"} and
                   not (job["attempt"] == 2 and "time limit" in str(job.get("error", "")).lower()) for job in jobs):
                raise RuntimeError("Worker failed; inspect retained task state")
            if operator_turn < args.operator_turns:
                state = [{k: j.get(k) for k in ("id", "status", "attempt", "applied")} for j in jobs]
                job_instruction = ("Continue the existing worker job; do not create another job. " if jobs else
                    "No worker job has been submitted yet. Submit the one initial assignment now; "
                    "do not search other projects or global job history. ")
                task["assignment"] = ("Continue the original Overseer task in this same operator conversation. "
                    "Your last turn ended before reviewed application and final verification. "
                    "Keep using the same audited CLI, configured worker, model, and permissions. "
                    + job_instruction +
                    "Repeat bounded waits while work is queued/running; do not end merely saying you will wait. "
                    "Complete the mandated correction, collect its result AND current full diff, apply only reviewed "
                    "completed work, and run tests in the original fixture. "
                    "Observed job state: " + json.dumps(state))
                print("Continuing the same native operator session", flush=True)
        report = verify(base, project, operator, worker, index, time.monotonic() - started)
        print(json.dumps(report, indent=2), flush=True)
    finally:
        if rpc:
            rpc.close()
        # Detached children must not outlive a failed test. Retain partial work.
        for path in (base / "state/projects").glob("*/tasks/*/task.json"):
            job = bridge.read_task(path)
            if job["status"] not in bridge.TERMINAL:
                (path.parent / "cancel").touch()
        os.environ.clear()
        os.environ.update(previous)


if __name__ == "__main__":
    main()
