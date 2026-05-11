"""YAML-based prompt loader with hot-reload support.

Prompts are loaded from config/prompts/*.yaml at startup and can be
reloaded via SIGHUP or dashboard admin panel without restart.

Each prompt YAML defines: version, model, timeout, max_tokens, temperature,
fallback_action, system prompt, user_template, and response_schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from strictyaml import dirty_load  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PromptConfig:
    """A single loaded prompt configuration."""

    name: str
    version: str
    model: str
    timeout_ms: int
    max_tokens: int
    temperature: float
    fallback_action: str
    system: str
    user_template: str
    response_schema: dict[str, Any] = field(default_factory=dict)

    def render_user(self, **kwargs: Any) -> str:
        """Render the user template with provided variables."""
        try:
            return self.user_template.format(**kwargs)
        except KeyError as e:
            logger.error("Missing template var %s in prompt %s", e, self.name)
            raise


class PromptLoader:
    """Loads and caches prompt configurations from YAML files.

    Hot-reload: call reload() to re-read all files from disk.
    """

    def __init__(self, prompts_dir: str | Path = "config/prompts") -> None:
        self._dir = Path(prompts_dir)
        self._prompts: dict[str, PromptConfig] = {}
        self._load_all()

    @property
    def available_prompts(self) -> list[str]:
        """List of loaded prompt names."""
        return list(self._prompts.keys())

    def get(self, name: str) -> PromptConfig:
        """Get a prompt by name (filename without extension)."""
        if name not in self._prompts:
            raise KeyError(
                f"Prompt '{name}' not found. Available: {self.available_prompts}"
            )
        return self._prompts[name]

    def reload(self) -> int:
        """Hot-reload all prompts from disk. Returns count of loaded prompts."""
        self._prompts.clear()
        return self._load_all()

    def _load_all(self) -> int:
        """Load all YAML files from the prompts directory."""
        if not self._dir.exists():
            logger.warning("Prompts directory does not exist: %s", self._dir)
            return 0

        count = 0
        for yaml_file in sorted(self._dir.glob("*.yaml")):
            if yaml_file.name.startswith("_"):
                # Skip schema/meta files
                continue
            try:
                config = self._load_file(yaml_file)
                self._prompts[config.name] = config
                count += 1
                logger.debug("Loaded prompt: %s (v%s)", config.name, config.version)
            except Exception as exc:
                logger.error("Failed to load prompt %s: %s", yaml_file, exc)

        logger.info("PromptLoader: %d prompts loaded from %s", count, self._dir)
        return count

    def _load_file(self, path: Path) -> PromptConfig:
        """Parse a single YAML prompt file."""
        parsed = dirty_load(path.read_text(), allow_flow_style=True)
        raw = parsed.data
        if not isinstance(raw, dict):
            raise ValueError(f"Expected dict, got {type(raw).__name__}")

        name = path.stem  # filename without .yaml
        return PromptConfig(
            name=name,
            version=str(raw.get("version", "1")),
            model=raw.get("model", "qwen"),
            timeout_ms=int(raw.get("timeout_ms", 500)),
            max_tokens=int(raw.get("max_tokens", 256)),
            temperature=float(raw.get("temperature", 0.0)),
            fallback_action=raw.get("fallback_action", "hold"),
            system=raw.get("system", ""),
            user_template=raw.get("user_template", ""),
            response_schema=raw.get("response_schema", {}),
        )
