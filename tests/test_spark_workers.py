"""Regression tests for opt-in boundaries and preservation of project changes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("spark_workers", Path(__file__).resolve().parents[1] / "spark_workers.py")
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)


class RepoFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="spark-workers-tests-")
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.state_patch = patch.object(sw, "STATE", self.state)
        self.state_patch.start()
        profile = Path(self.temp.name) / "worker.config.toml"
        profile.write_text('model = "spark-model"\nmodel_provider = "spark"\n[model_providers.spark]\nname = "Test local worker"\nbase_url = "http://127.0.0.1:18888/v1"\nwire_api = "responses"\n')
        self.profile_patch = patch.object(sw, "PROFILE", profile)
        self.profile_patch.start()
        self.git("init", "-q")
        (self.root / "main.py").write_text("value = 1\n")
        (self.root / ".gitignore").write_text("ignored.txt\n__pycache__/\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixture")

    def tearDown(self):
        self.profile_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def git(self, *args):
        return sw.git(self.root, *args).stdout

    def enable(self):
        with contextlib.redirect_stdout(io.StringIO()):
            sw.command_enable(SimpleNamespace(project=str(self.root), update=False, sandbox="danger-full-access"))

    def fixture_task(self, status="completed"):
        self.enable()
        folder = sw.project_state(self.root) / "tasks" / "abcdef123456"
        folder.mkdir(parents=True)
        commit, work = sw.snapshot(self.root, folder)
        task = {"id": folder.name, "project": str(self.root), "workspace": str(work),
                "snapshot": commit, "status": status, "applied": False,
                "attempt": 1, "created_at": sw.now()}
        sw.atomic_json(folder / "task.json", task)
        return task, folder / "task.json", work


class ProjectTests(RepoFixture):
    def test_opt_in_is_project_scoped_and_explicit_only(self):
        with self.assertRaises(sw.Error):
            sw.project(self.root)
        self.enable()
        self.assertEqual(sw.project(self.root), self.root)
        self.assertIn("allow_implicit_invocation: false", (self.root / sw.SKILL / "agents/openai.yaml").read_text())
        self.assertTrue((self.root / sw.SKILL / "assets/overseer.jpg").is_file())
        self.assertIn("name: spark-workers", (self.root / sw.LEGACY_SKILL / "SKILL.md").read_text())
        self.assertIn("allow_implicit_invocation: false", (self.root / sw.LEGACY_SKILL / "agents/openai.yaml").read_text())
        self.assertFalse((self.root / ".codex/config.toml").exists())
        other = Path(self.temp.name) / "other"
        other.mkdir()
        sw.run(["git", "init", "-q", other])
        with self.assertRaises(sw.Error):
            sw.project(other)
        with contextlib.redirect_stdout(io.StringIO()):
            sw.command_disable(SimpleNamespace(project=str(self.root)))
        with self.assertRaises(sw.Error):
            sw.project(self.root)

    def test_does_not_overwrite_existing_project_skill(self):
        skill = self.root / sw.SKILL
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Existing custom skill")
        with self.assertRaises(sw.Error):
            self.enable()
        self.assertEqual((skill / "SKILL.md").read_text(), "Existing custom skill")

    def test_upgrade_does_not_overwrite_unrelated_overseer_skill(self):
        self.enable()
        marker = self.root / sw.MARKER
        config = json.loads(marker.read_text())
        config.pop("skills")
        sw.atomic_json(marker, config)
        custom = self.root / sw.SKILL / "SKILL.md"
        custom.write_text("Unrelated existing Overseer skill")
        with self.assertRaisesRegex(sw.Error, "unrelated skill"):
            sw.command_enable(SimpleNamespace(project=str(self.root), update=True, sandbox=None))
        self.assertEqual(custom.read_text(), "Unrelated existing Overseer skill")

    def test_old_project_can_launch_with_legacy_skill_until_refreshed(self):
        self.enable()
        shutil.rmtree(self.root / sw.SKILL)
        args = SimpleNamespace(project=str(self.root), prompt="implement the task", prompt_file=None, full_access=False)
        with patch.object(sw.os, "chdir"), patch.object(sw.os, "execv") as execute:
            sw.command_run(args)
        self.assertEqual(execute.call_args.args[1][-1], "$spark-workers implement the task")

    def test_skill_updates_preserve_project_permission_choice(self):
        with contextlib.redirect_stdout(io.StringIO()):
            sw.command_enable(SimpleNamespace(project=str(self.root), update=False, sandbox="workspace-write"))
            sw.command_enable(SimpleNamespace(project=str(self.root), update=True, sandbox=None))
        config = json.loads((self.root / sw.MARKER).read_text())
        self.assertEqual(config["sandbox"], "workspace-write")

    def test_worker_harness_has_private_config_and_history_home(self):
        original = Path(self.temp.name) / "original-codex-home"
        original.mkdir()
        content = b'model = "operator-model"\n'
        (original / "config.toml").write_bytes(content)
        task = {"project": str(self.root)}
        with patch.dict(os.environ, {"CODEX_HOME": str(original)}):
            env = sw.harness_environment(task)
        private = Path(env["CODEX_HOME"])
        self.assertNotEqual(private, original)
        self.assertTrue(private.is_relative_to(self.state))
        self.assertEqual((private / "config.toml").read_bytes(), content)
        (private / "config.toml").write_text('model = "spark-model"\n[projects.example]\ntrust_level = "trusted"\n')
        self.assertEqual((original / "config.toml").read_bytes(), content)

    def test_legacy_jobs_cannot_be_reinterpreted_as_pi_or_change_models(self):
        settings = {"default_worker": "pi-local", "health_url": "", "workers": {"codex-local": {"harness": "pi"}}}
        with patch.object(sw, "SETTINGS", settings):
            self.assertEqual(sw.legacy_worker({})["harness"], "codex")
            self.assertEqual(sw.legacy_worker({})["health_url"], "")
            with self.assertRaisesRegex(sw.Error, "configuration changed"):
                sw.legacy_worker({"model": "different-model", "provider": "spark"})

    def test_operator_full_access_requires_explicit_launcher_flag(self):
        self.enable()
        args = SimpleNamespace(project=str(self.root), prompt="implement the task", prompt_file=None, full_access=False)
        with patch.object(sw.os, "chdir"), patch.object(sw.os, "execv") as execute:
            sw.command_run(args)
            normal = execute.call_args.args[1]
            self.assertNotIn("--sandbox", normal)
            self.assertNotIn("--model", normal)
            self.assertEqual(normal[-1], "$overseer implement the task")
            args.full_access = True
            sw.command_run(args)
            full = execute.call_args.args[1]
            self.assertEqual(full[full.index("--sandbox") + 1], "danger-full-access")

    def test_snapshot_includes_dirty_files_without_changing_user_index(self):
        (self.root / "main.py").write_text("value = 2\n")
        self.git("add", "main.py")
        (self.root / "main.py").write_text("value = 3\n")
        (self.root / "new.txt").write_text("untracked user content\n")
        (self.root / "ignored.txt").write_text("must not copy\n")
        before = self.git("diff", "--cached", "--binary")
        _, _, work = self.fixture_task()
        self.assertEqual((work / "main.py").read_text(), "value = 3\n")
        self.assertEqual((work / "new.txt").read_text(), "untracked user content\n")
        self.assertFalse((work / "ignored.txt").exists())
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)

    def test_apply_includes_new_files_preserves_index_and_user_changes(self):
        (self.root / "main.py").write_text("value = 3\n")
        self.git("add", "main.py")
        (self.root / "user.txt").write_text("user content\n")
        before = self.git("diff", "--cached", "--binary")
        task, path, work = self.fixture_task()
        (work / "main.py").write_text("value = 4\n")
        (work / "worker.txt").write_text("new implementation\n")
        self.assertIn(b"worker.txt", sw.patch_for(task))
        with contextlib.redirect_stdout(io.StringIO()):
            sw.command_apply(SimpleNamespace(project=str(self.root), task_id=task["id"]))
        self.assertEqual((self.root / "main.py").read_text(), "value = 4\n")
        self.assertEqual((self.root / "worker.txt").read_text(), "new implementation\n")
        self.assertEqual((self.root / "user.txt").read_text(), "user content\n")
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)
        self.assertTrue(sw.read_task(path)["applied"])
        with self.assertRaises(sw.Error):
            sw.command_apply(SimpleNamespace(project=str(self.root), task_id=task["id"]))

    def test_apply_conflict_keeps_original_files_unchanged(self):
        task, path, work = self.fixture_task()
        (work / "main.py").write_text("value = 4\n")
        (self.root / "main.py").write_text("value = 999\n")
        with self.assertRaises(sw.Error):
            sw.command_apply(SimpleNamespace(project=str(self.root), task_id=task["id"]))
        self.assertEqual((self.root / "main.py").read_text(), "value = 999\n")
        self.assertFalse(sw.read_task(path)["applied"])

    def test_failed_task_cannot_be_applied(self):
        task, _, work = self.fixture_task(status="failed")
        (work / "main.py").write_text("value = 4\n")
        with self.assertRaises(sw.Error):
            sw.command_apply(SimpleNamespace(project=str(self.root), task_id=task["id"]))
        self.assertEqual((self.root / "main.py").read_text(), "value = 1\n")

    def test_detects_dead_supervisor_and_does_not_confuse_reused_pids(self):
        _, path, _ = self.fixture_task(status="running")
        sw.update_task(path, pid=os.getpid(), process_token="wrong-process-start-time")
        self.assertEqual(sw.read_task(path)["status"], "orphaned")

    def test_task_lookup_cannot_escape_project_state(self):
        self.enable()
        for task_id in ("../task.json", "/tmp/something", "abc"):
            with self.assertRaises(sw.Error):
                sw.state_path(self.root, task_id)


class RpcTests(unittest.TestCase):
    def test_handles_notifications_while_waiting_for_response_and_server_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            program = folder / "fake.py"
            program.write_text('''import json, sys
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"method": "notice", "params": {"value": 1}}), flush=True)
    print(json.dumps({"id": message["id"], "result": {"ok": True}}), flush=True)
    break
''')
            rpc = sw.Rpc([sw.sys.executable, str(program)], folder, folder / "log", folder / "events")
            try:
                self.assertEqual(rpc.call("initialize"), {"ok": True})
                self.assertEqual(rpc.pending[0]["method"], "notice")
                with self.assertRaises(EOFError):
                    rpc.receive(timeout=2)
            finally:
                rpc.close()


class WorkerLifecycleTests(RepoFixture):
    def fake_rpc(self, on_turn=None):
        class FakeRpc:
            def __init__(self, *args, **kwargs):
                self.pending = []

            def call(self, method, params=None, **kwargs):
                if method in ("thread/start", "thread/resume"):
                    if params["modelProvider"] != "spark":
                        raise AssertionError("wrong provider")
                    return {"modelProvider": "spark", "model": "spark-model", "thread": {"id": "fake-thread"}}
                if method == "turn/start":
                    if on_turn:
                        on_turn(params)
                    return {"turn": {"id": "fake-turn"}}
                return {}

            def send(self, *args, **kwargs):
                pass

            def receive(self):
                return {"method": "turn/completed", "params": {"threadId": "fake-thread",
                        "turn": {"id": "fake-turn", "status": "completed"}}}

            def close(self):
                pass
        return FakeRpc

    def test_worker_only_uses_project_selected_full_access_mode(self):
        task, path, _ = self.fixture_task(status="queued")
        sw.update_task(path, assignment="test", sandbox="danger-full-access", timeout_seconds=10)
        observed = []
        with patch.object(sw, "Rpc", self.fake_rpc(on_turn=observed.append)), patch.object(sw, "harness_command", return_value=[]):
            sw.worker(path)
        self.assertEqual(observed[0]["sandboxPolicy"], {"type": "dangerFullAccess"})
        self.assertEqual(sw.read_task(path)["status"], "completed")

    def test_sandboxed_worker_does_not_fall_back_to_full_access(self):
        task, path, _ = self.fixture_task(status="queued")
        sw.update_task(path, assignment="test", sandbox="workspace-write", timeout_seconds=10)
        observed = []
        with patch.object(sw, "Rpc", self.fake_rpc(on_turn=observed.append)), patch.object(sw, "harness_command", return_value=[]):
            sw.worker(path)
        self.assertEqual(observed[0]["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertFalse(observed[0]["sandboxPolicy"]["networkAccess"])

    def test_cancelled_queued_task_never_starts_harness(self):
        _, path, _ = self.fixture_task(status="queued")
        (path.parent / "cancel").touch()
        with patch.object(sw, "Rpc") as rpc:
            sw.worker(path)
            rpc.assert_not_called()
        self.assertEqual(sw.read_task(path)["status"], "cancelled")

    def test_provider_mismatch_refuses_inference(self):
        _, path, _ = self.fixture_task(status="queued")
        sw.update_task(path, assignment="test", timeout_seconds=10)
        class WrongProvider(self.fake_rpc()):
            def call(self, method, params=None, **kwargs):
                if method == "thread/start":
                    return {"modelProvider": "openai", "model": "some-model", "thread": {"id": "fake"}}
                if method == "turn/start":
                    raise AssertionError("Must not start inference")
                return {}
        with patch.object(sw, "Rpc", WrongProvider), patch.object(sw, "harness_command", return_value=[]):
            sw.worker(path)
        self.assertEqual(sw.read_task(path)["status"], "failed")
        self.assertIn("refusing inference", sw.read_task(path)["error"])


if __name__ == "__main__":
    unittest.main()
