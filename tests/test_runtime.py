"""Regression checks for Unicode prompts and Windows process completion."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import chat
import mcp_server
import pf
import scheduler
import service
import config


class AtomicStateTests(unittest.TestCase):
    @unittest.skipUnless(pf.IS_WIN, 'Windows file sharing semantics')
    def test_state_write_survives_reader_without_delete_sharing(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            path.write_text('{"old": true}', encoding='utf-8')
            handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            release = threading.Timer(.15, lambda: kernel.CloseHandle(handle))
            release.start()
            try:
                config.save_json(path, {'status': '已完成'})
            finally:
                release.join()
            self.assertEqual(config.load_json(path, {}), {'status': '已完成'})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_progress_save_failure_does_not_kill_executor(self):
        child = "import json,time; print(json.dumps({'type':'turn.started'}),flush=True); time.sleep(3); print('EXECUTOR_FINISHED',flush=True)"
        with tempfile.TemporaryDirectory() as directory, patch.dict(agentbus._CUR, {'running': Path(directory) / 'running.json'}, clear=True), \
                patch.object(agentbus, 'update_json', side_effect=PermissionError('temporarily busy')):
            result = agentbus._run('codex', [sys.executable, '-c', child])
        self.assertEqual(result.returncode, 0)
        self.assertIn('EXECUTOR_FINISHED', result.stdout)


class ProcessStateTests(unittest.TestCase):
    def test_running_and_exited_process_with_retained_handle(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE, **pf.group_kwargs())
        try:
            self.assertTrue(pf.pid_alive(process.pid))
            self.assertIsNone(process.poll())
            process.stdin.close()
            self.assertEqual(process.wait(timeout=10), 0)
            # Keep the Popen object (and its Windows handle) alive during this check.
            self.assertFalse(pf.pid_alive(process.pid))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)

    def test_invalid_pids(self):
        for pid in (None, "", "not-a-pid", 0, -1):
            with self.subTest(pid=pid):
                self.assertFalse(pf.pid_alive(pid))

    @unittest.skipUnless(pf.IS_WIN, "Windows does not have a POSIX ps command")
    def test_mcp_job_state_does_not_require_ps_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / "probe.json").write_text(json.dumps({"pid": os.getpid(), "start": 1}), encoding="utf-8")
            (jobs / "probe.log").write_text("中文日志", encoding="utf-8")
            with patch.object(mcp_server, "JOBS", jobs), patch.object(
                    subprocess, "run", side_effect=AssertionError("must not run ps")):
                self.assertEqual(mcp_server.job_state("probe"), (True, "中文日志", 1))


class PromptEncodingTests(unittest.TestCase):
    def test_prompt_round_trip_when_default_encoding_is_gbk(self):
        prompt = "请仅回复：中文连接正常。项目路径 E:\\示例项目\\agentbus。"
        child = "import sys; data=sys.stdin.buffer.read(); data.decode('utf-8'); sys.stdout.buffer.write(data)"
        # The browser service may spawn Python without UTF-8 mode on Windows.
        with patch("subprocess._text_encoding", return_value="gbk"), patch.dict(agentbus._CUR, {}, clear=True):
            result = agentbus._run("claude", [sys.executable, "-c", child], input=prompt)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, prompt)

    def test_mixed_cli_stdout_uses_final_result_not_diagnostic_logs(self):
        output = 'Loaded config: { debug: false }\n' + json.dumps({"type": "assistant", "message": {"content": []}}) + '\n'
        output += json.dumps({"type": "result", "result": "中文回传正常", "session_id": "probe"}) + '\n'
        self.assertEqual(agentbus.load_json_str(output)["result"], "中文回传正常")
        self.assertEqual(agentbus.load_json_str('[]\n[1]\n'), {})

    def test_progress_is_captured_before_process_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "running.json"
            record.write_text('{}', encoding='utf-8')
            child = "import json,time; print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','input':{'secret':'private'}}]}}),flush=True); time.sleep(3)"
            observed = []

            def inspect(detail):
                if detail:
                    observed.append(detail)

            with patch.dict(agentbus._CUR, {}, clear=True), patch.object(agentbus, "_save_progress", side_effect=inspect):
                result = agentbus._run('codebuddy', [sys.executable, '-c', child])
            self.assertEqual(result.returncode, 0)
            self.assertIn('正在使用工具：Read', observed)
            self.assertNotIn('private', ''.join(observed))


class ChatWorkspaceTests(unittest.TestCase):
    def test_delivery_handoff_ignores_mentions_inside_review(self):
        cfg = {'roster': {'office': ['claude', 'codex', 'codebuddy']}}
        agents = {a: {'name': a, 'run': a} for a in cfg['roster']['office']}
        with patch.object(chat, 'meta', return_value={'handoff_mode': 'single', 'members': list(agents)}), \
                patch.object(chat, 'agents_dict', return_value=agents):
            review = '@codebuddy 的实现需要修正，交给工程审核。\n交接：@codex task_id=sample stage=technical_review revision=1 检查产物'
            self.assertEqual(chat.handoff_targets('产品交付', review, 'claude', cfg), ['codex'])
            self.assertEqual(chat.handoff_targets('产品交付', '@codex 和 @codebuddy 的工作均已完成。', 'claude', cfg), [])
            for invalid in ['交接：@codex 检查\n交接：@codebuddy 修复', '交接：@codex 检查，再 @codebuddy 修复', '交接：@missing 检查']:
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    chat.handoff_targets('产品交付', invalid, 'claude', cfg)

    def test_long_review_cycle_limit_is_local_to_channel(self):
        with patch.object(chat, 'meta', side_effect=lambda name: {'relay_hops': 24} if name == '产品交付' else {}), \
                patch.object(chat, 'lim', return_value={'hops': 4}):
            self.assertEqual(chat.channel_limits('产品交付')['hops'], 24)
            self.assertEqual(chat.channel_limits('大厅')['hops'], 4)

    def test_channel_project_takes_precedence_over_service_directory(self):
        import config
        with patch.object(chat, 'meta', return_value={'project': '共同项目'}), \
                patch.object(config, 'project', return_value=('共同项目', Path('shared'), None, '')) as project:
            self.assertEqual(chat.workspace('群聊'), ('共同项目', 'shared'))
            project.assert_called_once_with('共同项目')

    def test_hidden_agent_cannot_be_mentioned_or_join_group(self):
        cfg = {'roster': {'office': ['claude', 'opencode']}}
        agents = {'claude': {'name': 'Claude', 'run': 'claude'}, 'opencode': {'name': 'OpenCode', 'run': 'opencode', 'hidden': True}}
        with patch.object(chat, 'agents_dict', return_value=agents), patch.object(chat, 'load_config', return_value=cfg), \
                patch.object(chat, 'sealed', return_value=False), patch.object(chat, 'meta', return_value={'members': ['claude', 'opencode']}):
            self.assertEqual(chat.mentions('@opencode @claude', cfg), ['claude'])
            self.assertEqual(chat.talk_members('群聊'), ['claude'])


class JobStatusTests(unittest.TestCase):
    def test_real_slot_contention_clears_queue_when_slot_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / "running.json"
            path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            acquired, errors = threading.Event(), []
            settings = {"screen_lock": False, "limits": {"none": 1}}
            with patch.object(scheduler, "LOCKS", home / "locks"), patch.object(scheduler, "settings", return_value=settings):
                holder = scheduler._try("none", 1, {"pid": os.getpid(), "name": "占用者"})
                self.assertIsNotNone(holder)

                def wait_for_slot():
                    try:
                        with scheduler.acquire("probe", {"name": "测试队员", "billing": "none"}, None, False, path):
                            acquired.set()
                    except Exception as error:
                        errors.append(error)

                waiter = threading.Thread(target=wait_for_slot, daemon=True)
                waiter.start()
                try:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        record = json.loads(path.read_text(encoding="utf-8"))
                        if record.get("queued"):
                            break
                        time.sleep(.02)
                    self.assertIn("占用者", record.get("queued", ""))
                    self.assertFalse(acquired.is_set())
                    pf.unlock(holder)
                    holder.close()
                    self.assertTrue(acquired.wait(5), errors)
                    self.assertNotIn("queued", json.loads(path.read_text(encoding="utf-8")))
                finally:
                    if not holder.closed:
                        pf.unlock(holder)
                        holder.close()
                    waiter.join(5)
                self.assertFalse(waiter.is_alive())
                self.assertFalse(errors)

    def test_queue_running_and_persisted_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            jobs, running = home / "jobs", home / "running"
            jobs.mkdir()
            running.mkdir()
            (jobs / "probe.json").write_text(json.dumps({"pid": os.getpid(), "start": 1}), encoding="utf-8")
            record = {"pid": os.getpid(), "worker": os.getpid(), "queued": "等 Claude 空位"}
            path = running / "probe.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            with patch.object(mcp_server, "JOBS", jobs), patch.object(mcp_server, "HOME", home):
                queued = mcp_server.job_snapshot("probe")
                self.assertEqual((queued["status"], queued["reason"]), ("queued", "等 Claude 空位"))
                record.pop("queued")
                path.write_text(json.dumps(record), encoding="utf-8")
                self.assertEqual(mcp_server.job_snapshot("probe")["status"], "running")
                (jobs / "probe.json").write_text(json.dumps({"pid": os.getpid(), "status": "ok", "reply": "完成"}), encoding="utf-8")
                # Final output takes precedence while the process is still exiting.
                self.assertEqual(mcp_server.job_snapshot("probe")["status"], "ok")

    def test_cancelled_and_interrupted_jobs_are_distinct(self):
        for cancelled, status in [(True, "stopped"), (False, "interrupted")]:
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                jobs, running = home / "jobs", home / "running"
                jobs.mkdir()
                running.mkdir()
                (jobs / "probe.json").write_text('{"pid": 1234, "start": 1}', encoding="utf-8")
                (jobs / "probe.log").write_text("接活了", encoding="utf-8")
                (running / "probe.json").write_text(json.dumps({"pid": 1234, "cancel": cancelled}), encoding="utf-8")
                with patch.object(mcp_server, "JOBS", jobs), patch.object(mcp_server, "HOME", home), patch.object(pf, "pid_alive", return_value=False):
                    self.assertEqual(mcp_server.job_snapshot("probe")["status"], status)

    def test_lost_job_reports_to_chat_without_dispatching_more_work(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            jobs = home / "jobs"
            jobs.mkdir()
            (jobs / "probe.json").write_text('{"pid": 1234, "start": 1}', encoding="utf-8")
            record = {"id": "project/run", "project": "project", "run": "run", "job": "probe", "pid": 1234,
                      "agent": "claude", "channel": "测试", "cancel": True}
            with patch.object(mcp_server, "JOBS", jobs), patch.object(chat, "CHAT", home / "chat"), \
                    patch.object(service, "project", return_value=("project", home, home, "")), \
                    patch.object(service, "ledger_add"), patch.object(chat, "send", side_effect=AssertionError("must not dispatch")), \
                    patch.object(mcp_server, "update_json", side_effect=lambda p, fn: self._update(p, fn)):
                service._record_lost_now(record)
                self.assertEqual(mcp_server.job_snapshot("probe")["status"], "stopped")
                message = chat.read("测试")[-1]
                self.assertTrue(message["system"])
                self.assertIn("手动停止", message["text"])

    @staticmethod
    def _update(path, mutate):
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
