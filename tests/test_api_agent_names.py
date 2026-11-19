from copy import deepcopy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import chat
import config
import service


class ApiAgentNames(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(config, "CONFIG", home / "config.json"))
        stack.enter_context(patch.object(config, "HOME", home))
        self.old = {"api_agents": {"api-1": {"name": "原名", "account": "stub", "model": "model"}},
                    "clones": {"api-1-2": {"base": "api-1", "name": "分身"}},
                    "lineups": {"项目": {"jobs": {"api-1": {"tag:UI 设计": "prefer"}}}}}
        config.save_json(config.CONFIG, self.old)

    def rename(self, aid, name):
        handler = SimpleNamespace(path="/api/api-agent/rename", headers={}, _allowed=lambda: True,
                                  _body=json.dumps({"agent": aid, "name": name}).encode(),
                                  _json=lambda obj, status=200: (status, obj))
        return service.Handler._do_post(handler)

    def test_persistent_name_and_mentions_without_changing_id_or_roles(self):
        expected = deepcopy(self.old)
        for aid, group, name in (("api-1", "api_agents", "Gemini 设计师"), ("api-1-2", "clones", "原型审稿")):
            status, reply = self.rename(aid, "  " + name + "  ")
            self.assertEqual((status, reply), (200, {"ok": True, "name": name}))
            expected[group][aid]["name"] = name
            raw = config.load_json(config.CONFIG, {})
            self.assertEqual(raw, expected)
            self.assertEqual(config.agents_dict(raw)[aid]["name"], name)
            self.assertEqual(chat.mentions("@" + name + " 请审稿", raw), [aid])
            self.assertEqual(chat.mentions("@" + aid, raw), [aid])

    def test_create_uses_the_same_name_rules_as_rename(self):
        import wallet
        account = {"stub": {"provider": "custom", "base": "https://example.invalid/v1", "name": "stub"}}
        with patch.object(wallet, "accounts", lambda: account):
            for name in ["所有人小王", "all hands", "everyone", "Allen 2 @x", "api-1", "原名", "claude"]:
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        service.new_api_agent("stub", "model", name)
            self.assertEqual(config.load_json(config.CONFIG, {}), self.old)
            allen = service.new_api_agent("stub", "model", "Allen")
            self.assertEqual(config.load_json(config.CONFIG, {})["api_agents"][allen]["name"], "Allen")
            aid = service.new_api_agent("stub", "model", "  新同事  ")
            raw = config.load_json(config.CONFIG, {})
            self.assertEqual(raw["api_agents"][aid]["name"], "新同事")
            self.assertEqual(chat.mentions("@新同事 你好", raw), [aid])

    def test_rename_returns_the_stored_name_and_keeps_config_intact(self):
        self.assertEqual(service.rename_api_agent("api-1", "  新名字  "), "新名字")
        raw = config.load_json(config.CONFIG, {})
        self.assertEqual(raw["api_agents"]["api-1"]["name"], "新名字")
        self.assertEqual(raw["clones"], self.old["clones"])

    def test_name_prefixed_with_all_is_not_a_broadcast(self):
        raw = {"api_agents": {"api-1": {"name": "Allen", "account": "stub", "model": "m"}},
               "roster": {"office": ["api-1"]}}
        with patch.object(chat, "sealed", lambda a: False):
            self.assertEqual(chat.mentions("@Allen 看一下", raw), ["api-1"])
            self.assertEqual(chat.mentions("@allergy 是什么意思", raw), [])
            self.assertEqual(chat.mentions("@all 开会", raw), ["api-1"])

    def test_reject_collisions_reserved_mentions_invalid_names_and_wrong_ids(self):
        for aid, name in [
            ("api-1", " "), ("api-1", "x" * 49), ("api-1", "Gpt"),
            ("api-1", "CODEX"), ("api-1", "分身"), ("api-1", "所有人"),
            ("api-1", "All hands"), ("api-1", "x@claude"), ("api-1", "x\ny"),
            ("api-1", None), ("codex", "改名"), ("missing", "改名"), ([], "改名"),
        ]:
            with self.subTest(aid=aid, name=name):
                status, reply = self.rename(aid, name)
                self.assertEqual(status, 400)
                self.assertIn("error", reply)
                self.assertEqual(config.load_json(config.CONFIG, {}), self.old)


if __name__ == "__main__":
    unittest.main()
