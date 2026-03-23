from __future__ import annotations

from pathlib import Path

from orchestrator.retrieval.local_index import search_workspace_code


def test_search_workspace_code_returns_provenance_documents(tmp_path: Path) -> None:
    src = tmp_path / "pkg"
    src.mkdir(parents=True, exist_ok=True)
    target = src / "math_utils.py"
    target.write_text(
        "def add_values(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    noise = src / "notes.md"
    noise.write_text("unrelated text", encoding="utf-8")

    docs = search_workspace_code(
        "python function add_values",
        workspace_root=tmp_path,
        max_results=3,
    )

    assert len(docs) >= 1
    first = docs[0]
    assert first.url.startswith("file://")
    assert first.title.endswith("pkg/math_utils.py")
    assert "add_values" in first.text.value

