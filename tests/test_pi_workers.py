"""Pi adapter contract and installed CLI tests; no model or GPU required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_portable_install import installer, ROOT
from worker_harnesses import Error, PiWorker


class PiContractTests(unittest.TestCase):
    def worker(self):
        return PiWorker(None, {})

    def test_waits_for_settled_after_retry_and_uses_latest_assistant_result(self):
        worker = self.worker()
        worker.event({"type": "message_end", "message": {"role": "assistant", "content": [],
                      "stopReason": "error", "errorMessage": "temporary overload"}})
        self.assertIsNone(worker.event({"type": "agent_end", "willRetry": True}))
        worker.event({"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Fixed and tested"}], "stopReason": "stop"}})
        self.assertEqual(worker.event({"type": "agent_settled", "aborted": False})["status"], "completed")

    def test_provider_failure_abort_and_missing_final_response_are_not_success(self):
        for message, aborted in (({"stopReason": "error", "errorMessage": "bad API"}, False),
                                 ({"stopReason": "stop"}, True), ({}, False)):
            with self.subTest(message=message, aborted=aborted):
                worker = self.worker()
                worker.last_message = message
                self.assertEqual(worker.event({"type": "agent_settled", "aborted": aborted})["status"], "failed")

    def test_handled_prompt_fails_without_waiting(self):
        class Rpc:
            def call(self, *args):
                return {"disposition": "handled"}
        with self.assertRaisesRegex(Error, "without starting"):
            PiWorker(Rpc(), {"assignment": "task"}).prompt()

    def test_unattended_extension_ui_fails(self):
        with self.assertRaisesRegex(Error, "extension UI"):
            self.worker().event({"type": "extension_ui_request"})

    def test_identity_mismatch_rejects_before_prompt(self):
        spec = {"model": "qwen", "provider": "local", "api": "openai-completions", "base_url": "http://localhost/v1"}
        for key in ("id", "provider", "api", "baseUrl"):
            with self.subTest(field=key):
                model = {"id": "qwen", "provider": "local", "api": "openai-completions", "baseUrl": "http://localhost/v1"}
                model[key] = "unexpected"
                class Rpc:
                    def call(self, method, **kwargs):
                        if method != "get_state":
                            raise AssertionError("Inference must not start")
                        return {"model": model, "sessionFile": "/private/session"}
                with self.assertRaisesRegex(Error, "refusing inference"):
                    PiWorker(Rpc(), {"worker_config": spec}).start("instructions")


FAKE_PI = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
required = ["--no-extensions", "--no-mcp", "--offline", "--no-skills", "--no-prompt-templates",
            "--no-context-files", "--no-approve"]
assert all(flag in args for flag in required), args
assert os.environ["PI_OFFLINE"] == "1"
home = Path(os.environ["PI_CODING_AGENT_DIR"])
assert (home / "models.json").is_file()
assert not (home / "auth.json").exists()
provider = args[args.index("--provider")+1]
model = args[args.index("--model")+1]
session = Path(args[args.index("--session")+1]) if "--session" in args else Path(args[args.index("--session-dir")+1]) / "persistent.jsonl"
session.parent.mkdir(parents=True, exist_ok=True)
session.touch()
def emit(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    cmd = req["type"]
    data = {}
    if cmd == "get_state":
        data = {"model": {"id": model, "provider": provider, "api": "openai-completions",
                         "baseUrl": "http://127.0.0.1:9999/v1"}, "sessionFile": str(session)}
    elif cmd == "prompt":
        text = req["message"]
        with session.open("a") as stream:
            stream.write(text + "\\n")
        if "disconnect" in text:
            raise SystemExit(0)
        if "hang" not in text:
            Path("implementation.txt").write_text("revised\\n" if "follow-up" in text else "created\\n")
            # Events can precede the command acknowledgment. Unicode separators are valid JSON text.
            emit({"type": "message_end", "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Implemented ✓\\u2028and tested"}], "stopReason": "stop"}})
            emit({"type": "tool_execution_end", "toolName": "bash", "isError": False,
                  "result": {"content": [{"type": "text", "text": "Tests passed"}]}})
            emit({"type": "agent_end", "willRetry": False})
            emit({"type": "agent_settled", "aborted": False})
        data = {"disposition": "started"}
    elif cmd == "abort":
        emit({"type": "agent_settled", "aborted": True})
    elif cmd != "clear_queue":
        raise SystemExit("Unexpected command: " + cmd)
    emit({"type": "response", "id": req["id"], "command": cmd, "success": True, "data": data})
'''


class PiInstalledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="foundry pi ")
        self.folder = Path(self.temp.name)
        prefix = self.folder / "install prefix"
        installer.install(prefix)
        self.binary = prefix / "bin/overseer"
        self.fake = self.folder / "fake pi"
        self.fake.write_text(FAKE_PI)
        self.fake.chmod(0o755)
        self.models = self.folder / "models.json"
        self.models.write_text(json.dumps({"providers": {"local": {"baseUrl": "http://127.0.0.1:9999/v1",
            "api": "openai-completions", "apiKey": "unused", "models": [{"id": "local-qwen"}]}}}))
        self.env = os.environ.copy()
        for key in ("SPARK_WORKERS_PROFILE", "SPARK_WORKERS_CODEX"):
            self.env.pop(key, None)
        self.env.update(SPARK_WORKERS_CONFIG=str(self.folder / "settings.toml"),
                        SPARK_WORKERS_STATE_DIR=str(self.folder / "state"))
        self.project = self.folder / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", self.project], check=True)
        (self.project / "original.txt").write_text("keep me\n")
        subprocess.run(["git", "-C", self.project, "add", "."], check=True)
        subprocess.run(["git", "-C", self.project, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                        "commit", "-qm", "fixture"], check=True)
        self.cli("configure", "--worker", "pi-local", "--harness", "pi", "--pi", str(self.fake),
                 "--models", str(self.models), "--provider", "local", "--model", "local-qwen", "--health-url", "")
        self.cli("enable")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, ok=True):
        result = subprocess.run([str(self.binary), *args], cwd=self.project, env=self.env,
                                capture_output=True, text=True, timeout=20)
        if not ok:
            self.assertNotEqual(result.returncode, 0)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def select_pi(self):
        self.cli("enable", "--worker", "pi-local", "--sandbox", "danger-full-access")

    def test_opt_in_permissions_and_no_silent_downgrade(self):
        self.assertIn("no built-in workspace sandbox", self.cli("submit", "--worker", "pi-local", "task", ok=False))
        self.assertFalse(list((self.folder / "state").glob("projects/*/tasks/*/workspace")))
        self.select_pi()
        doctor = self.cli("doctor")
        self.assertEqual((doctor["worker"], doctor["harness"]), ("pi-local", "pi"))
        self.assertTrue(doctor["ok"])

    def test_registering_codex_preserves_pi_and_keeps_codex_as_default(self):
        profile = self.folder / "codex.toml"
        profile.write_text('model = "local-codex-model"\nmodel_provider = "custom"\n'
                           '[model_providers.custom]\nbase_url = "http://127.0.0.1:9999/v1"\nwire_api = "responses"\n')
        self.cli("configure", "--profile", str(profile), "--codex", sys.executable, "--health-url", "")
        # Reconfiguring a worker preserves its explicit disabled health endpoint.
        self.cli("configure", "--profile", str(profile), "--codex", sys.executable)
        doctor = self.cli("doctor")
        self.assertEqual((doctor["harness"], doctor["model"]), ("codex", "local-codex-model"))
        self.select_pi()
        self.assertEqual(self.cli("doctor")["harness"], "pi")
        self.assertEqual(self.cli("doctor", "--worker", "codex-local")["harness"], "codex")

    def test_installed_pi_resume_freezes_config_and_reports_diff_for_review(self):
        self.select_pi()
        job = self.cli("submit", "Implement task")
        job = self.cli("wait", job["id"], "--seconds", "10")
        self.assertEqual(job["status"], "completed", job)
        session = job["resume_ref"]
        result = self.cli("result", job["id"])
        self.assertIn("Implemented ✓\u2028and tested", result["report"])
        events = [json.loads(line) for line in Path(result["progress"]).read_text().split("\n") if line]
        self.assertEqual([event["type"] for event in events], ["report", "tool", "finished"])
        self.assertFalse((self.project / "implementation.txt").exists())
        # Break the registered model file and project selection: follow-up must use its frozen worker.
        self.models.write_text("invalid model config")
        marker = self.project / ".codex/spark-workers.json"
        config = json.loads(marker.read_text())
        config.update(worker="missing-worker", sandbox="workspace-write")
        marker.write_text(json.dumps(config))
        self.cli("followup", job["id"], "Apply follow-up")
        revised = self.cli("wait", job["id"], "--seconds", "10")
        self.assertEqual(revised["status"], "completed", revised)
        self.assertEqual(revised["resume_ref"], session)
        self.assertEqual(revised["sandbox"], "danger-full-access")
        self.assertEqual(Path(session).read_text(), "Implement task\nApply follow-up\n")
        self.cli("apply", job["id"])
        self.assertEqual((self.project / "implementation.txt").read_text(), "revised\n")
        self.assertEqual((self.project / "original.txt").read_text(), "keep me\n")
        self.assertEqual(subprocess.check_output(["git", "-C", self.project, "diff", "--cached"]), b"")

    def test_active_cancel_sends_abort_and_retains_partial_state(self):
        self.select_pi()
        job = self.cli("submit", "hang until cancelled")
        self.cli("wait", job["id"], "--seconds", "1")
        self.cli("cancel", job["id"])
        job = self.cli("wait", job["id"], "--seconds", "10")
        self.assertEqual(job["status"], "cancelled", job)
        self.assertTrue(Path(job["workspace"]).exists())

    def test_timeout_and_disconnect_fail(self):
        self.select_pi()
        for prompt, timeout in (("hang", "1"), ("disconnect", "10")):
            with self.subTest(prompt=prompt):
                job = self.cli("submit", prompt, "--timeout", timeout)
                job = self.cli("wait", job["id"], "--seconds", "10")
                self.assertEqual(job["status"], "failed", job)
                self.assertTrue(job["error"])


if __name__ == "__main__":
    unittest.main()
