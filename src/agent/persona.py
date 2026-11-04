"""The editable tone of the agent, re-read from config/persona.yaml on every model call.

Non-developers change it without a restart. It is added after the core rules and is marked as style
only: safety rules live in code. A broken edit keeps the last good version, so it can't take the
agent down. In production this is a versioned table behind an admin console.
"""
import logging
from pathlib import Path

import yaml

from agent.config import CONFIG_DIR
from agent.observability import emit

MAX_PERSONA_CHARS = 1500


class Persona:
    def __init__(self, path: Path = CONFIG_DIR / "persona.yaml") -> None:
        self.path = path
        self._last_good = ""

    def text(self) -> str:
        try:
            tone = yaml.safe_load(self.path.read_text())["tone"]
            if not isinstance(tone, str):
                raise ValueError("`tone` must be text")
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as e:
            emit("persona_invalid", logging.ERROR, error=str(e)[:200])
            return self._last_good
        self._last_good = tone.strip()[:MAX_PERSONA_CHARS]
        return self._last_good
