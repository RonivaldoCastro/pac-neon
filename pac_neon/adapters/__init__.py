from pac_neon.adapters.base import AdapterRegistry, BaseAgentAdapter, GenericPluginAdapter
from pac_neon.adapters.antigravity_adapter import AntigravityAdapter
from pac_neon.adapters.claude_adapter import ClaudeAdapter
from pac_neon.adapters.gemini_codex_adapters import GeminiAdapter, CodexAdapter
from pac_neon.models import ProviderMetadata


def register_default_adapters() -> None:
    AdapterRegistry.register(AntigravityAdapter())
    AdapterRegistry.register(ClaudeAdapter())
    AdapterRegistry.register(GeminiAdapter())
    AdapterRegistry.register(CodexAdapter())
    AdapterRegistry.register(
        GenericPluginAdapter(
            ProviderMetadata(
                id="cursor",
                display_name="Cursor Agent",
                arcade_title="SHADOW-CURSOR // AGENT",
                neon_color="#A855F7",
                secondary_color="#00E5FF",
                sprite_type="ghost_pinky",
                cli_command="cursor",
                supports_pty_wrap=True,
                supports_hooks=True,
            )
        )
    )
    AdapterRegistry.register(
        GenericPluginAdapter(
            ProviderMetadata(
                id="aider",
                display_name="Aider CLI",
                arcade_title="SPEEDY-AIDER // PAIR",
                neon_color="#FF2E93",
                secondary_color="#FFE600",
                sprite_type="ghost_pinky",
                cli_command="aider",
                supports_pty_wrap=True,
                supports_hooks=True,
            )
        )
    )


__all__ = [
    "AdapterRegistry",
    "BaseAgentAdapter",
    "GenericPluginAdapter",
    "AntigravityAdapter",
    "ClaudeAdapter",
    "GeminiAdapter",
    "CodexAdapter",
    "register_default_adapters",
]
