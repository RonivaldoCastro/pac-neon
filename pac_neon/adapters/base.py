from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Type
from pac_neon.models import AgentUnit, ProviderMetadata


class BaseAgentAdapter(ABC):
    """Base interface for any AI coding agent provider (Antigravity, Claude, Gemini, Codex, etc.)."""

    @abstractmethod
    def get_metadata(self) -> ProviderMetadata:
        """Returns visual and technical metadata for this provider."""
        raise NotImplementedError

    @abstractmethod
    def discover_live_agents(self, max_age_minutes: int = 180) -> List[AgentUnit]:
        """Scans local logs, transcripts, or processes to discover running/recent agents."""
        raise NotImplementedError

    def format_approval_Sequence(self, action: str, custom_text: Optional[str] = None) -> str:
        """Returns the exact PTY stdin sequence to approve/deny a permission prompt for this CLI."""
        if custom_text:
            return custom_text.rstrip("\r\n") + "\r"
        if action == "allow":
            return "y\r"
        if action == "always":
            return "a\r"
        if action == "deny":
            return "n\r"
        return "\r"


class GenericPluginAdapter(BaseAgentAdapter):
    """Dynamic adapter for newly registered future AI agents (e.g., Cursor, Aider, Windsurf, Custom)."""

    def __init__(self, metadata: ProviderMetadata):
        self._metadata = metadata

    def get_metadata(self) -> ProviderMetadata:
        return self._metadata

    def discover_live_agents(self, max_age_minutes: int = 180) -> List[AgentUnit]:
        return []


class AdapterRegistry:
    """Global registry of all AI Agent Provider Adapters."""

    _adapters: Dict[str, BaseAgentAdapter] = {}

    @classmethod
    def register(cls, adapter: BaseAgentAdapter) -> None:
        meta = adapter.get_metadata()
        cls._adapters[meta.id.lower()] = adapter

    @classmethod
    def get(cls, provider_id: str) -> Optional[BaseAgentAdapter]:
        return cls._adapters.get(provider_id.lower())

    @classmethod
    def all_adapters(cls) -> List[BaseAgentAdapter]:
        return list(cls._adapters.values())

    @classmethod
    def all_metadata(cls) -> List[ProviderMetadata]:
        return [a.get_metadata() for a in cls._adapters.values()]

    @classmethod
    def list_providers(cls) -> List[Dict[str, str]]:
        return [a.get_metadata().model_dump() for a in cls._adapters.values()]

    @classmethod
    def ensure_provider(cls, provider_id: str) -> ProviderMetadata:
        existing = cls.get(provider_id)
        if existing:
            return existing.get_metadata()
        pid = provider_id.lower().strip()
        meta = ProviderMetadata(
            id=pid,
            display_name=provider_id.capitalize(),
            arcade_title=f"NEON-{pid.upper()} // PLUGIN",
            neon_color="#FF2E93",
            secondary_color="#00E5FF",
            sprite_type="ghost_pinky",
            cli_command=pid,
        )
        cls.register(GenericPluginAdapter(meta))
        return meta

    @classmethod
    def discover_all(cls, max_age_minutes: int = 180) -> List[AgentUnit]:
        discovered: List[AgentUnit] = []
        for adapter in cls._adapters.values():
            try:
                discovered.extend(adapter.discover_live_agents(max_age_minutes=max_age_minutes))
            except Exception:
                continue
        return discovered
