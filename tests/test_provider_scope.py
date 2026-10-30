"""Gemini/mock scope and legacy-provider independence; no real provider access."""
import ast
import json
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values
import pytest
import yaml

from experiments import run_baseline

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('via_config', [False, True])
def test_removed_provider_is_rejected_before_credentials_or_clients(tmp_path, monkeypatch, via_config):
    def forbidden(*args, **kwargs):
        pytest.fail('Removed backend reached credential loading')
    monkeypatch.setattr(run_baseline, 'load_dotenv', forbidden)
    monkeypatch.setattr(run_baseline, 'dotenv_values', forbidden)
    if via_config:
        cfg = yaml.safe_load((ROOT / 'configs/baseline.yaml').read_text())
        cfg['backend'] = 'openai'
        path = tmp_path / 'unsupported.yaml'
        path.write_text(yaml.safe_dump(cfg))
        args = ['--config', str(path)]
    else:
        args = ['--backend', 'openai']
    monkeypatch.setattr(sys, 'argv', ['test', *args])
    with pytest.raises(SystemExit) as caught:
        run_baseline.main()
    assert caught.value.code == 2


def test_active_sources_and_requirements_have_no_removed_sdk_dependency():
    for folder in ('src', 'experiments'):
        for path in (ROOT / folder).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = ([name.name for name in node.names] if isinstance(node, ast.Import)
                         else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                assert not any(name.split('.')[0] in {'openai', 'tiktoken'} for name in names), path
    requirements = (ROOT / 'requirements.txt').read_text().splitlines()
    assert not any(line.lower().startswith(('openai', 'tiktoken')) for line in requirements)
    assert not (ROOT / 'experiments/real_pilot.py').exists()


def test_environment_example_is_gemini_only_and_contains_no_key():
    values = dotenv_values(ROOT / '.env.example')
    assert set(values) == {'GEMINI_API_KEY', 'GEMINI_MODEL'}
    assert not values['GEMINI_API_KEY']
    assert values['GEMINI_MODEL'] == 'gemini-3.5-flash-lite'


def test_all_project_modules_import_without_legacy_packages_or_network():
    code = r'''
import importlib
import importlib.abc
from pathlib import Path
import socket
import sys

class RemovedPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'tiktoken'}:
            raise ModuleNotFoundError('Legacy provider package intentionally unavailable')

def blocked(*args, **kwargs):
    raise AssertionError('Network access attempted during offline imports')

sys.meta_path.insert(0, RemovedPackages())
socket.create_connection = blocked
socket.socket.connect = blocked
for folder in ('src', 'experiments'):
    for path in Path(folder).rglob('*.py'):
        module = '.'.join(path.with_suffix('').parts).removesuffix('.__init__')
        importlib.import_module(module)
assert not any(name.split('.')[0] in {'openai', 'tiktoken'} for name in sys.modules)
print('Gemini/mock modules import without legacy packages or network')
'''
    process = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                             capture_output=True, text=True, timeout=90)
    assert process.returncode == 0, process.stderr


def test_mock_cli_needs_no_provider_key_and_logs_no_legacy_sdk(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_baseline, 'load_dotenv', lambda *args: None)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    output = tmp_path / 'mock.jsonl'
    monkeypatch.setattr(sys, 'argv', ['test', '--backend', 'mock', '--output', str(output)])
    run_baseline.main()
    capsys.readouterr()
    record = json.loads(output.read_text())
    assert record['simulated'] and record['prediction'] == '6'
    assert 'openai' not in record['versions'] and 'tiktoken' not in record['versions']
