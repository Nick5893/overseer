"""Small native protocol adapters. Queueing, permissions and Git stay in the bridge."""
import signal


class Error(RuntimeError):
    pass


class CodexWorker:
    def __init__(self, rpc, task):
        self.rpc, self.task = rpc, task

    def start(self, instructions):
        rpc, task = self.rpc, self.task
        spec = task["worker_config"]
        rpc.call("initialize", {"clientInfo": {"name": "overseer", "version": "0.4.0"},
                                "capabilities": {"experimentalApi": True}}, timeout=30)
        rpc.send("initialized", request=False)
        params = {"model": spec["model"], "modelProvider": spec["provider"],
                  "cwd": task["workspace"], "approvalPolicy": "never", "sandbox": task["sandbox"],
                  "developerInstructions": instructions,
                  "config": {"agents.enabled": False, "features.apps": False,
                             "features.plugins": False, "features.memories": False,
                             "skills.include_instructions": False, "web_search": "disabled"}}
        resume = task.get("resume_ref") or task.get("thread_id")
        if resume:
            params.update(threadId=resume, excludeTurns=True)
        result = rpc.call("thread/resume" if resume else "thread/start", params)
        if result.get("modelProvider") != spec["provider"] or result.get("model") != spec["model"]:
            raise Error("Worker did not select the configured local model/provider; refusing inference.")
        self.thread = result["thread"]["id"]
        return {"resume_ref": self.thread, "thread_id": self.thread,
                "model": result["model"], "provider": result["modelProvider"]}

    def prompt(self):
        task = self.task
        policy = ({"type": "dangerFullAccess"} if task["sandbox"] == "danger-full-access" else
                  {"type": "workspaceWrite", "writableRoots": [task["workspace"]], "networkAccess": False})
        result = self.rpc.call("turn/start", {"threadId": self.thread,
            "input": [{"type": "text", "text": task["assignment"], "text_elements": []}],
            "sandboxPolicy": policy})
        self.turn = result["turn"]["id"]
        return {"turn_id": self.turn}

    def interrupt(self):
        self.rpc.send("turn/interrupt", {"threadId": self.thread, "turnId": self.turn})

    def event(self, msg):
        params = msg.get("params", {})
        if params.get("threadId") != self.thread:
            return None
        method = msg.get("method")
        if method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                return {"type": "report", "text": item.get("text", "")}
            if item.get("type") in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
                                    "webSearch", "imageGeneration", "collabAgentToolCall"}:
                return {"type": "tool", "item": item}
        if method == "turn/completed" and params.get("turn", {}).get("id") == self.turn:
            turn = params["turn"]
            return {"type": "finished", "status": "completed" if turn["status"] == "completed" else "failed",
                    "error": turn.get("error")}
        return None


class PiWorker:
    def __init__(self, rpc, task):
        self.rpc, self.task = rpc, task
        self.last_message = None

    def start(self, instructions):
        spec = self.task["worker_config"]
        state = self.rpc.call("get_state", timeout=30)
        model = state.get("model", {})
        if (model.get("id"), model.get("provider"), model.get("api"), model.get("baseUrl")) != (
                spec["model"], spec["provider"], spec["api"], spec["base_url"]):
            raise Error("Pi did not select the configured model/provider/endpoint; refusing inference.")
        session = state.get("sessionFile")
        if not session or (self.task.get("resume_ref") and session != self.task["resume_ref"]):
            raise Error("Pi did not open the expected persistent session.")
        return {"resume_ref": session, "model": model["id"], "provider": model["provider"]}

    def prompt(self):
        result = self.rpc.call("prompt", {"message": self.task["assignment"]})
        if result.get("disposition") != "started":
            raise Error("Pi accepted the prompt without starting a run; refusing to wait for nonexistent work.")
        return {}

    def interrupt(self):
        self.rpc.send("clear_queue")
        self.rpc.send("abort")

    def event(self, msg):
        kind = msg.get("type")
        if kind == "extension_ui_request":
            raise Error("Unattended Pi worker cannot answer an extension UI request.")
        if kind == "message_end" and msg.get("message", {}).get("role") == "assistant":
            self.last_message = msg["message"]
            return {"type": "report", "text": "\n".join(
                block.get("text", "") for block in self.last_message.get("content", [])
                if block.get("type") == "text")}
        if kind == "tool_execution_end":
            return {"type": "tool", "name": msg.get("toolName"), "result": msg.get("result"),
                    "is_error": msg.get("isError", False)}
        # agent_end may be followed by retries or compaction. Only settled is final.
        if kind == "agent_settled":
            message = self.last_message or {}
            good = not msg.get("aborted") and message.get("stopReason") == "stop"
            return {"type": "finished", "status": "completed" if good else "failed",
                    "error": None if good else message.get("errorMessage") or "Pi settled without a successful final response."}
        return None


