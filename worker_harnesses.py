"""Two small protocol adapters. Queueing, permissions and Git stay in the bridge."""


class Error(RuntimeError):
    pass


class CodexWorker:
    def __init__(self, rpc, task):
        self.rpc, self.task = rpc, task

    def start(self, instructions):
        rpc, task = self.rpc, self.task
        spec = task["worker_config"]
        rpc.call("initialize", {"clientInfo": {"name": "frontier_foundry", "version": "0.2.0"},
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
