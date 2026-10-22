"""Native headless/RPC fixtures verify additional harnesses without API access."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_portable_install import installer, bridge
from worker_harnesses import OmpWorker

FAKE = r'''
import json, os, signal, sys, time
from pathlib import Path
args = sys.argv[1:]
assert os.environ["PWD"] == str(Path.cwd()), "Inherited operator PWD escaped the job worktree"
if KIND != "opencode":
    instructions=args[args.index("--append-system-prompt")+1]
    assert "Implementation workspace: " + str(Path.cwd()) in instructions
model = args[args.index("--model")+1]
def emit(record): print(json.dumps(record), flush=True)
def record(text):
    with history.open("a") as stream: stream.write(text + "\n")
    if "error" not in text and "hang" not in text:
        Path("implementation.txt").write_text("revised\n" if "follow-up" in text else "created\n")
if KIND == "omp":
    home = Path(os.environ["PI_CODING_AGENT_DIR"])
    assert "--no-extensions" in args and "--no-ui" in args and "--no-prewalk" in args
    assert (home / "models.yml").exists()
    session = args[args.index("--resume")+1] if "--resume" in args else str(home / "session.jsonl")
    history = Path(session); history.touch()
    prompt_id = None
    for line in sys.stdin:
        req = json.loads(line); cmd = req["type"]; data = {}
        if cmd == "get_state":
            provider, model_id = model.split("/",1)
            data = {"model":{"provider":provider,"id":model_id,"api":"openai-completions","baseUrl":"http://127.0.0.1:9999/v1"},"sessionFile":session}
        elif cmd == "prompt":
            prompt_id = req["id"]; text = req["message"]; record(text)
            emit({"type":"response","id":req["id"],"success":True,"data":{"agentInvoked":True}})
            if "hang" in text: continue
            emit({"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":"Implemented and tested"}]}})
            emit({"type":"agent_end","isTerminal":True,"yielded":True})
            emit({"type":"prompt_result","id":"unrelated","agentInvoked":True,"status":"completed","sessionSettled":True})
            emit({"type":"prompt_result","id":prompt_id,"agentInvoked":True,"status":"error" if "error" in text else "completed","error":"provider failure" if "error" in text else None,"sessionSettled":False})
            emit({"type":"session_settled"})
            continue
        elif cmd == "abort":
            emit({"type":"prompt_result","id":prompt_id,"agentInvoked":True,"status":"aborted","sessionSettled":True})
        else: raise AssertionError(cmd)
        emit({"type":"response","id":req["id"],"success":True,"data":data})
else:
    text = sys.stdin.read()
    if KIND == "claude":
        assert "--bare" in args and "--strict-mcp-config" in args
        if "FOUNDRY_TEST_KEY" in os.environ:
            assert os.environ["ANTHROPIC_API_KEY"] == os.environ["FOUNDRY_TEST_KEY"]
            assert os.environ["ANTHROPIC_CUSTOM_HEADERS"] == "Authorization: Bearer " + os.environ["FOUNDRY_TEST_KEY"]
            assert "CLAUDE_CODE_USE_BEDROCK" not in os.environ
        home=Path(os.environ["CLAUDE_CONFIG_DIR"])
        session=args[args.index("--resume")+1] if "--resume" in args else args[args.index("--session-id")+1]
        emit({"type":"system","subtype":"init","model":"wrong" if "mismatch" in text else model,"session_id":session})
    else:
        assert "--standalone" in args and "--auto" in args
        assert os.environ["OPENCODE_DISABLE_PROJECT_CONFIG"]=="true"
        config=json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        assert "Implementation workspace: " + str(Path.cwd()) in config["agent"]["foundry"]["prompt"]
        assert config["model"]==config["small_model"]==model
        assert config["permission"]["*"]=="deny"
        home=Path(os.environ["XDG_STATE_HOME"]); home.mkdir(parents=True,exist_ok=True)
        session=args[args.index("--session")+1] if "--session" in args else "ses_fixture"
        emit({"type":"step_start","sessionID":session,"part":{}})
    history=home/"history.txt"; record(text)
    if "hang" in text:
        signal.signal(signal.SIGINT,lambda *_:sys.exit(130))
        while True: time.sleep(.05)
    if KIND=="claude":
        emit({"type":"user","message":{"content":[{"type":"tool_result","content":"Tests passed"}]}})
        # Claude may return subtype=success together with is_error=true for API errors.
        emit({"type":"result","subtype":"success","is_error":"error" in text,"result":"Implemented and tested","session_id":session})
    else:
        if "error" in text: emit({"type":"error","sessionID":session,"error":{"message":"provider failure"}})
        emit({"type":"tool_use","sessionID":session,"part":{"state":{"status":"completed","output":"Tests passed"}}})
        emit({"type":"step_finish","sessionID":session,"part":{"reason":"tool-calls"}})
        emit({"type":"step_start","sessionID":session,"part":{}})
        emit({"type":"text","sessionID":session,"part":{"text":"Implemented and tested"}})
        if "omits-finish" not in text: emit({"type":"step_finish","sessionID":session,"part":{"reason":"length" if "truncated" in text else "stop"}})
    if "exit-failure" in text: raise SystemExit(3)
'''


class ExtraHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="foundry extra ")
        self.folder = Path(self.temp.name)
        self.prefix = self.folder / "installed prefix"
        installer.install(self.prefix)
        self.binary = self.prefix / "bin/overseer"
        self.project = self.folder / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / "original.txt").write_text("keep original\n")
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture"], check=True)
        self.env = os.environ.copy()
        self.env.update(SPARK_WORKERS_CONFIG=str(self.folder / "settings.toml"), SPARK_WORKERS_STATE_DIR=str(self.folder / "state"))
        self.env["PWD"] = str(self.project)  # Must be replaced by the worker's actual cwd.
        self.env.pop("SPARK_WORKERS_PROFILE", None)
        self.env.pop("SPARK_WORKERS_CODEX", None)
        for harness in ("omp", "claude", "opencode"):
            binary = self.folder / ("fake " + harness)
            binary.write_text("#!/usr/bin/env python3\nKIND=" + repr(harness) + "\n" + FAKE)
            binary.chmod(0o755)
            self.cli("configure", "--harness", harness, "--binary", str(binary), "--base-url", "http://127.0.0.1:9999/v1", "--model", "qwen", "--health-url", "")
        self.cli("enable", "--sandbox", "danger-full-access")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, ok=True):
        result = subprocess.run([str(self.binary), *args], cwd=self.project, env=self.env, text=True, capture_output=True, timeout=20)
        if not ok:
            self.assertNotEqual(result.returncode, 0)
            return result.stderr
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def submit(self, harness, text, timeout="10"):
        job = self.cli("submit", "--worker", harness + "-local", text, "--timeout", timeout)
        return self.cli("wait", job["id"], "--seconds", "10")

    def test_three_installed_harnesses_edit_resume_and_apply(self):
        for harness in ("omp", "claude", "opencode"):
            with self.subTest(harness=harness):
                (self.project / "implementation.txt").unlink(missing_ok=True)
                job = self.submit(harness, "implement omits-finish" if harness == "opencode" else "implement")
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual(job["harness"], harness)
                self.assertFalse((self.project / "implementation.txt").exists())
                session = job["resume_ref"]
                result = self.cli("result", job["id"])
                self.assertIn("Implemented", result["report"])
                # Freeze both the worker and mode even if its registration is removed.
                config = Path(self.env["SPARK_WORKERS_CONFIG"])
                original = config.read_text()
                config.write_text("")
                self.cli("followup", job["id"], "follow-up")
                job = self.cli("wait", job["id"], "--seconds", "10")
                config.write_text(original)
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual(job["resume_ref"], session)
                self.cli("apply", job["id"])
                self.assertEqual((self.project / "implementation.txt").read_text(), "revised\n")
                self.assertEqual((self.project / "original.txt").read_text(), "keep original\n")
                self.assertEqual(subprocess.check_output(["git", "-C", str(self.project), "diff", "--cached"]), b"")

    def test_new_harnesses_require_explicit_full_access(self):
        self.cli("enable", "--sandbox", "workspace-write")
        for harness in ("omp", "claude", "opencode"):
            with self.subTest(harness=harness):
                self.assertIn("no built-in workspace sandbox", self.cli("submit", "--worker", harness + "-local", "task", ok=False))

    def test_native_errors_and_nonzero_exit_are_failures(self):
        for harness, prompt in (("omp", "error"), ("claude", "error"), ("claude", "exit-failure"),
                                ("claude", "mismatch"), ("opencode", "error"), ("opencode", "exit-failure"), ("opencode", "truncated")):
            with self.subTest(harness=harness, prompt=prompt):
                job = self.submit(harness, prompt)
                self.assertEqual(job["status"], "failed", job)
                self.assertTrue(job["error"])
                self.cli("apply", job["id"], ok=False)

    def test_cancellation_and_timeout_for_each_transport(self):
        for harness in ("omp", "claude", "opencode"):
            with self.subTest(harness=harness):
                job = self.cli("submit", "--worker", harness + "-local", "hang")
                self.cli("wait", job["id"], "--seconds", "1")
                self.cli("cancel", job["id"])
                job = self.cli("wait", job["id"], "--seconds", "10")
                self.assertEqual(job["status"], "cancelled", job)
                job = self.submit(harness, "hang", timeout="1")
                self.assertEqual(job["status"], "failed", job)

    def test_workers_lists_all_registered_harnesses_without_secrets(self):
        result = self.cli("workers")
        self.assertEqual({item["harness"] for item in result["harnesses"]}, {"codex", "pi", "omp", "opencode", "claude"})
        by_name = {item["worker"]: item for item in result["workers"]}
        for harness in ("omp", "claude", "opencode"):
            self.assertTrue(by_name[harness + "-local"]["valid"])
            self.assertTrue(by_name[harness + "-local"]["installed"])
        self.assertNotIn("apiKey", json.dumps(result))
        self.assertIn("interactive terminal", self.cli("setup", ok=False))

    def test_claude_credentials_stay_in_child_environment(self):
        self.env.update(FOUNDRY_TEST_KEY="test-private-token-value", CLAUDE_CODE_USE_BEDROCK="1")
        self.cli("configure", "--harness", "claude", "--binary", str(self.folder / "fake claude"),
                 "--base-url", "http://127.0.0.1:9999/v1", "--model", "qwen", "--api-key-env", "FOUNDRY_TEST_KEY", "--auth-mode", "bearer", "--health-url", "")
        job = self.submit("claude", "implement")
        self.assertEqual(job["status"], "completed", job)
        self.assertNotIn("test-private-token-value", json.dumps(self.cli("result", job["id"])))
        self.assertNotIn("test-private-token-value", Path(self.env["SPARK_WORKERS_CONFIG"]).read_text())

    def test_installer_can_leave_legacy_command_untouched(self):
        prefix = self.folder / "parallel install"
        (prefix / "bin").mkdir(parents=True)
        legacy = prefix / "bin/spark-workers"
        legacy.write_text("existing live Spark installation")
        installer.install(prefix, legacy_alias=False)
        self.assertEqual(legacy.read_text(), "existing live Spark installation")
        self.assertTrue((prefix / "bin/overseer").is_symlink())


class WizardAndOmpContractTests(unittest.TestCase):
    def test_opencode_scoped_instructions_do_not_mutate_frozen_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            frozen = bridge.freeze_worker({"harness": "opencode", "name": "local", "model": "qwen",
                "provider": "local", "base_url": "http://localhost/v1"}, folder, "danger-full-access")
            task = {**frozen, "project": "/original/project", "workspace": "/private/worktree"}
            instructions = bridge.task_instructions(task)
            env = bridge.harness_environment(task, instructions=instructions)
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["agent"]["foundry"]["prompt"], instructions)
            self.assertIn("Implementation workspace: /private/worktree", instructions)
            self.assertIn("Original checkout (do not edit", instructions)
            saved = json.loads((folder / "opencode-home/opencode.json").read_text())
            self.assertEqual(saved["agent"]["foundry"]["prompt"], bridge.WORKER_INSTRUCTIONS)

    def test_omp_translates_pi_env_reference_without_persisting_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "models.json"
            source.write_text(json.dumps({"providers": {"local": {"apiKey": "$LOCAL_MODEL_KEY",
                "api": "openai-completions", "baseUrl": "http://localhost/v1", "models": [{"id": "qwen"}]}}}))
            selected = {"harness": "omp", "name": "omp-local", "models": str(source),
                        "provider": "local", "model": "qwen"}
            with patch.dict(os.environ, {"LOCAL_MODEL_KEY": "test-secret-do-not-persist"}):
                frozen = bridge.freeze_worker(selected, base, "danger-full-access")
                spec = frozen["worker_config"]
                config = json.loads((base / "omp-home/models.yml").read_text())
                self.assertEqual(config["providers"]["local"]["apiKey"], "LOCAL_MODEL_KEY")
                self.assertEqual(spec["api_key_env"], "LOCAL_MODEL_KEY")
                self.assertNotIn("test-secret-do-not-persist", json.dumps(frozen) + json.dumps(config))
                self.assertEqual(json.loads(source.read_text())["providers"]["local"]["apiKey"], "$LOCAL_MODEL_KEY")
                bridge.harness_environment(frozen)
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(bridge.Error, "Missing credential"):
                bridge.harness_environment(frozen)

    def test_role_instructions_do_not_leak_worker_delegation_ban_into_operator(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = base / "profile.toml"
            profile.write_text('model="qwen"\nmodel_provider="local"\n[model_providers.local]\nbase_url="http://localhost/v1"\nwire_api="responses"\n')
            models = base / "models.json"
            models.write_text(json.dumps({"providers": {"local": {"api": "openai-completions",
                "baseUrl": "http://localhost/v1", "models": [{"id": "qwen"}]}}}))
            instructions = "Plan work and delegate implementation through the Overseer CLI."
            for harness in ("codex", "pi", "omp", "opencode", "claude"):
                with self.subTest(harness=harness):
                    selected = {"name": "local", "harness": harness, "model": "qwen", "provider": "local",
                                "binary": "fake-harness", "profile": str(profile), "models": str(models),
                                "base_url": "http://localhost/v1"}
                    for role, text in (("operator", instructions), ("worker", bridge.WORKER_INSTRUCTIONS)):
                        folder = base / harness / role
                        folder.mkdir(parents=True)
                        frozen = bridge.freeze_worker(selected, folder, "danger-full-access", instructions=text)
                        task = {**frozen, "workspace": str(base)}
                        command = bridge.harness_command(task, instructions=text)
                        if harness == "opencode":
                            config = json.loads((folder / "opencode-home/opencode.json").read_text())
                            self.assertEqual(config["agent"]["foundry"]["prompt"], text)
                        elif harness != "codex":
                            self.assertEqual(command[command.index("--append-system-prompt") + 1], text)
                    self.assertIn("Do not delegate", bridge.WORKER_INSTRUCTIONS)

    def test_setup_configures_only_and_shows_explicit_project_choice(self):
        args = SimpleNamespace()
        with patch.object(bridge.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=["opencode", "local-code", "opencode", "http://localhost:1234/v1", "qwen", "local", "LOCAL_KEY"]), patch.object(bridge, "command_configure") as configure, contextlib.redirect_stdout(io.StringIO()) as out:
            bridge.command_setup(args)
        configure.assert_called_once_with(args)
        self.assertEqual(args.api_key_env, "LOCAL_KEY")
        self.assertIn("enable --worker local-code --sandbox danger-full-access", out.getvalue())

    def test_omp_never_uses_pi_settled_or_uncorrelated_results(self):
        worker = OmpWorker(None, {})
        worker.prompt_id, worker.outcome = "correct-id", None
        self.assertIsNone(worker.event({"type": "agent_settled", "aborted": False}))
        self.assertIsNone(worker.event({"type": "prompt_result", "id": "other", "status": "completed", "sessionSettled": True}))
        self.assertIsNone(worker.event({"type": "prompt_result", "id": "correct-id", "agentInvoked": True, "status": "completed", "sessionSettled": False}))
        self.assertEqual(worker.event({"type": "session_settled"})["status"], "completed")
