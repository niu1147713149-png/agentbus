import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import api_agent


def event(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def sse(*parts):
    return b": heartbeat\r\n\r\n" + b"".join(
        ("data: " + (part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)) + "\r\n\r\n").encode()
        for part in parts)


@contextmanager
def relay(respond):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            response = respond(requests)
            if response is None:  # Close before sending an HTTP status line.
                return
            content_type, content = response
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory() as tmp:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
        thread.start()
        root = Path(tmp)
        base = f"http://127.0.0.1:{server.server_port}/v1"
        try:
            with patch.object(api_agent, "SESS", root / "sessions"), \
                 patch.object(api_agent.wallet, "accounts", return_value={"test": {"base": base}}), \
                 patch.object(api_agent.wallet, "key_of", return_value="local-fixture"):
                yield requests, root
        finally:
            server.shutdown()
            thread.join()


def run_job(root, model="gemini-4-argon"):
    return api_agent.run("test", model, "fixture", root)


class GeminiTransportTests(unittest.TestCase):
    def test_stream_required_relay_returns_one_reply_and_usage(self):
        usage = {"prompt_tokens": 9, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}
        frames = sse(event({"content": "已"}), event({"content": "收"}, "stop"),
                     {"choices": [], "usage": usage}, "[DONE]")
        with relay(lambda reqs: ("text/event-stream; charset=utf-8", frames) if reqs[-1][1].get("stream") else None) as (seen, root):
            text, _, _, tokens = run_job(root)
            self.assertEqual(text, "已收")
            self.assertEqual(tokens, {"input": 5, "cache": 4, "output": 3})
            self.assertEqual([p for p, _ in seen], ["/v1/chat/completions"])

    def test_ordinary_json_accepted_without_reposting(self):
        json_reply = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        with relay(lambda _: ("application/json", json_reply)) as (seen, root):
            for model in ("gemini-4-argon", "other-model"):
                self.assertEqual(run_job(root, model)[0], "ok")
            self.assertTrue(seen[0][1]["stream"])
            self.assertNotIn("stream", seen[1][1])
            self.assertEqual(len(seen), 2)

    def test_fragmented_tool_calls_keep_order_and_google_signature(self):
        def tool(index, **fields):
            return event({"tool_calls": [{"index": index, **fields}]})
        first = sse(tool(1, id="b", type="function", function={"name": "list_dir", "arguments": '{"path"'}),
                    tool(0, id="a", type="function", function={"name": "read_file", "arguments": '{"path":"sample'}),
                    tool(0, function={"arguments": '.txt"}'}, extra_content={"google": {"thought_signature": "test-signature"}}),
                    tool(1, function={"arguments": ':""}'}), event({}, "tool_calls"), "[DONE]")
        second = sse(event({"content": "done"}, "stop"), "[DONE]")
        with relay(lambda seen: ("text/event-stream", first if len(seen) == 1 else second)) as (seen, root):
            (root / "sample.txt").write_text("read-me", encoding="utf-8")
            self.assertEqual(run_job(root)[0], "done")
            self.assertEqual(len(seen), 2)
            assistant, read, listing = seen[1][1]["messages"][-3:]
            self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["a", "b"])
            self.assertEqual(assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"], "test-signature")
            self.assertEqual((read["tool_call_id"], read["content"], listing["tool_call_id"]), ("a", "read-me", "b"))

    def test_disconnect_before_headers_not_retried(self):
        with relay(lambda _: None) as (seen, root):
            with self.assertRaisesRegex(api_agent.ApiError, "响应头.*未自动重发"):
                run_job(root)
            self.assertEqual(len(seen), 1)

    def test_unfinished_stream_cannot_write_or_succeed(self):
        call = {"index": 0, "id": "write", "function": {"name": "write_file", "arguments": '{"path":"never","content":"no"}'}}
        with relay(lambda _: ("text/event-stream", sse(event({"content": "partial", "tool_calls": [call]})))) as (seen, root):
            with self.assertRaisesRegex(api_agent.ApiError, "未完整"):
                run_job(root)
            self.assertFalse((root / "never").exists())
            self.assertEqual(len(seen), 1)

    def test_invalid_tool_arguments_fail_before_any_tool_runs(self):
        calls = [{"index": i, "id": str(i), "function": {"name": "write_file", "arguments": args}}
                 for i, args in enumerate(['{"path":"never","content":"no"}', '{"path":"incomplete"'])]
        with relay(lambda _: ("text/event-stream", sse(event({"tool_calls": calls}, "tool_calls"), "[DONE]"))) as (_, root):
            with self.assertRaisesRegex(api_agent.ApiError, "工具调用未完整"):
                run_job(root)
            self.assertFalse((root / "never").exists())

    def test_real_worker_posts_streamed_result_back_to_isolated_chat(self):
        with relay(lambda _: ("text/event-stream", sse(event({"content": "relay-ok"}, "stop"), "[DONE]"))) as (seen, root):
            home = root / "bus-home"
            home.mkdir()
            base = api_agent.wallet.accounts()["test"]["base"]
            account = {"base": base, "provider": "custom", "source": {"kind": "env", "var": "FIXTURE_RELAY_KEY"}}
            agent = {"model": "gemini-4-argon", "name": "fixture-gemini", "account": "test", "base": base}
            (home / "wallet.json").write_text(json.dumps({"accounts": {"test": account}}), "utf-8")
            (home / "config.json").write_text(json.dumps({"api_agents": {"api-1": agent}}), "utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBUS_")}
            env.update(AGENTBUS_HOME=str(home), AGENTBUS_PORT="1", AGENTBUS_CHAT="test", AGENTBUS_SELF="user",
                       FIXTURE_RELAY_KEY="fixture", NO_PROXY="127.0.0.1", PYTHONDONTWRITEBYTECODE="1")
            cmd = [sys.executable, "-X", "utf8", str(Path(api_agent.__file__).with_name("agentbus.py")),
                   "-p", "通用", "ask", "api-1", "fixture", "--new"]
            worker = subprocess.run(cmd, cwd=root, env=env, capture_output=True, encoding="utf-8", timeout=25)
            self.assertEqual(worker.returncode, 0, worker.stderr)
            ledger = json.loads((home / "ledger.jsonl").read_text("utf-8").strip())
            self.assertEqual((ledger["status"], ledger["reply"]), ("ok", "relay-ok"))
            msgs = [json.loads(line) for f in (home / "chat").glob("*.jsonl")
                    for line in f.read_text("utf-8").splitlines()]
            self.assertTrue(any(m["frm"] == "api-1" and m["text"] == "relay-ok" for m in msgs))
            self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