class OmpWorker(PiWorker):
    """OMP shares Pi's commands but has its own correlated completion protocol."""
    def prompt(self):
        result = self.rpc.call("prompt", {"message": self.task["assignment"]})
        self.prompt_id = self.rpc.last_request_id
        self.outcome = None
        if result.get("agentInvoked") is False:
            raise Error("OMP handled the prompt without starting agent work.")
        return {}

    def interrupt(self):
        self.rpc.send("abort")

    def event(self, msg):
        kind = msg.get("type")
        if kind in {"host_tool_request", "host_uri_request", "rpc_frame_error"}:
            raise Error(f"Unattended OMP worker cannot handle {kind}.")
        if kind == "prompt_result" and msg.get("id") == self.prompt_id:
            self.outcome = msg
            if msg.get("sessionSettled"):
                return self.finished()
        if kind == "session_settled":
            if self.outcome is None:
                return None
            return self.finished()
        # OMP emits session_settled, never Pi's agent_settled.
        if kind != "agent_settled":
            return super().event(msg)
        return None

    def finished(self):
        good = self.outcome.get("status") == "completed" and self.outcome.get("agentInvoked") is True
        message = self.last_message or {}
        if message.get("stopReason") in {"error", "aborted"}:
            good = False
        return {"type": "finished", "status": "completed" if good else "failed",
                "error": None if good else self.outcome.get("error") or message.get("errorMessage") or "OMP prompt failed or was aborted."}


class StreamWorker:
    """One native headless process per attempt; completion also requires clean exit."""
    def __init__(self, rpc, task):
        self.rpc, self.task = rpc, task
        self.spec = task["worker_config"]
        self.session = task.get("resume_ref")
        self.error = None

    def start(self, instructions):
        return {"model": self.spec["model"], "provider": self.spec["provider"]}

    def prompt(self):
        self.rpc.proc.stdin.write(self.task["assignment"])
        self.rpc.proc.stdin.close()
        return {}

    def interrupt(self):
        if self.rpc.proc.poll() is None:
            self.rpc.proc.send_signal(signal.SIGINT)

    def finish(self):
        code = self.rpc.proc.wait(timeout=5)
        good = code == 0 and not self.error and self.successful()
        return {"type": "finished", "status": "completed" if good else "failed",
                "error": None if good else self.error or f"Harness exited {code} without a successful final result."}


class ClaudeWorker(StreamWorker):
    def __init__(self, rpc, task):
        super().__init__(rpc, task)
        self.result = None
        self.verified = False

    def event(self, msg):
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            expected = self.task.get("resume_ref") or self.spec["session_id"]
            if msg.get("model") != self.spec["model"] or msg.get("session_id") != expected:
                raise Error("Claude Code reported an unexpected model/session; refusing this result.")
            self.session, self.verified = expected, True
            return {"type": "identity", "resume_ref": expected}
        if msg.get("type") == "result":
            self.result = msg
            if msg.get("is_error") or msg.get("subtype") != "success":
                self.error = msg.get("errors") or msg.get("result") or "Claude Code reported failure."
            return {"type": "report", "text": msg.get("result") or ""}
        if msg.get("type") in {"assistant", "user"}:
            blocks = msg.get("message", {}).get("content", [])
            if isinstance(blocks, list):
                tools = [block for block in blocks if block.get("type") in {"tool_use", "tool_result"}]
                if tools:
                    return {"type": "tool", "items": tools}
        return None

    def successful(self):
        return self.verified and self.result and self.result.get("subtype") == "success"


class OpenCodeWorker(StreamWorker):
    def __init__(self, rpc, task):
        super().__init__(rpc, task)
        self.reason = None
        self.reported = False

    def event(self, msg):
        session = msg.get("sessionID")
        if session:
            if self.session and session != self.session:
                raise Error("OpenCode reported an unexpected session.")
            self.session = session
        kind, part = msg.get("type"), msg.get("part", {})
        event = None
        if kind == "step_start":
            # A tool-calls finish belongs to the previous step. Some versions
            # omit step_finish for the final text-only step before clean exit.
            self.reason = None
        elif kind == "error":
            self.error = msg.get("error") or "OpenCode reported failure."
        elif kind == "text":
            self.reported = True
            event = {"type": "report", "text": part.get("text", "")}
        elif kind == "tool_use":
            event = {"type": "tool", "item": part}
        elif kind == "step_finish":
            self.reason = part.get("reason")
        if session:
            event = event or {"type": "identity"}
            event["resume_ref"] = session
        return event

    def successful(self):
        # OpenCode 2.0.x can emit only reconciled text/tool records. Its clean
        # process exit is the completion boundary; explicit failed finish reasons
        # from versions that emit step_finish still take precedence.
        return self.session and self.reported and self.reason in {None, "stop", "end_turn"}


ADAPTERS = {"codex": CodexWorker, "pi": PiWorker, "omp": OmpWorker,
            "claude": ClaudeWorker, "opencode": OpenCodeWorker}
