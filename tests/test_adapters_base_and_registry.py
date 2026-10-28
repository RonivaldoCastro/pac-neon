from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pac_neon.adapters import AdapterRegistry, BaseAgentAdapter, GenericPluginAdapter, register_default_adapters
from pac_neon.models import ProviderMetadata


class DummyCustomAdapter(BaseAgentAdapter):
    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            id="dummy-ai",
            display_name="Dummy AI",
            arcade_title="DUMMY // AI",
            neon_color="#00FF66",
            secondary_color="#FFE600",
            sprite_type="ghost_inky",
            cli_command="dummy-ai",
        )

    def discover_live_agents(self, max_age_minutes: int = 180):
        return []


class TestAdaptersBaseAndRegistry(unittest.TestCase):
    """
    Objective: Validate that BaseAgentAdapter, GenericPluginAdapter, and AdapterRegistry
    allow seamless registration, discovery, and approval sequence formatting for any AI provider.
    """

    def setUp(self):
        register_default_adapters()

    def test_base_adapter_default_behavior_and_approval_sequences(self):
        adapter = DummyCustomAdapter()
        self.assertEqual(adapter.discover_live_agents(), [])
        self.assertEqual(adapter.format_approval_sequence("allow"), "y\r")
        self.assertEqual(adapter.format_approval_sequence("always"), "a\r")
        self.assertEqual(adapter.format_approval_sequence("deny"), "n\r")
        self.assertEqual(adapter.format_approval_sequence("custom", "use --dry-run"), "use --dry-run\r")

    def test_generic_plugin_adapter_preserves_metadata(self):
        meta = ProviderMetadata(
            id="aider-pro",
            display_name="Aider Pro",
            arcade_title="SPEEDY-AIDER // PRO",
            neon_color="#FF2E93",
            secondary_color="#00E5FF",
            sprite_type="ghost_pinky",
            cli_command="aider",
        )
        plugin = GenericPluginAdapter(meta)
        self.assertEqual(plugin.get_metadata().id, "aider-pro")
        self.assertEqual(plugin.get_metadata().display_name, "Aider Pro")
        self.assertEqual(plugin.discover_live_agents(), [])

    def test_adapter_registry_registration_and_dynamic_ensure_provider(self):
        # Default providers should be registered
        for built_in in ("antigravity", "claude", "gemini", "codex", "cursor", "aider"):
            self.assertIsNotNone(AdapterRegistry.get(built_in), f"Missing built-in provider {built_in}")

        all_meta = AdapterRegistry.all_metadata()
        self.assertGreaterEqual(len(all_meta), 6)
        self.assertEqual(len(AdapterRegistry.all_adapters()), len(all_meta))

        # Dynamic provisioning of an unknown future AI CLI
        future_meta = AdapterRegistry.ensure_provider(" Devin-CLI ")
        self.assertEqual(future_meta.id, "devin-cli")
        self.assertIsNotNone(AdapterRegistry.get("devin-cli"))
        self.assertTrue(any(p["id"] == "devin-cli" for p in AdapterRegistry.list_providers()))


if __name__ == "__main__":
    unittest.main()
