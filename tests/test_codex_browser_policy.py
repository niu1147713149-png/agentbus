"""Codex should be able to use AgentBus without opening its UI."""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import mcp_server
import pf


class CodexBrowserTests(unittest.TestCase):
    def test_announce_never_launches_ui_for_codex(self):
        with patch("config.is_working_as_member", return_value=False), \
                patch("urllib.request.urlopen") as request, \
                patch.object(pf, "open_agentbus") as launch, \
                patch.dict(os.environ, {"AGENTBUS_SELF": "codex"}):
            mcp_server._announce("Codex CLI")
            request.assert_called_once()
            request.reset_mock()
            request.side_effect = ConnectionRefusedError()
            mcp_server._announce("unknown-client")
            request.assert_called_once()
            launch.assert_not_called()

    def test_initialize_and_join_give_codex_manual_open_guidance(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"clientInfo": {"name": "Codex CLI"}}}
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(init) + "\n")), \
                patch.object(sys, "stdout", out), patch("threading.Thread.start"):
            mcp_server.main()
        hint = mcp_server._ui_hint("codex")
        self.assertIn("不要自动打开", hint)
        self.assertIn(hint, json.loads(out.getvalue())["result"]["instructions"])
        with patch.dict(os.environ, {"AGENTBUS_SELF": "codex"}), \
                patch.object(mcp_server, "gate", return_value=True), \
                patch("config.touch_lead"), patch("config.update_json"), \
                patch("mcp_server.load_config", return_value={}), \
                patch("config.mounted_skills", return_value=[]), patch("config.mounted_packs", return_value=[]), \
                patch("service.state", return_value={"agents": [], "running": []}), \
                patch("service.catalog", return_value={"categories": []}):
            brief = mcp_server.hello({})
        self.assertIn(hint, brief)
        self.assertNotIn("现在就打开", brief)

    def test_explicit_open_still_available(self):
        with patch.object(pf, "open_agentbus", return_value="opened") as launch:
            self.assertEqual(mcp_server.call("agentbus_open", {}), "opened")
            launch.assert_called_once_with()


class CreditsGate(unittest.TestCase):
    """派 GPT 之前的额度闸门：没法确认还有额度，就不派，免得 opencodex 转去扣付费 credits。"""

    NOW = 1_700_000_000.0

    def block(self, q, billing="codex", allow=False):
        import agentbus
        return agentbus._credits_block(billing, q, allow, now=self.NOW)

    def test_fresh_low_usage_is_allowed(self):
        self.assertIsNone(self.block({"ok": True, "weekly": 40, "five_hour": 20, "updated": self.NOW - 60}))

    def test_exhausted_quota_is_blocked(self):
        self.assertIn("额度用完", self.block({"ok": True, "weekly": 100, "five_hour": 3, "updated": self.NOW}))

    def test_unreadable_quota_is_blocked(self):
        self.assertIn("没法确认", self.block({"ok": False, "error": "opencodex 没开"}))

    def test_stale_cache_is_blocked_even_when_it_looks_low(self):
        q = {"ok": True, "stale": True, "weekly": 40, "five_hour": 40, "updated": self.NOW - 3 * 86400}
        self.assertIn("过期", self.block(q))

    def test_old_timestamp_without_stale_flag_is_blocked(self):
        q = {"ok": True, "weekly": 10, "five_hour": 10, "updated": self.NOW - 3600}
        self.assertIn("过期", self.block(q))

    def test_explicit_allow_overrides_everything(self):
        self.assertIsNone(self.block({"ok": False, "error": "x"}, allow=True))
        self.assertIsNone(self.block({"ok": True, "weekly": 100, "updated": self.NOW}, allow=True))

    def test_other_billing_types_are_not_affected(self):
        self.assertIsNone(self.block({"ok": False}, billing="deepseek"))
        self.assertIsNone(self.block({"ok": False}, billing="free"))


if __name__ == "__main__":
    unittest.main()
