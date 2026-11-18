"""Model selection through persistence, capability discovery and CLI dispatch."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import agentbus
import brains
import config
import service


OPTIONS = {
    'models': [{'id': 'catalog-model', 'name': 'Catalog', 'default': 'medium',
                'efforts': ['low', 'medium', 'high', 'xhigh']}],
    'current': {'model': 'custom-current', 'effort': 'max'},
    'custom_model': True,
    'custom_efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
    'switch': True,
}


class ModelChoiceTests(unittest.TestCase):
    def test_wrapped_claude_help_and_settings_are_recognized(self):
        help_text = """  --effort <level>   Effort for this session
                        (low, medium, high, xhigh, max)
  --model <model>        Use an alias (e.g.
                        'fable', 'opus', or 'sonnet') or a full ID.
  --name <name>          Name
"""
        with patch.object(brains, '_help', return_value=help_text) as help_call, \
                patch.object(Path, 'read_text', return_value='{"model":"claude-custom","effortLevel":"high"}'):
            result = brains._claude('configured-claude-aws.cmd')
        help_call.assert_called_once_with(['configured-claude-aws.cmd', '--help'])
        self.assertEqual(result['current'], {'model': 'claude-custom', 'effort': 'high'})
        self.assertIn('fable', [m['id'] for m in result['models']])
        self.assertEqual(result['models'][0]['efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])

    def test_uncatalogued_current_model_keeps_available_efforts(self):
        cache = dict(copy.deepcopy(OPTIONS), t=time.time(), version=brains.CAPABILITIES_VERSION, source_bin=None)
        cache.pop('custom_efforts')
        with patch.dict(brains._mem, {'codex': cache}, clear=True):
            options = brains.options({'run': 'codex'})
        self.assertEqual(options['custom_efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertTrue(options['custom_model'])
        self.assertEqual(brains.model_info(options, 'new-provider/model')['efforts'], options['custom_efforts'])

    def test_full_ids_and_efforts_validate_without_silent_changes(self):
        model = 'arn:aws:bedrock:us-east-1:123:inference-profile/us.anthropic.claude-model-v1:0'
        self.assertEqual(brains.validate_choice({'model': ' ' + model + ' ', 'effort': 'max'}, OPTIONS),
                         {'model': model, 'effort': 'max'})
        self.assertEqual(brains.validate_choice({'model': 'claude-model[1m]', 'effort': ''}, OPTIONS),
                         {'model': 'claude-model[1m]'})
        self.assertEqual(brains.validate_choice({'model': '', 'effort': ''}, OPTIONS), {})
        for choice in [{'model': 'bad&command'}, {'model': '--model'}, {'model': 'bad\nmodel'},
                       {'model': ['wrong-type']}, {'effort': 'unknown'}, {'effort': None},
                       {'model': 'catalog-model', 'effort': 'max'}]:
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                brains.validate_choice(choice, OPTIONS)

    def test_switching_model_with_auto_avoids_incompatible_global_effort(self):
        self.assertEqual(brains.for_run({'model': 'catalog-model'}, OPTIONS),
                         {'model': 'catalog-model', 'effort': 'medium'})
        self.assertEqual(brains.effective({'model': 'catalog-model'}, OPTIONS)[1], 'medium')
        self.assertEqual(brains.for_run({'model': 'custom-id'}, OPTIONS),
                         {'model': 'custom-id', 'effort': 'max'})
        self.assertEqual(brains.for_run({}, OPTIONS), {})

    def test_save_invalid_choice_and_reset_preserve_other_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / 'config.json'
            initial = {'brains': {'claude': {'model': 'opus', 'effort': 'high'}}, 'unrelated': {'keep': True}}
            path.write_text(json.dumps(initial), encoding='utf-8')
            with patch.object(config, 'CONFIG', path), patch.object(config, 'HOME', home), \
                    patch.object(brains, 'options', return_value=OPTIONS):
                saved = service.set_brain('codex', {'model': 'custom-id', 'effort': 'max'})
                self.assertEqual(saved['chosen'], {'model': 'custom-id', 'effort': 'max'})
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    service.set_brain('codex', {'model': 'catalog-model', 'effort': 'max'})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(service.set_brain('codex', {})['chosen'], {})
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), initial)

    def test_saved_choices_reach_each_cli_and_keep_configured_binary(self):
        for kind, choice in [('claude', {'model': 'claude-custom[1m]', 'effort': 'xhigh'}),
                             ('codex', {'model': 'provider/custom-model', 'effort': 'max'}),
                             ('codex', {'model': 'catalog-model'})]:
            with self.subTest(kind=kind, choice=choice), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                record = root / 'probe.json'
                binary = 'configured-claude-aws.cmd' if kind == 'claude' else 'configured-codex.exe'
                (record.with_suffix('.last.md')).write_text('OK', encoding='utf-8')
                result = subprocess.CompletedProcess([], 0, json.dumps({'result': 'OK', 'session_id': 'probe'}), '')
                with patch.object(agentbus, 'load_config', return_value={'brains': {kind: choice}}), \
                        patch.object(brains, 'options', return_value=OPTIONS), \
                        patch.object(agentbus, '_run', return_value=result) as runner:
                    reply, *_ = agentbus.run_agent(kind, {'run': kind, 'bin': binary}, root, 'probe', None, record)
                self.assertEqual(reply, 'OK')
                argv = runner.call_args.args[1]
                self.assertEqual(argv[0], binary)
                if kind == 'claude':
                    self.assertEqual(argv[argv.index('--model') + 1], choice['model'])
                    self.assertEqual(argv[argv.index('--effort') + 1], choice['effort'])
                else:
                    self.assertEqual(argv[argv.index('-m') + 1], choice['model'])
                    self.assertEqual(argv[argv.index('-c') + 1], 'model_reasoning_effort="' + choice.get('effort', 'medium') + '"')


if __name__ == '__main__':
    unittest.main()
