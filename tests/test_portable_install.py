"""Portable install and process-level lifecycle checks; no GPU or model API."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


installer = module("foundry_installer", "install.py")
catalog = module("foundry_catalog", "make_model_catalog.py")
bridge = module("portable_bridge", "spark_workers.py")


class PortableTests(unittest.TestCase):
    def test_installed_operator_instructions_need_no_project_or_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            installer.install(folder / "prefix")
            env = dict(os.environ, OVERSEER_CONFIG=str(folder / "settings.toml"),
                       OVERSEER_CODEX=str(folder / "no-codex"))
            result = subprocess.run([str(folder / "prefix/bin/overseer"), "instructions"],
                                    cwd=folder, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("overseer submit", result.stdout)
            self.assertIn("overseer followup", result.stdout)
            self.assertIn("overseer apply", result.stdout)
            self.assertIn("repeat the wait", result.stdout)
            self.assertNotIn("$overseer", result.stdout)
            self.assertNotIn("Codex", result.stdout)

    def test_installer_refuses_foreign_command_before_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / "bin").mkdir()
            foreign = prefix / "bin/spark-workers"
            foreign.write_text("an unrelated existing command\n")
            with self.assertRaises(RuntimeError):
                installer.install(prefix)
            self.assertEqual(foreign.read_text(), "an unrelated existing command\n")
            self.assertFalse((prefix / "share/frontier-foundry").exists())
            self.assertFalse((prefix / "bin/overseer").exists())

    def test_generated_catalog_uses_local_alias_without_mutating_input(self):
        bundled = {"models": [{"slug": "bundled", "context_window": 100000,
                               "tool_mode": "code_mode_only", "base_instructions": "runtime instructions"}]}
        original = json.loads(json.dumps(bundled))
        output = catalog.build_catalog(bundled, "my-local-model", 32768)
        self.assertEqual(output["models"][0]["slug"], "my-local-model")
        self.assertEqual(output["models"][0]["max_context_window"], 32768)
        self.assertEqual(output["models"][0]["input_modalities"], ["text"])
        self.assertEqual(bundled, original)

    def test_worker_profile_rejects_builtin_openai_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "worker.toml"
            profile.write_text('model = "test"\nmodel_provider = "openai"\n')
            with self.assertRaises(bridge.Error):
                bridge.model_profile(profile)

    def test_worker_profile_rejects_chat_completions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "worker.toml"
            profile.write_text('model = "test"\nmodel_provider = "custom"\n[model_providers.custom]\nbase_url = "http://localhost/v1"\nwire_api = "chat"\n')
            with self.assertRaises(bridge.Error):
                bridge.model_profile(profile)

    def test_clean_installed_cli_lifecycle_with_custom_model(self):
        with tempfile.TemporaryDirectory(prefix="foundry portable ") as tmp:
            folder = Path(tmp)
            prefix = folder / "install prefix"
            installer.install(prefix)
            installer.install(prefix)  # Upgrading owned links is idempotent.
            self.assertTrue((prefix / "bin/overseer").is_symlink())
            binary = prefix / "bin/overseer"
            fake_codex = folder / "fake-codex"
            fake_codex.write_text('''#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import uuid

if sys.argv[1:4] == ["debug", "models", "--bundled"]:
    print(json.dumps({"models": [{"slug": "bundled", "tool_mode": "code_mode_only", "base_instructions": "Test harness instructions"}]}))
    raise SystemExit(0)
if "app-server" not in sys.argv:
    raise SystemExit("Unexpected fake Codex command")

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params", {})
    if "id" not in request:
        continue
    if method == "initialize":
        result = {}
    elif method in ("thread/start", "thread/resume"):
        assert "Implementation workspace: " + params["cwd"] in params["developerInstructions"]
        assert "Original checkout (do not edit" in params["developerInstructions"]
        thread_id = params.get("threadId", str(uuid.uuid4()))
        result = {"thread": {"id": thread_id}, "model": params["model"], "modelProvider": params["modelProvider"]}
    elif method == "turn/start":
        turn_id = str(uuid.uuid4())
        text = params["input"][0]["text"]
        value = "revised" if "follow-up" in text else "created"
        Path("implementation.txt").write_text(value + "\\n")
        print(json.dumps({"id": request["id"], "result": {"turn": {"id": turn_id}}}), flush=True)
        print(json.dumps({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": {"type": "agentMessage", "text": "Worker " + value + " implementation.txt"}}}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}}}), flush=True)
        continue
    else:
        raise SystemExit("Unexpected method: " + method)
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
''')
            fake_codex.chmod(0o755)
            env = os.environ.copy()
            env.update(SPARK_WORKERS_CONFIG=str(folder / "settings.toml"),
                       SPARK_WORKERS_STATE_DIR=str(folder / "state"),
                       CODEX_HOME=str(folder / "operator-home"))
            env.pop("SPARK_WORKERS_PROFILE", None)
            env.pop("SPARK_WORKERS_CODEX", None)
            operator_home = Path(env["CODEX_HOME"])
            operator_home.mkdir()
            original_config = b'model = "frontier-operator"\n'
            (operator_home / "config.toml").write_bytes(original_config)
            profile = folder / "worker.config.toml"
            subprocess.run([str(prefix / "bin/overseer-catalog"), "--codex", str(fake_codex),
                            "--model", "tiny-local-model", "--context-window", "32768",
                            "--output", str(folder / "catalog.json"), "--profile-output", str(profile)],
                           env=env, capture_output=True, text=True, check=True, timeout=15)
            project = folder / "test project"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            (project / "README.md").write_text("Original project\n")
            subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@localhost",
                            "commit", "-qm", "fixture"], check=True)
            (project / "user-notes.txt").write_text("Keep this uncommitted file\n")

            def cli(*args):
                result = subprocess.run([str(binary), *args], cwd=project, env=env,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            cli("configure", "--profile", str(profile), "--codex", str(fake_codex), "--health-url", "")
            cli("enable")
            marker = json.loads((project / ".codex/spark-workers.json").read_text())
            self.assertEqual(marker["sandbox"], "workspace-write")
            self.assertTrue(cli("doctor")["ok"])
            task = cli("submit", "Implement this self-contained test task")
            task_id = task["id"]
            task = cli("wait", task_id, "--seconds", "10")
            self.assertEqual(task["status"], "completed", task)
            first_thread = task["thread_id"]
            result = cli("result", task_id)
            self.assertIn("Worker created", result["report"])
            renamed_env = dict(env, OVERSEER_CONFIG=env["SPARK_WORKERS_CONFIG"],
                               OVERSEER_STATE_DIR=env["SPARK_WORKERS_STATE_DIR"],
                               SPARK_WORKERS_CONFIG=str(folder / "wrong-config.toml"),
                               SPARK_WORKERS_STATE_DIR=str(folder / "wrong-state"))
            for alias in ("foundry", "spark-workers"):
                legacy = subprocess.run([str(prefix / "bin" / alias), "result", task_id],
                                        cwd=project, env=renamed_env, capture_output=True, text=True, timeout=15)
                self.assertEqual(legacy.returncode, 0, legacy.stderr)
                self.assertEqual(json.loads(legacy.stdout)["id"], task_id)
            self.assertEqual(result["model"], "tiny-local-model")
            self.assertEqual(result["provider"], "local_worker")
            self.assertFalse((project / "implementation.txt").exists())
            profile.write_text('model = "changed-after-submit"\n')
            cli("followup", task_id, "Apply this follow-up correction", "--timeout", "10")
            task = cli("wait", task_id, "--seconds", "10")
            self.assertEqual(task["status"], "completed", task)
            self.assertEqual(task["thread_id"], first_thread)
            self.assertEqual(task["timeout_seconds"], 10)
            self.assertEqual(task["model"], "tiny-local-model")
            diff = subprocess.run([str(binary), "diff", task_id], cwd=project, env=env,
                                  capture_output=True, text=True, check=True)
            self.assertIn("+revised", diff.stdout)
            self.assertTrue(cli("apply", task_id)["applied"])
            self.assertEqual((project / "implementation.txt").read_text(), "revised\n")
            self.assertEqual((project / "user-notes.txt").read_text(), "Keep this uncommitted file\n")
            self.assertEqual((operator_home / "config.toml").read_bytes(), original_config)
            self.assertEqual(subprocess.check_output(["git", "-C", str(project), "diff", "--cached"]), b"")
            cli("disable")
            self.assertFalse((project / ".codex/spark-workers.json").exists())


if __name__ == "__main__":
    unittest.main()
