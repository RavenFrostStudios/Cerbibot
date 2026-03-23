from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from string import Formatter
from typing import Any

import yaml


@dataclass(slots=True)
class PromptTemplate:
    name: str
    version: int
    role: str
    template: str
    variables: list[str]

    @property
    def template_id(self) -> str:
        return f"{self.name}_v{self.version}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()


class PromptLibrary:
    def __init__(self, directory: str):
        self.directory = Path(directory).expanduser()
        self.templates: dict[str, PromptTemplate] = {}
        self._latest_by_name: dict[str, PromptTemplate] = {}
        self._latest_by_role: dict[str, PromptTemplate] = {}
        self._load()

    def _load(self) -> None:
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.yaml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            template = PromptTemplate(
                name=str(raw.get("name", "")).strip(),
                version=int(raw.get("version", 1)),
                role=str(raw.get("role", "general")).strip(),
                template=str(raw.get("template", "")),
                variables=[str(v) for v in list(raw.get("variables", []))],
            )
            if not template.name:
                continue
            self.templates[template.template_id] = template
            current_name = self._latest_by_name.get(template.name)
            if current_name is None or template.version > current_name.version:
                self._latest_by_name[template.name] = template
            current_role = self._latest_by_role.get(template.role)
            if current_role is None or template.version > current_role.version:
                self._latest_by_role[template.role] = template

    def list_templates(self) -> list[PromptTemplate]:
        return sorted(self.templates.values(), key=lambda t: (t.role, t.name, t.version))

    def resolve(self, selector: str, *, role: str | None = None) -> PromptTemplate:
        selector = selector.strip()
        if selector.endswith("_latest"):
            name = selector[: -len("_latest")]
            tpl = self._latest_by_name.get(name)
            if tpl is None:
                raise ValueError(f"Unknown prompt selector: {selector}")
            return tpl
        if selector in self.templates:
            return self.templates[selector]
        if selector.endswith("_v"):
            raise ValueError(f"Invalid prompt selector: {selector}")
        if role is not None and selector in self._latest_by_name:
            return self._latest_by_name[selector]
        raise ValueError(f"Unknown prompt selector: {selector}")

    def resolve_for_role(self, role: str, selection: dict[str, str]) -> PromptTemplate | None:
        selector = selection.get(role)
        if selector:
            return self.resolve(selector, role=role)
        return self._latest_by_role.get(role)

    def render(self, selector: str, *, variables: dict[str, Any], role: str | None = None) -> str:
        tpl = self.resolve(selector, role=role)
        required = set(tpl.variables)
        provided = set(variables.keys())
        missing = sorted(required - provided)
        if missing:
            raise ValueError(f"Missing prompt variables for {tpl.template_id}: {missing}")
        fields = {f for _, f, _, _ in Formatter().parse(tpl.template) if f}
        unresolved = sorted(fields - provided)
        if unresolved:
            raise ValueError(f"Template {tpl.template_id} has unresolved placeholders: {unresolved}")
        return tpl.template.format(**variables)

    def selected_hashes(self, selection: dict[str, str]) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for role, selector in selection.items():
            try:
                tpl = self.resolve(selector, role=role)
            except Exception:
                continue
            out[role] = {"template_id": tpl.template_id, "hash": tpl.content_hash}
        return out
