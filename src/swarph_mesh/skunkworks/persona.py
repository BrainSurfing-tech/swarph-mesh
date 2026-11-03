"""Persona pack loader: reads personas/<name>.md.

A pack is one MD file with two required H1 sections, ``# IDEATION_SYSTEM`` and
``# ARCHITECT_SYSTEM`` (system prompts for those two stages), and an optional
``# DEFAULTS`` section of ``key: value`` lines. Any other line of the form
``# word`` starts a new section, so prose inside a section must not use it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

_PERSONAS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "personas")
_H1_RE = re.compile(r"^# (\w+)\s*$", re.MULTILINE)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass
class PersonaPack:
    name: str
    ideation_system: str
    architect_system: str
    defaults: Dict[str, str] = field(default_factory=dict)
    source_path: Optional[str] = None


def load_persona(name: str, *, base_dir: Optional[str] = None) -> PersonaPack:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"persona name {name!r} must match {_NAME_RE.pattern}")
    base = base_dir or _PERSONAS_DIR
    path = os.path.join(base, f"{name}.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"persona pack {name!r} not found at {path}")

    with open(path, encoding="utf-8") as f:
        text = f.read()

    sections = _split_h1_sections(text)
    for required in ("IDEATION_SYSTEM", "ARCHITECT_SYSTEM"):
        if required not in sections:
            raise ValueError(
                f"persona {name!r} at {path} missing required # {required} section"
            )

    defaults: Dict[str, str] = {}
    for line in (sections.get("DEFAULTS") or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            k, v = line.split(":", 1)
            defaults[k.strip()] = v.strip()

    return PersonaPack(
        name=name,
        ideation_system=sections["IDEATION_SYSTEM"].strip(),
        architect_system=sections["ARCHITECT_SYSTEM"].strip(),
        defaults=defaults,
        source_path=path,
    )


def _split_h1_sections(text: str) -> Dict[str, str]:
    matches = list(_H1_RE.finditer(text))
    out: Dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1)] = text[m.end() : end]
    return out
