"""The live verifier must distinguish real work from help and success claims."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("operator_smoke", Path(__file__).resolve().parents[1] / "tools/orchestrator_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class OrchestratorEvidenceTests(unittest.TestCase):
    def test_help_and_failures_cannot_count_as_successful_operations(self):
        events = [{"args": args, "returncode": code} for args, code in (
            (["result", "--help"], 0), (["followup", "--help"], 0),
            (["apply", "job"], 1), (["--version"], 0),
            (["wait", "job", "--seconds", "15"], 0),
            (["result", "job"], 0), (["diff", "job"], 0))]
        self.assertEqual([e["args"][0] for e in smoke.operation_events(events)], ["wait", "result", "diff"])

    def test_successful_job_cannot_pass_without_reviewing_corrected_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            folder = base / "state/projects/project/tasks/job"
            folder.mkdir(parents=True)
            job = {"harness": "pi", "model": "local", "attempt": 2, "applied": True,
                   "status": "completed", "resume_ref": "session"}
            (folder / "task.json").write_text(json.dumps(job))
            events = []
            for verb, attempt in (("doctor", 0), ("submit", 0), ("wait", 1), ("result", 1),
                                  ("diff", 0), ("followup", 0), ("wait", 2), ("result", 2), ("apply", 0)):
                response = {**job, "attempt": attempt} if verb in {"wait", "result"} else {}
                stdout = "diff --git a/clamp.py b/clamp.py\n" if verb == "diff" else json.dumps(response)
                events.append({"args": [verb, "job"], "returncode": 0, "stdout": stdout})
            (base / "cli-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            with self.assertRaisesRegex(AssertionError, "Correction was not reviewed"):
                smoke.verify(base, base / "project", {"harness": "codex", "model": "local"},
                             {"harness": "pi", "model": "local"}, "", 1)
            events.insert(-1, {"args": ["diff", "job", "--stat"], "returncode": 0,
                               "stdout": " clamp.py | 3 +++\n"})
            (base / "cli-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            with self.assertRaisesRegex(AssertionError, "Correction was not reviewed"):
                smoke.verify(base, base / "project", {"harness": "codex", "model": "local"},
                             {"harness": "pi", "model": "local"}, "", 1)


if __name__ == "__main__":
    unittest.main()
