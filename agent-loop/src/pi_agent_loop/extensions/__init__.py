"""Explicit capability packages with scoped, reversible startup."""

from .bundle import ExtensionBundle, ExtensionPackage, load_extension_bundle
from .skills import Skill, SkillRegistry
from .mcp import MCPClient, MCPContractChangedError, MCPServerConfig, MCPToolPolicy

__all__ = [
    "ExtensionBundle",
    "ExtensionPackage",
    "Skill",
    "SkillRegistry",
    "load_extension_bundle",
    "MCPClient",
    "MCPContractChangedError",
    "MCPServerConfig",
    "MCPToolPolicy",
]
