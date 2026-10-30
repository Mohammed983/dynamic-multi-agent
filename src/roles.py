"""Opt-in reasoning strategies. Agent IDs stay zero-based for existing graphs."""
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import yaml


@dataclass(frozen=True)
class ReasoningRole:
    name: str
    instruction: str


def parse_roles(rows: Sequence[Mapping[str, str]], count: int) -> tuple[ReasoningRole, ...]:
    if not isinstance(rows, (list, tuple)) or len(rows) != count:
        raise ValueError('Provide exactly one reasoning role per agent.')
    roles = []
    for row in rows:
        if (not isinstance(row, Mapping) or set(row) != {'name', 'instruction'}
                or any(not isinstance(row[key], str) or not row[key].strip() for key in ('name', 'instruction'))):
            raise ValueError('Each role requires only nonempty name and instruction strings.')
        roles.append(ReasoningRole(row['name'].strip(), row['instruction'].strip()))
    if len({role.name for role in roles}) != count:
        raise ValueError('Role names must be unique; ordering fixes the agent/role assignment.')
    return tuple(roles)


def load_roles(path: str | Path, count: int = 4) -> tuple[ReasoningRole, ...]:
    return parse_roles(yaml.safe_load(Path(path).read_text(encoding='utf-8')), count)
