from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.prompts.library import PromptLibrary


def test_prompt_library_load_resolve_render(tmp_path: Path) -> None:
    (tmp_path / "drafter_v1.yaml").write_text(
        """
name: drafter
version: 1
role: drafter
template: "Q: {query}"
variables: [query]
""",
        encoding="utf-8",
    )
    (tmp_path / "drafter_v2.yaml").write_text(
        """
name: drafter
version: 2
role: drafter
template: "Q2: {query}"
variables: [query]
""",
        encoding="utf-8",
    )
    lib = PromptLibrary(str(tmp_path))
    tpl = lib.resolve("drafter_latest")
    assert tpl.version == 2
    out = lib.render("drafter_latest", variables={"query": "hello"})
    assert "Q2: hello" in out


def test_prompt_library_missing_vars(tmp_path: Path) -> None:
    (tmp_path / "critic_v1.yaml").write_text(
        """
name: critic
version: 1
role: critic
template: "{query} {draft}"
variables: [query, draft]
""",
        encoding="utf-8",
    )
    lib = PromptLibrary(str(tmp_path))
    with pytest.raises(ValueError):
        lib.render("critic_v1", variables={"query": "x"})
