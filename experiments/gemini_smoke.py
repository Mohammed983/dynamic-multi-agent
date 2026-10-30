"""One optional free-tier Gemini inference; never restarts the ten-question pilot."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.metadata
import json
import logging
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
import yaml

from src.agents import Agent
from src.evaluation import exact_match, extract_answer
from src.gemini_client import GeminiError, GeminiLLM
from src.llm_client import DEMO_QUESTION

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/gemini.yaml')
    parser.add_argument('--run', action='store_true', help='Issue one inference only after free-tier gates pass.')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    config = yaml.safe_load(args.config.read_text(encoding='utf-8'))
    values = dotenv_values(ROOT / '.env')
    # Do not treat an unrelated ambient key/GOOGLE_API_KEY as the requested local key.
    local_key = (values.get('GEMINI_API_KEY') or '').strip()
    state = {'timestamp': datetime.now(timezone.utc).isoformat(), 'backend': 'gemini',
             'model': config.get('model') or values.get('GEMINI_MODEL') or 'gemini-2.5-flash',
             'simulated': False, 'key_present': bool(local_key),
             'free_tier_confirmed': config.get('gemini_free_tier_confirmed') is True,
             'rate_limits': config.get('gemini_rate_limits'), 'generation_calls_issued': 0,
             'generation_requested': args.run, 'status': 'not_run',
             'inference_settings': {'max_output_tokens': config['max_output_tokens'],
                                    'temperature': config['temperature'],
                                    'thinking_budget': config.get('gemini_thinking_budget', 0),
                                    'thinking_level': config.get('gemini_thinking_level'),
                                    'sdk_attempts_per_request': 1},
             'sdk_version': importlib.metadata.version('google-genai')}
    backend = None
    try:
        if not local_key:
            state.update(status='blocked_missing_key', reason='GEMINI_API_KEY is absent from the local .env file.')
        else:
            # Constructor reads GEMINI_API_KEY, so deliberately load only the local
            # Gemini credential rather than preferring a preexisting environment value.
            import os
            load_dotenv(ROOT / '.env')
            os.environ['GEMINI_API_KEY'] = local_key
            backend = GeminiLLM(state['model'], config['max_output_tokens'], config['temperature'],
                                config['timeout'], config.get('gemini_thinking_budget', 0),
                                state['free_tier_confirmed'], state['rate_limits'],
                                thinking_level=config.get('gemini_thinking_level'))
            state['model_metadata'] = backend.verify_model()  # Metadata GET, no generation.
            backend.validate_free_access()
            state['status'] = 'preflight_passed'
            if args.run:
                prompt = Agent(0, backend).build_prompt(DEMO_QUESTION, ())
                completion = backend.generate(prompt)
                state.update(status='completed', question=DEMO_QUESTION, response=asdict(completion),
                             prediction=extract_answer(completion.text), ground_truth='#### 6',
                             correct=exact_match(extract_answer(completion.text), '#### 6'),
                             usage=backend.usage_records[-1])
    except (GeminiError, ValueError) as exc:
        # Only locally authored/sanitized errors reach this handler.
        state.update(status='blocked_or_failed', reason=str(exc))
    finally:
        if backend is not None:
            state['generation_calls_issued'] = backend.generation_calls_issued
            state['provider_usage'] = list(backend.usage_records)
            backend.close()
    output = ROOT / 'results/gemini_smoke.jsonl'
    with output.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(state) + '\n')
    print(json.dumps(state, indent=2))


if __name__ == '__main__':
    main()
