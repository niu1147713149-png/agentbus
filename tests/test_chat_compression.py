from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import chat
import config
import pf
import service


class ChatCompression(unittest.TestCase):
    def setUp(self):
        stack = self.stack = ExitStack()
        self.addCleanup(stack.close)
        home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for module, key, value in ((config, "CONFIG", home / "config.json"), (config, "PROJECTS", home / "projects.json"), (config, "HOME", home),
                                   (chat, "CHAT", home / "chat"), (pf, "HOME", home)):
            stack.enter_context(patch.object(module, key, value))
        self.raw = {"api_agents": {"api-test": {"name": "Test", "model": "stub", "account": "stub"}}}
        config.save_json(config.CONFIG, self.raw)
        stack.enter_context(patch.dict(service._compress_status, clear=True))
        stack.enter_context(patch.dict(service._compress_fail, clear=True))
        stack.enter_context(patch.object(service, "_compress_rev", 0))
        self.run = stack.enter_context(patch.object(pf, "run_tree", side_effect=AssertionError("unexpected call")))
        self.ch = "temporary"

    def add(self, text, **extra):
        return chat.append(self.ch, "user", text, **extra)

    def test_auto_check_skips_unchanged_channel_files(self):
        self.add("x" * 100)
        with patch.object(service, "_start_compression", return_value={"status": "noop"}) as start:
            service._auto_compress()
            service._auto_compress()
            self.assertEqual(start.call_count, 1)
            self.add("y" * 100)
            service._auto_compress()
            self.assertEqual(start.call_count, 2)

    def test_short_channel_over_budget_and_one_long_message(self):
        for i in range(7):
            self.add(str(i) * 2500)
        u = chat.usage(self.ch)
        self.assertGreater(u["chars"], u["compress_at"])
        self.assertTrue(0 < u["eligible"] < 7)
        with patch.object(chat, "_summarize", return_value="approved decision"):
            self.assertEqual(chat.compress(self.ch), "approved decision")
        next_u = chat.usage(self.ch)
        ctx = chat.context(self.ch)
        self.assertTrue(ctx.startswith(next_u["summary_preview"]))
        self.assertIn("6" * 2500, ctx)
        self.assertLessEqual(len(ctx), u["budget"])
        self.assertEqual(next_u["eligible"], 0)
        self.assertIsNone(chat.compress(self.ch, force=True))
        self.assertEqual(len([m for m in chat.read(self.ch) if not m.get("system")]), 7)

        self.ch = "single"
        self.add("long" * 2100)
        self.assertEqual(chat.usage(self.ch)["eligible"], 1)
        self.assertIsNone(chat.compress(self.ch))  # honor configured automatic threshold

    def test_full_batches_previous_summary_cursor_retry_and_concurrent_append(self):
        chat.set_meta(self.ch, summary="PRIOR")
        ids = [self.add(f"ID{i:02d} " + "x" * 3400)["id"] for i in range(22)]
        old = chat.usage(self.ch)["eligible"]
        prompts = []

        def reply(channel, duty, previous, material, cap):
            prompts.append((previous, material))
            if len(prompts) == 2:
                raise chat.CompressionError("simulated")
            return "FIRST"
        with patch.object(chat, "_summarize", side_effect=reply):
            with self.assertRaises(chat.CompressionError):
                chat.compress(self.ch, force=True)
        self.assertEqual(prompts[0][0], "PRIOR")
        self.assertEqual(prompts[1][0], "FIRST")
        checkpoint = chat.meta(self.ch)
        n = ids.index(checkpoint["summary_upto"]) + 1
        self.assertGreater(n, 0)
        self.assertLess(n, old)
        self.assertEqual(checkpoint["summary"], "FIRST")
        seen, previous = [], []

        def retry(channel, duty, carry, text, cap):
            if not seen:
                self.add("arrived during compression")
            previous.append(carry)
            seen.append(text)
            return f"retry-{len(seen)}"

        with patch.object(chat, "_summarize", side_effect=retry):
            chat.compress(self.ch, force=True)
        self.assertEqual(previous, ["FIRST"] + [f"retry-{i}" for i in range(1, len(seen))])
        self.assertTrue(all(len(x) <= chat.BATCH_CHARS for x in [prompts[0][1], *seen]))
        combined = prompts[0][1] + "".join(seen)
        for i in range(old):
            self.assertEqual(combined.count(f"ID{i:02d} "), 1)
        self.assertEqual(chat.meta(self.ch)["summary_upto"], ids[old - 1])
        self.assertIn("arrived during compression", chat.context(self.ch))

    def test_oversized_row_cursor_waits_for_last_fragment(self):
        self.add("abc123" * 11000)
        plan = chat.compression_plan(self.ch)
        expected = plan[1][0][1]
        with patch.object(chat, "_summarize", side_effect=["part", chat.CompressionError("interrupted")]):
            with self.assertRaises(chat.CompressionError):
                chat.compress(self.ch, force=True)
        self.assertIsNone(chat.meta(self.ch)["summary_upto"])
        with patch.object(chat, "_summarize", return_value="summary") as parts:
            chat.compress(self.ch, force=True)
        self.assertEqual("".join(c.args[3] for c in parts.call_args_list), expected)

    def test_no_2000_row_loss_and_legacy_cursor(self):
        first = self.add("EARLIEST")
        for i in range(2001):
            self.add(str(i))
        self.assertEqual(chat.usage(self.ch)["messages"], 2002)
        self.assertEqual(chat.compression_plan(self.ch)[1][0][0]["id"], first["id"])
        chat.set_meta(self.ch, summary_upto=first["id"], summary="prior", summary_pos=19)
        self.assertEqual(chat.usage(self.ch)["messages"], 2001)

    def test_nonzero_blank_or_oversized_model_reply_never_advances(self):
        self.add("x" * 17000)
        self.run.side_effect = None
        for rc, content in [(1, "upstream error"), (0, " "), (0, "x" * 2001)]:
            self.run.return_value = subprocess.CompletedProcess([], rc, content, "")
            with self.assertRaises(chat.CompressionError):
                chat.compress(self.ch)
            self.assertFalse(chat.meta(self.ch)["summary_upto"])
        args = self.run.call_args
        self.assertGreater(len(args.kwargs["input"]), 17000)
        self.assertLess(len(str(args.args[0])), 1024)
        self.assertLess(args.args[0].index("--project"), args.args[0].index("ask"))

    def test_stdin_utf8_across_real_subprocess(self):
        # Tiny local echo worker; never contacts a provider or the running service.
        with patch.object(pf, "run_tree", self.original_run_tree):
            out = pf.run_tree([sys.executable, "-X", "utf8", "-c",
                               "import sys; print(len(sys.stdin.read()))"], timeout=10,
                              input="中" * 40000, encoding="utf-8")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "40000"))

    original_run_tree = staticmethod(pf.run_tree)

    def test_http_noop_running_duplicate_failure_retry_and_done(self):
        def request():
            h = SimpleNamespace(path="/api/chat/compress", headers={}, _allowed=lambda: True,
                                _body=json.dumps({"channel": self.ch}).encode(), _json=lambda x, status=200: x)
            return service.Handler._do_post(h)
        self.add("small")
        self.assertEqual(request()["status"], "noop")
        self.add("x" * 17000)
        with patch.object(service.threading, "Thread") as thread:
            self.assertEqual(request()["status"], "running")
            self.assertEqual(request()["status"], "running")
            self.assertEqual(thread.call_count, 1)
            with patch.object(chat, "_summarize", side_effect=chat.CompressionError("retry")):
                thread.call_args.kwargs["target"]()
            self.assertEqual(service._compression_state(self.ch)["status"], "failed")
            service._auto_compress()
            self.assertEqual(thread.call_count, 1)
            self.assertEqual(request()["status"], "running")
            with patch.object(chat, "_summarize", return_value="summary"):
                thread.call_args.kwargs["target"]()
            self.assertEqual(service._compression_state(self.ch)["status"], "done")
            self.assertEqual(request()["status"], "noop")
        self.assertEqual(service._compress_rev, 4)

    def test_no_api_reports_failure_without_checkpoint(self):
        self.add("x" * 18000)
        config.save_json(config.CONFIG, {})
        with patch.object(service.threading, "Thread") as thread:
            self.assertEqual(service._start_compression(self.ch)["status"], "failed")
            thread.assert_not_called()
        self.assertIsNone(chat.meta(self.ch)["summary_upto"])


if __name__ == "__main__":
    unittest.main()
