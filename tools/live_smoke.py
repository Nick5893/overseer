#!/usr/bin/env python3
"""Explicit live-model test in a fresh disposable project. No services are changed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from install import install


def run(command, **kwargs):
    return subprocess.run([str(arg) for arg in command], capture_output=True, text=True,
                          check=True, timeout=30, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("codex", "pi", "omp", "opencode", "claude"), required=True)
    parser.add_argument("--binary", help="Worker harness executable (default: PATH)")
    parser.add_argument("--profile", help="Codex local Responses profile")
    parser.add_argument("--models", help="Pi custom models.json")
    parser.add_argument("--provider", default="local")
    parser.add_argument("--model", default="local-worker")
    parser.add_argument("--base-url", help="Explicit endpoint for OMP/OpenCode/Claude, or Pi without a models file")
    parser.add_argument("--api-key-env", help="Credential environment variable name")
    parser.add_argument("--auth-mode", choices=("api-key", "bearer"), default="api-key")
    parser.add_argument("--full-access", action="store_true", help="Explicitly allow unrestricted worker execution")
    parser.add_argument("--output", type=Path, help="New evidence directory; default: private temporary directory")
    args = parser.parse_args()
    if args.harness != "codex" and not args.full_access:
        parser.error("This harness requires explicit --full-access")
    if ((args.harness == "codex" and not args.profile) or
            (args.harness != "codex" and not args.models and not args.base_url)):
        parser.error("Pass --profile for Codex; otherwise --base-url or --models for Pi/OMP")
    os.umask(0o077)
    if args.output:
        base = args.output.expanduser().resolve()
        base.mkdir(mode=0o700)  # Refuse existing directories.
    else:
        base = Path(tempfile.mkdtemp(prefix="overseer-live-"))
    print(f"Evidence: {base}", flush=True)
    prefix = base / "install prefix"
    install(prefix)
    binary = prefix / "bin/overseer"
    project = base / "project"
    project.mkdir()
    env = os.environ.copy()
    for key in ("SPARK_WORKERS_PROFILE", "SPARK_WORKERS_CODEX", "OVERSEER_PROFILE", "OVERSEER_CODEX"):
        env.pop(key, None)
    env.update(OVERSEER_CONFIG=str(base / "settings.toml"),
               OVERSEER_STATE_DIR=str(base / "state"), CODEX_HOME=str(base / "operator-home"))
    Path(env["CODEX_HOME"]).mkdir()
    operator_config = Path(env["CODEX_HOME"]) / "config.toml"
    operator_config.write_text('# Operator config must remain unchanged.\n')
    original_config = operator_config.read_bytes()

    def cli(*command):
        return json.loads(run([binary, *command], cwd=project, env=env).stdout)

    def wait(job_id):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            job = cli("wait", job_id, "--seconds", "15")
            print(f"{job_id}: {job['status']}", flush=True)
            if job["status"] in {"completed", "failed", "cancelled", "orphaned"}:
                if job["status"] != "completed":
                    raise RuntimeError(f"Live worker failed. Inspect {cli('result', job_id)}")
                return job
        cli("cancel", job_id)
        raise TimeoutError("Live test exceeded its deadline; cancellation requested")

    run(["git", "init", "-q", project])
    (project / "clamp.py").write_text('def clamp(value, low, high):\n    raise NotImplementedError\n')
    tests = '''import unittest
from clamp import clamp
class Tests(unittest.TestCase):
    def test_inside(self): self.assertEqual(clamp(4, 0, 10), 4)
    def test_below(self): self.assertEqual(clamp(-4, 0, 10), 0)
    def test_above(self): self.assertEqual(clamp(40, 0, 10), 10)
    def test_float(self): self.assertEqual(clamp(1.5, 0, 2), 1.5)
    def test_equal_bounds(self): self.assertEqual(clamp(40, 2, 2), 2)
'''
    (project / "test_clamp.py").write_text(tests)
    (project / ".gitignore").write_text('__pycache__/\n')
    run(["git", "-C", project, "add", "."])
    run(["git", "-C", project, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"])
    (project / "operator-notes.txt").write_text('Preserve this operator file.\n')
    run(["git", "-C", project, "add", "operator-notes.txt"])
    original_index = run(["git", "-C", project, "diff", "--cached", "--binary"]).stdout
    configure = ["configure", "--harness", args.harness, "--health-url", ""]
    if args.harness == "codex":
        configure += ["--profile", str(Path(args.profile).expanduser().resolve())]
    else:
        configure += ["--provider", args.provider, "--model", args.model]
        if args.models:
            configure += ["--models", str(Path(args.models).expanduser().resolve())]
        else:
            configure += ["--base-url", args.base_url]
        if args.api_key_env:
            configure += ["--api-key-env", args.api_key_env]
        if args.harness == "claude":
            configure += ["--auth-mode", args.auth_mode]
    if args.binary:
        configure += ["--binary", args.binary]
    cli(*configure)
    name = args.harness + "-local"
    cli("enable", "--worker", name, "--sandbox", "danger-full-access" if args.full_access else "workspace-write")
    job = cli("submit", "Implement clamp(value, low, high) in clamp.py, returning the value clamped to the inclusive bounds. "
              "Keep test_clamp.py and operator-notes.txt unchanged. Read the files, actually edit the code, "
              "run python3 -m unittest -v, and report the real results. Do not commit.", "--timeout", "240")
    job = wait(job["id"])
    first_session = job["resume_ref"]
    first_check = run([sys.executable, "-m", "unittest", "-v"], cwd=job["workspace"])
    (base / "initial-tests.txt").write_text(first_check.stdout + first_check.stderr)
    cli("followup", job["id"], "Also raise ValueError when low > high. Add test_invalid_bounds.py with a unittest for that case. "
        "Keep test_clamp.py and operator-notes.txt unchanged. Actually edit the code, run python3 -m unittest -v, "
        "and report the real result. Do not commit.")
    job = wait(job["id"])
    assert job["resume_ref"] == first_session, "Follow-up changed the session"
    assert (project / "clamp.py").read_text().endswith('raise NotImplementedError\n')
    patch = run([binary, "diff", job["id"]], cwd=project, env=env).stdout
    (base / "reviewed.diff").write_text(patch)
    assert "test_invalid_bounds.py" in patch, "Missing regression test"
    assert (Path(job["workspace"]) / "test_clamp.py").read_text() == tests
    changed = run(["git", "diff", "--name-only", job["snapshot"]], cwd=job["workspace"]).stdout.splitlines()
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=job["workspace"]).stdout.splitlines()
    assert set(changed + untracked) == {"clamp.py", "test_invalid_bounds.py"}, "Unexpected worker changes"
    # Print the bounded fixture patch as evidence before automatic application.
    print(patch, flush=True)
    cli("apply", job["id"])
    final_check = run([sys.executable, "-m", "unittest", "-v"], cwd=project)
    (base / "final-tests.txt").write_text(final_check.stdout + final_check.stderr)
    run([sys.executable, "-c", "from clamp import clamp\ntry: clamp(1, 5, 0)\nexcept ValueError: pass\nelse: raise AssertionError('invalid bounds accepted')"], cwd=project)
    assert (project / "operator-notes.txt").read_text() == 'Preserve this operator file.\n'
    assert run(["git", "-C", project, "diff", "--cached", "--binary"]).stdout == original_index
    assert operator_config.read_bytes() == original_config
    evidence = {"harness": args.harness, "model": job["model"], "provider": job["provider"],
                "job_id": job["id"], "status": job["status"], "attempts": job["attempt"],
                "same_session": True, "applied": True, "index_preserved": True,
                "operator_config_preserved": True, "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()}
    (base / "verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(final_check.stdout + final_check.stderr, flush=True)
    print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
