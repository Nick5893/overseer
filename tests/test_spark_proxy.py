"""Protocol checks for the local Codex/vLLM adapter; no model or credentials."""
import copy
import http.client
import http.server
import json
import threading
import unittest

from spark_proxy import EventAdapter, Proxy, ToolAdapter


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "tools": [
                {"type": "custom", "name": "exec", "description": "Run JavaScript"},
                {"type": "namespace", "name": "clock", "tools": [
                    {"type": "function", "name": "sleep", "parameters": {"type": "object"}}
                ]},
                {"type": "function", "name": "wait", "parameters": {"type": "object"}},
            ],
            "input": [],
        }
        self.adapter = ToolAdapter(self.request)
        self.code = 'text(await tools.exec_command({cmd: "printf \\\"hello\\\\n\\\""}));\n'

    def call(self, arguments=None):
        return {"type": "function_call", "name": "exec", "id": "fc_1",
                "call_id": "call_1", "arguments": arguments if arguments is not None else json.dumps({"input": self.code})}

    def test_freeform_and_namespace_tools_are_visible_as_functions(self):
        tools = self.adapter.request["tools"]
        self.assertEqual([x["name"] for x in tools], ["exec", "clock__sleep", "wait"])
        self.assertTrue(all(x["type"] == "function" for x in tools))
        self.assertEqual(tools[0]["parameters"]["required"], ["input"])
        self.assertEqual(self.request["tools"][0]["type"], "custom")

    def test_history_round_trip_preserves_source_and_call_ids(self):
        original = self.call()
        custom = self.adapter.output_item(original)
        self.assertEqual(custom["type"], "custom_tool_call")
        self.assertEqual(custom["input"], self.code)
        self.assertNotIn("arguments", custom)
        wire = self.adapter.input_item(custom)
        self.assertEqual(wire, original)
        result = self.adapter.input_item({"type": "custom_tool_call_output", "call_id": "call_1", "output": "ok"})
        self.assertEqual(result, {"type": "function_call_output", "call_id": "call_1", "output": "ok"})

    def test_namespaced_calls_restore_codex_namespace(self):
        wire = {"type": "function_call", "name": "clock__sleep", "arguments": "{}"}
        item = self.adapter.output_item(wire)
        self.assertEqual(item["name"], "sleep")
        self.assertEqual(item["namespace"], "clock")
        self.assertEqual(self.adapter.input_item(item), wire)

    def test_stream_reassembles_fragmented_json_as_raw_source_once(self):
        events = EventAdapter(self.adapter)
        added = events.translate({"type": "response.output_item.added", "output_index": 0, "item": self.call("")})
        self.assertEqual(added[0]["item"]["type"], "custom_tool_call")
        args = self.call()["arguments"]
        for fragment in (args[:9], args[9:23], args[23:]):
            self.assertEqual(events.translate({"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": fragment}), [])
        done = events.translate({"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0, "arguments": args})
        self.assertEqual(done[0]["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(done[0]["delta"], self.code)
        self.assertEqual(done[1]["input"], self.code)
        item_done = events.translate({"type": "response.output_item.done", "output_index": 0, "item": self.call()})
        self.assertEqual(len(item_done), 1)
        self.assertEqual(item_done[0]["item"]["input"], self.code)
        self.assertEqual([x["sequence_number"] for x in added + done + item_done], [0, 1, 2, 3])

    def test_missing_arguments_done_event_still_delivers_input(self):
        events = EventAdapter(self.adapter)
        events.translate({"type": "response.output_item.added", "output_index": 0, "item": self.call("")})
        result = events.translate({"type": "response.output_item.done", "output_index": 0, "item": self.call()})
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["delta"], self.code)

    def test_completed_response_and_forced_tool_choice(self):
        response = self.adapter.response({"status": "completed", "output": [self.call()]})
        self.assertEqual(response["output"][0]["input"], self.code)
        request = copy.deepcopy(self.request)
        request["tool_choice"] = {"type": "custom", "name": "exec"}
        self.assertEqual(ToolAdapter(request).request["tool_choice"], {"type": "function", "name": "exec"})

    def test_invalid_wrapper_fails_instead_of_executing_wrong_input(self):
        with self.assertRaises(ValueError):
            self.adapter.output_item(self.call('{"input": 123}'))

    def test_model_code_field_alias_preserves_the_raw_source(self):
        self.assertEqual(self.adapter.raw_input(json.dumps({"code": self.code})), self.code)

    def test_namespace_collision_is_rejected(self):
        request = copy.deepcopy(self.request)
        request["tools"].append({"type": "function", "name": "clock__sleep"})
        with self.assertRaises(ValueError):
            ToolAdapter(request)


class CancellationTests(unittest.TestCase):
    def test_client_disconnect_cancels_upstream_during_prefill(self):
        started = threading.Event()
        closed = threading.Event()

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.connection.settimeout(4)
                started.set()
                if self.connection.recv(1) == b"":
                    closed.set()

        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        proxy.upstream_port = backend.server_port
        backend.daemon_threads = proxy.daemon_threads = True
        for server in (backend, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=4)
        try:
            client.request("POST", "/v1/responses", json.dumps({"tools": [], "input": []}), {"Content-Type": "application/json"})
            self.assertTrue(started.wait(3))
            client.close()
            self.assertTrue(closed.wait(3), "Cancellation did not reach the inference server")
        finally:
            client.close()
            for server in (proxy, backend):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
