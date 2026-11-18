"""Test chat delivery with synthetic uploads only; never call an installed model."""
import codecs
from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import brains
import chat
import config
import mcp_server
import scheduler
import service
from chat_attachments import INLINE_CHARS, material


class ChatAttachments(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = tmp / "project"
        self.root.mkdir()
        home = tmp / "home"
        self.uploads = home / "chat" / "files"
        self.uploads.mkdir(parents=True)
        for module, attr, value in [(config, "HOME", home), (chat, "CHAT", home / "chat"), (chat, "FILES", self.uploads)]:
            self.stack.enter_context(patch.object(module, attr, value))

    def upload(self, name, data):
        ref, status = self.post("/api/chat/upload", data, {"X-Name": quote(name)})
        self.assertEqual(status, 200)
        return ref

    def post(self, path, body, headers=None):
        handler = SimpleNamespace(_allowed=lambda: True, path=path, headers=headers or {}, _body=body,
                                  _json=lambda data, code=200: (data, code))
        return service.Handler._do_post(handler)

    def unpack(self, files):
        return json.loads(material(files, self.uploads, self.root).splitlines()[-1])

    def test_uploads_are_distinct_and_decoded(self):
        refs = [self.upload("同名.md", raw) for raw in (
            codecs.BOM_UTF8 + "正文一".encode(), "正文二".encode("utf-16"))]
        self.assertNotEqual(refs[0]["path"], refs[1]["path"])
        self.assertEqual([x["text"] for x in self.unpack(refs)], ["正文一", "正文二"])
        self.assertTrue(all(x["complete"] for x in self.unpack(refs)))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_budget_and_binary_copies_are_lossless_and_reusable(self):
        refs = [self.upload("long.md", ("长" * INLINE_CHARS * 2).encode()), self.upload("photo.png", b"\x89PNG\x00raw")]
        items = self.unpack(refs)
        self.assertEqual(self.unpack(refs), items)
        self.assertLessEqual(sum(len(x.get("text", "")) for x in items), INLINE_CHARS)
        self.assertFalse(items[0]["complete"])
        for ref, item in zip(refs, items):
            copy = Path(item["file"])
            self.assertTrue(copy.is_relative_to(self.root))
            self.assertEqual(copy.read_bytes(), Path(ref["path"]).read_bytes())
        Path(items[0]["file"]).write_bytes(b"modified")
        self.assertIn("error", self.unpack(refs)[0])
        self.assertEqual(Path(items[0]["file"]).read_bytes(), b"modified")

    def test_invalid_references_never_reach_dispatch(self):
        outside = self.root / "private.md"
        outside.write_text("outside-marker", encoding="utf-8")
        paths = [outside, self.uploads / "missing.md"]
        for method, name in [(os.link, "hard.md"), (os.symlink, "soft.md")]:
            try:
                link = self.uploads / name
                method(outside, link)
                paths.append(link)
            except OSError:
                pass
        with patch.object(chat, "send") as send:
            for path in paths:
                files = [{"path": str(path), "name": path.name}]
                self.assertIn("error", self.unpack(files)[0])
                self.assertEqual(self.post("/api/chat/send", json.dumps({"text": "test", "files": files}).encode())[1], 400)
            send.assert_not_called()

    def test_group_and_handoff_deliver_actual_cli_input(self):
        ids = ["claude", "codex", "codebuddy"]
        agents = {aid: {"id": aid, "name": aid, "run": aid, "billing": "none"} for aid in ids}
        cfg = {"roster": {"office": ids}}
        bus = self.root / "bus"
        (bus / "runs").mkdir(parents=True)
        self.stack.enter_context(patch.dict(os.environ, {"AGENTBUS_CHAT_FILES": "[]", "AGENTBUS_DEPTH": "0"}))
        self.stack.enter_context(patch.dict(agentbus._CUR, {}, clear=True))
        for module, attr, value in [(chat, "workspace", ("test", str(self.root))), (chat, "lim", chat.DEFAULTS),
                (chat, "meta", {"mode": "talk", "members": ids, "rounds": 1}), (chat, "set_meta", None),
                (chat, "agents_dict", agents), (chat, "load_config", cfg), (chat, "sealed", False),
                (agentbus, "project", ("test", self.root, bus, "")), (agentbus, "load_config", cfg),
                (agentbus, "agents_dict", agents), (agentbus, "inbox", []), (config, "sealed", False),
                (agentbus, "watchdog_cfg", (1, 0)), (agentbus.quota, "snapshot", {}), (brains, "chosen", {}),
                (agentbus, "overlapping", []), (agentbus, "_finish", None), (agentbus, "pf_is_win", True),
                (scheduler, "acquire", MagicMock(waited=0)),
                (mcp_server, "job_snapshot", {"status": "ok", "reply": "（跳过）"})]:
            self.stack.enter_context(patch.object(module, attr, return_value=value))
        self.stack.enter_context(patch.object(agentbus, "RUNNING", bus / "running"))
        board = self.stack.enter_context(patch.object(agentbus, "board_add"))
        prompts, job_env = [], []

        def fake_cli(kind, argv, **kw):
            prompts.append((kind, kw["input"]))
            self.assertNotIn("AGENTBUS_CHAT_FILES", kw["env"])
            self.assertNotIn("END-9321", str(argv))
            if kind == "codex":
                Path(argv[argv.index("-o") + 1]).write_text("ok", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, '{"result":"ok"}', '')

        def fake_job(args, cwd, env_extra):
            self.assertNotIn("END-9321", str(args))
            job_env.append(dict(env_extra))
            ns = SimpleNamespace(sender="user", agent=args["agent"], tag=args.get("tag"), task=args["task"],
                                 file=None, project="test", new=True)
            with patch.dict(os.environ, env_extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exitcode:
                agentbus.cmd_ask(ns)
            self.assertEqual(exitcode.exception.code, 0)
            return f"fake-{len(prompts)}"

        self.stack.enter_context(patch.object(agentbus, "_run", side_effect=fake_cli))
        self.stack.enter_context(patch.object(mcp_server, "start_job", side_effect=fake_job))
        body = "START-1634\n" + "合成需求\n" * 2200 + "\nEND-9321"
        ref = self.upload("需求.md", body.encode("utf-8"))
        previous = self.upload("old.md", b"PREVIOUS-TOPIC")
        chat.append("A", "user", "previous", files=[previous])
        with patch.object(chat, "start_talk") as start:
            ack, code = self.post("/api/chat/send", json.dumps({"channel": "A", "text": "review", "files": [ref]}).encode())
            self.assertEqual(code, 200)
            start.assert_called_once_with("A", ack["message"]["id"], str(self.root))
        additional = self.upload("extra.md", b"FOLLOWUP")
        chat.append("A", "user", "additional notes", files=[additional])
        chat.append("A", "user", "@codex private", files=[previous])
        chat.run_talk("A", ack["message"]["id"])
        self.assertEqual([aid for aid, _ in prompts], ids)
        for _, prompt in prompts:
            self.assertNotIn("PREVIOUS-TOPIC", prompt)
            self.assertNotIn(str(self.uploads), prompt)
            data = json.loads(prompt.splitlines()[-1])
            self.assertEqual([x["text"] for x in data], [body, "FOLLOWUP"])
            self.assertTrue(data[0]["complete"])
        chat.dispatch("A", ids[0], "review", "user", 0, files=[ref])
        with patch.dict(os.environ, job_env[-1]):
            chat.dispatch("A", ids[1], "handoff", ids[0], 1)
            chat.dispatch("B", ids[2], "other channel", ids[0], 1)
            chat.dispatch("A", ids[2], "new assignment", "user", 0)
        self.assertEqual(["END-9321" in p for _, p in prompts[-4:]], [True, True, False, False])
        self.assertNotIn("END-9321", str(board.call_args_list))


if __name__ == "__main__":
    unittest.main()
