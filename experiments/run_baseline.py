"""Run a small explicit experiment and append reproducible JSONL records."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import uuid
import numpy as np
import yaml
from dotenv import dotenv_values, load_dotenv
from src.benchmarks import BenchmarkItem, GSM8K, AQUARAT
from src.controllers.heuristic import HeuristicController
from src.evaluation import exact_match
from src.llm_client import DEMO_QUESTION, MockLLM, OpenAILLM
from src.runner import run_question
from src.roles import load_roles

ROOT = Path(__file__).resolve().parents[1]


def main(default_topology: str | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--backend', choices=['mock', 'openai', 'gemini'])
    parser.add_argument('--model')
    parser.add_argument('--topology', choices=['fully_connected', 'ring', 'empty', 'static_sparse', 'heuristic', 'mlp'])
    parser.add_argument('--benchmark', choices=['demo', 'gsm8k', 'aqua_rat'])
    parser.add_argument('--roles-config', type=Path)
    parser.add_argument('--split', choices=['train', 'validation', 'test'])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--rounds', type=int)
    parser.add_argument('--agents', type=int)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--checkpoint', type=Path)
    args = parser.parse_args()
    config_path = args.config or ROOT / 'configs/baseline.yaml'
    config = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    if default_topology is not None and args.config is None:
        config['topology'] = default_topology
    for key in ('backend', 'model', 'topology', 'benchmark', 'split', 'limit', 'rounds', 'agents'):
        if getattr(args, key) is not None:
            config[key] = getattr(args, key)
    load_dotenv(ROOT / '.env')
    if config['backend'] == 'gemini':
        local = dotenv_values(ROOT / '.env')
        if local.get('GEMINI_API_KEY') is not None:
            os.environ['GEMINI_API_KEY'] = local['GEMINI_API_KEY']
        config['model'] = config.get('model') or local.get('GEMINI_MODEL') or os.getenv('GEMINI_MODEL') or 'gemini-2.5-flash'
    else:
        config['model'] = config.get('model') or os.getenv('OPENAI_MODEL') or None
    random.seed(config['seed'])
    np.random.seed(config['seed'])
    roles_path = args.roles_config or config.get('roles_config')
    roles = None
    if roles_path:
        roles_path = Path(roles_path)
        if not roles_path.is_absolute():
            roles_path = ROOT / roles_path
        roles = load_roles(roles_path, config['agents'])
        config['roles_config'] = str(roles_path)
        config['roles_sha256'] = hashlib.sha256(roles_path.read_bytes()).hexdigest()
    if config['backend'] == 'mock' and config['benchmark'] != 'demo':
        parser.error('Mock backend is only a routing fixture for --benchmark demo.')
    if config['backend'] == 'gemini':
        from src.gemini_client import GeminiLLM
        backend = GeminiLLM(config['model'], config['max_output_tokens'], config['temperature'],
                            config['timeout'], config.get('gemini_thinking_budget', 0),
                            config.get('gemini_free_tier_confirmed', False), config.get('gemini_rate_limits'),
                            thinking_level=config.get('gemini_thinking_level'))
        try:
            backend.validate_free_access()  # Fail before dataset loading/inference if unconfirmed.
        except Exception:
            backend.close()
            raise
    elif config['backend'] == 'mock':
        backend = MockLLM()
    elif config['backend'] == 'openai':
        backend = OpenAILLM(config['model'], config['max_output_tokens'], config['temperature'],
                            config['timeout'], config['max_retries'])
    else:
        parser.error('Unknown backend; choose mock, openai or gemini.')
    controller = None
    topology = config['topology']
    if topology == 'heuristic':
        controller = HeuristicController(config['heuristic_threshold'], config['top_k'],
                                         config.get('heuristic_disagreement_weight', .6),
                                         config.get('heuristic_consensus_weight', .2),
                                         config.get('heuristic_novelty_weight', .2))
    elif topology == 'mlp':
        from src.controllers.mlp import MLPController
        checkpoint = args.checkpoint or config.get('mlp_checkpoint')
        if not checkpoint:
            parser.error('MLP requires --checkpoint with a trained, validated model.')
        controller = MLPController.load(checkpoint, threshold=config.get('mlp_threshold', .5),
                                        top_k=config['top_k'])
        if not controller.provenance.get('validated'):
            parser.error('MLP checkpoint has no training/validation provenance; no inference was run.')
        config['mlp_checkpoint'] = str(checkpoint)
        config['mlp_checkpoint_sha256'] = hashlib.sha256(Path(checkpoint).read_bytes()).hexdigest()
    benchmark = (AQUARAT(config.get('aqua_revision', AQUARAT.DEFAULT_REVISION), config.get('difficulty_selection'))
                 if config['benchmark'] == 'aqua_rat' else
                 GSM8K(config['validation_fraction'], config['dataset_revision'], config.get('difficulty_selection')))
    items = ([BenchmarkItem('demo:apples', DEMO_QUESTION, '#### 6', 'demo')]
             if config['benchmark'] == 'demo' else benchmark.load(config['split'], config['limit'], config['seed']))
    output = args.output or ROOT / config['output']
    config['output'] = str(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    versions = {}
    for package in ('openai', 'google-genai', 'networkx', 'torch', 'datasets', 'numpy', 'pandas', 'scikit-learn'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    for item in items:
        usage_start = len(backend.usage_records) if config['backend'] == 'gemini' else 0
        try:
            result = run_question(item.question, backend, config['agents'], config['rounds'],
                                  'dynamic' if controller is not None else topology, tuple(config['sparse_edges']),
                                  controller, config['allow_self_loops'], roles=roles,
                                  answer_format=benchmark.answer_format)
        except BaseException:
            if config['backend'] == 'gemini':
                backend.close()
            raise
        if config['backend'] == 'gemini':
            result['provider_usage'] = [{**usage, 'round': i // config['agents'] + 1,
                                         'agent_id': i % config['agents']}
                                        for i, usage in enumerate(backend.usage_records[usage_start:])]
        result.update({'run_id': str(uuid.uuid4()), 'timestamp': datetime.now(timezone.utc).isoformat(),
                       'config': config, 'python': platform.python_version(), 'versions': versions,
                       'benchmark': config['benchmark'], 'split': item.split, 'item_id': item.item_id,
                       'seed': config['seed'], 'ground_truth': item.reference,
                       'correct': benchmark.evaluate(result['prediction'], item.reference),
                       'dataset_provenance': item.provenance})
        with output.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(result) + '\n')
        print(json.dumps(result, indent=2))
    if config['backend'] == 'gemini':
        backend.close()
    print(f'Saved {len(items)} evaluation records to {output}')


if __name__ == '__main__':
    main()
