"""TOML package loading with explicit factories, dependency order and rollback."""

from __future__ import annotations

import inspect
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..context import content_digest
from ..async_utils import await_owned_cleanup
from ..types import AgentTool
from .skills import SkillRegistry


@dataclass(frozen=True, slots=True)
class ExtensionPackage:
    name: str
    tools: tuple[AgentTool, ...] = ()
    resources: tuple[Any, ...] = ()
    validators: Mapping[str, Any] = field(default_factory=dict)
    version: str = "1"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.version, str)
            or not self.version.strip()
        ):
            raise ValueError("extension name/version must be non-empty")
        object.__setattr__(self, "validators", MappingProxyType(dict(self.validators)))


async def _close(resources: list[Any]) -> None:
    errors: list[BaseException] = []
    seen: set[int] = set()
    for resource in reversed(resources):
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
        if not callable(closer):
            errors.append(TypeError("owned extension resource requires close/aclose"))
            continue
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            errors.append(error)
    if errors:
        raise BaseExceptionGroup("extension cleanup failed", errors)


class ExtensionBundle:
    def __init__(
        self, packages: list[ExtensionPackage], skills: SkillRegistry | None = None
    ) -> None:
        self.packages = tuple(packages)
        self.skills = skills
        self.tools = tuple(tool for package in packages for tool in package.tools) + (
            () if skills is None else (skills.create_tool(),)
        )
        if any(not isinstance(tool, AgentTool) for tool in self.tools):
            raise TypeError("extensions must provide AgentTool instances")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("duplicate extension tool name")
        self.validators: dict[str, Any] = {}
        for package in packages:
            for name, validator in package.validators.items():
                if name in self.validators or not callable(validator):
                    raise ValueError("duplicate or non-callable extension validator")
                self.validators[name] = validator
        self.version = content_digest(
            {
                "packages": [(p.name, p.version) for p in packages],
                "skills": None if skills is None else skills.version,
            }
        )
        self.closed = False

    async def aclose(self) -> None:
        if self.closed:
            return
        await await_owned_cleanup(
            _close(
                [
                    resource
                    for package in self.packages
                    for resource in package.resources
                ]
            )
        )
        self.closed = True

    async def __aenter__(self) -> ExtensionBundle:
        if self.closed:
            raise RuntimeError("extension bundle is closed")
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()


async def load_extension_bundle(
    config_path: str | Path, *, package_factories: Mapping[str, Callable[..., Any]]
) -> ExtensionBundle:
    path = Path(config_path).resolve(strict=True)
    if path.stat().st_size > 262_144:
        raise ValueError("extension configuration exceeds size limit")
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if set(data) - {"packages", "skills"}:
        raise ValueError("unknown extension configuration field")
    rows = data.get("packages", [])
    if not isinstance(rows, list):
        raise ValueError("packages must be an array")
    catalogue: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
            "name",
            "factory",
            "depends_on",
            "settings",
        }:
            raise ValueError("invalid extension package configuration")
        name = row.get("name")
        factory = row.get("factory", name)
        dependencies = row.get("depends_on", [])
        if not isinstance(name, str) or not name.strip() or name in catalogue:
            raise ValueError("missing or duplicate extension name")
        if not isinstance(factory, str) or factory not in package_factories:
            raise ValueError("missing explicit extension factory")
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError("extension depends_on must contain unique names")
        if not isinstance(row.get("settings", {}), dict):
            raise ValueError("extension settings must be a table")
        catalogue[name] = {**row, "factory": factory, "depends_on": dependencies}
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting or name not in catalogue:
            raise ValueError("cyclic or missing extension dependency")
        if name in order:
            return
        visiting.add(name)
        for dependency in catalogue[name]["depends_on"]:
            visit(dependency)
        visiting.remove(name)
        order.append(name)

    for name in catalogue:
        visit(name)
    loaded: dict[str, ExtensionPackage] = {}
    try:
        for name in order:
            row = catalogue[name]
            result = package_factories[row["factory"]](
                row.get("settings", {}), {key: loaded[key] for key in row["depends_on"]}
            )
            package = await result if inspect.isawaitable(result) else result
            if not isinstance(package, ExtensionPackage):
                raise TypeError("extension factory must return ExtensionPackage")
            loaded[name] = package
            if package.name != name:
                raise ValueError(
                    "extension factory returned a different package identity"
                )
        skill_paths = data.get("skills", [])
        if isinstance(skill_paths, dict):
            if set(skill_paths) != {"directories"}:
                raise ValueError("skills table only supports directories")
            skill_paths = skill_paths["directories"]
        if not isinstance(skill_paths, list) or any(
            not isinstance(item, str) for item in skill_paths
        ):
            raise ValueError("skills must be an array of explicit directory paths")
        skills = (
            SkillRegistry([path.parent / item for item in skill_paths])
            if skill_paths
            else None
        )
        return ExtensionBundle(list(loaded.values()), skills)
    except BaseException as error:
        try:
            await await_owned_cleanup(
                _close(
                    [
                        resource
                        for package in loaded.values()
                        for resource in package.resources
                    ]
                )
            )
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "extension activation and rollback failed", [error, cleanup_error]
            )
        raise
