#!/usr/bin/env python3
"""Translate Codex freeform/namespaced tools for the local vLLM Responses API.

Inference stays on loopback. Credentials are forwarded, never stored or logged.
Tool execution and approvals remain entirely in Codex.
"""

import argparse
import copy
import http.client
import http.server
import json
import logging
import select
import socket
import threading


class ToolAdapter:
    def __init__(self, request):
        self.specs = {}
        self.request = copy.deepcopy(request)
        tools = []

        def flatten(tool, namespace=None):
            if tool.get("type") == "namespace":
                for child in tool.get("tools", []):
                    flatten(child, tool["name"])
                return
            tool = copy.deepcopy(tool)
            name = tool.get("name")
            if tool.get("type") not in ("custom", "function"):
                tools.append(tool)
                return
            wire_name = f"{namespace}__{name}" if namespace else name
            if wire_name in self.specs:
                raise ValueError("Tool names collide after namespace translation")
            self.specs[wire_name] = (name, namespace, tool["type"] == "custom")
            if tool["type"] == "custom":
                tool = {
                    "type": "function",
                    "name": wire_name,
                    "description": tool.get("description", "")
                    + "\nCompatibility wrapper: provide the complete raw tool input "
                    "as the input string in the JSON arguments. For exec, input "
                    "must be JavaScript for the Codex tool runtime, not shell code.",
                    "parameters": {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            else:
                tool["name"] = wire_name
            tools.append(tool)

        for tool in request.get("tools", []):
            flatten(tool)
        self.request["tools"] = tools
        if isinstance(request.get("input"), list):
            self.request["input"] = [self.input_item(x) for x in request["input"]]
        choice = self.request.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") in ("custom", "function"):
            choice["type"] = "function"
            if choice.get("namespace"):
                choice["name"] = choice.pop("namespace") + "__" + choice["name"]

    @staticmethod
    def input_item(item):
        item = copy.deepcopy(item)
        if item.get("type") == "custom_tool_call":
            item["type"] = "function_call"
            item["arguments"] = json.dumps({"input": item.pop("input")})
        elif item.get("type") == "custom_tool_call_output":
            item["type"] = "function_call_output"
        if item.get("type") == "function_call" and item.get("namespace"):
            item["name"] = item.pop("namespace") + "__" + item["name"]
        return item

    @staticmethod
    def raw_input(arguments):
        value = json.loads(arguments)
        if isinstance(value, dict):
            for key in ("input", "code", "script"):
                if isinstance(value.get(key), str):
                    return value[key]
        raise ValueError("Local model returned invalid freeform-tool arguments")

    def output_item(self, item, partial=False):
        item = copy.deepcopy(item)
        if item.get("type") != "function_call":
            return item
        spec = self.specs.get(item.get("name"))
        if not spec:
            return item
        name, namespace, custom = spec
        item["name"] = name
        if namespace:
            item["namespace"] = namespace
        if custom:
            item["type"] = "custom_tool_call"
            arguments = item.pop("arguments", "")
            item["input"] = "" if partial else self.raw_input(arguments)
        return item

    def response(self, response):
        response = copy.deepcopy(response)
        if "output" in response:
            response["output"] = [self.output_item(x) for x in response["output"]]
        return response


class EventAdapter:
    def __init__(self, adapter):
        self.adapter = adapter
        self.custom_ids = set()
        self.finished_ids = set()
        self.arguments = {}
        self.sequence = 0

    def finish_input(self, event, arguments):
        item_id = event["item_id"]
        if item_id in self.finished_ids:
            return []
        raw = self.adapter.raw_input(arguments)
        self.finished_ids.add(item_id)
        common = {"item_id": item_id, "output_index": event["output_index"]}
        return [
            {"type": "response.custom_tool_call_input.delta", "delta": raw, **common},
            {"type": "response.custom_tool_call_input.done", "input": raw, **common},
        ]

    def translate(self, event):
        event = copy.deepcopy(event)
        kind = event.get("type", "")
        out = []
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            spec = self.adapter.specs.get(item.get("name"))
            if spec and spec[2]:
                self.custom_ids.add(item["id"])
                if kind == "response.output_item.done":
                    out += self.finish_input(
                        {"item_id": item["id"], "output_index": event["output_index"]},
                        item.get("arguments", self.arguments.get(item["id"], "")),
                    )
            event["item"] = self.adapter.output_item(
                item, partial=kind == "response.output_item.added"
            )
        if event.get("item_id") in self.custom_ids:
            if kind == "response.function_call_arguments.delta":
                item_id = event["item_id"]
                self.arguments[item_id] = self.arguments.get(item_id, "") + event["delta"]
                return []
            if kind == "response.function_call_arguments.done":
                out += self.finish_input(
                    event, event.get("arguments", self.arguments.get(event["item_id"], ""))
                )
            else:
                out.append(event)
        else:
            out.append(event)
        for value in out:
            if isinstance(value.get("response"), dict):
                value["response"] = self.adapter.response(value["response"])
            value["sequence_number"] = self.sequence
            self.sequence += 1
        return out


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # No prompt, URL, or credential logging.

    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_event(self, event):
        data = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"event: {event['type']}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def forward(self):
        if self.path == "/health" and self.command == "GET":
            self.send_json(200, {"status": "ok", "adapter": "codex-spark"})
            return
        if not self.path.startswith("/v1/"):
            self.send_json(404, {"error": {"message": "Unknown adapter endpoint"}})
            return
        conn = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=900)
        streaming = False
        stop_monitor = threading.Event()
        client_gone = threading.Event()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 64 * 1024 * 1024:
                self.send_json(413, {"error": {"message": "Request exceeds adapter limit"}})
                self.close_connection = True
                return
            if self.headers.get("Content-Encoding", "identity") != "identity":
                self.send_json(415, {"error": {"message": "Compressed requests unsupported"}})
                self.close_connection = True
                return
            body = self.rfile.read(length) if length else None
            adapter = None
            if self.command == "POST" and self.path.split("?")[0] == "/v1/responses":
                adapter = ToolAdapter(json.loads(body))
                body = json.dumps(adapter.request).encode()
            skipped = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in skipped}
            headers["Accept-Encoding"] = "identity"
            conn.request(self.command, self.path, body=body, headers=headers)
            upstream_socket = conn.sock

            def monitor_disconnect():
                # Abort inference even if the client cancels during a long prefill,
                # before vLLM has sent the first streamed token.
                while not stop_monitor.wait(0.25):
                    try:
                        ready, _, _ = select.select([self.connection], [], [], 0)
                        if not ready or self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                            continue
                    except BlockingIOError:
                        continue
                    except OSError:
                        pass
                    client_gone.set()
                    try:
                        upstream_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

            threading.Thread(target=monitor_disconnect, daemon=True).start()
            upstream = conn.getresponse()
            content_type = upstream.getheader("Content-Type", "application/json")
            if adapter and upstream.status == 200 and "text/event-stream" in content_type:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                streaming = True
                events = EventAdapter(adapter)
                data = []
                while True:
                    line = upstream.readline()
                    if not line:
                        break
                    if line in (b"\n", b"\r\n"):
                        if data:
                            payload = b"\n".join(data)
                            if payload != b"[DONE]":
                                for event in events.translate(json.loads(payload)):
                                    self.send_event(event)
                            data = []
                    elif line.startswith(b"data:"):
                        data.append(line[5:].strip())
                    elif line.startswith(b":"):
                        self.wfile.write(line + b"\n")
                        self.wfile.flush()
                self.close_connection = True
            else:
                result = upstream.read()
                if adapter and upstream.status == 200:
                    result = json.dumps(adapter.response(json.loads(result))).encode()
                self.send_response(upstream.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (ValueError, OSError, http.client.HTTPException) as exc:
            if client_gone.is_set():
                self.close_connection = True
                return
            # Log exception class only: exception strings can contain model content.
            logging.error("Adapter failure (%s)", type(exc).__name__)
            message = "Spark tool adapter failed: " + type(exc).__name__
            if streaming:
                try:
                    self.send_event({"type": "error", "code": "spark_adapter_error", "message": message})
                except OSError:
                    pass
                self.close_connection = True
            else:
                self.send_json(502, {"error": {"message": message, "type": "adapter_error"}})
        finally:
            stop_monitor.set()
            conn.close()

    do_GET = forward
    do_POST = forward
    do_DELETE = forward


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18991)
    parser.add_argument("--upstream-port", type=int, default=18888)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Proxy)
    server.upstream_port = args.upstream_port
    server.daemon_threads = True
    logging.basicConfig(level=logging.INFO)
    logging.info("Spark tool adapter listening on loopback port %d", args.port)
    server.serve_forever()
