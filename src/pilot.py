"""Provider-neutral durable JSONL logging shared by Gemini experiment runners."""
import json
import os
from pathlib import Path


def append_jsonl(path: Path, value: dict) -> None:
    """Flush each event so an interrupted run retains issued-request evidence."""
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
