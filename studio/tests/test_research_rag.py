import sys
from pathlib import Path


def _research_rag():
    # pytest is launched from `studio/` in CI; add the repository root for the
    # standalone scripts module without depending on the current directory.
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from scripts.research_rag import read_chunks, search

    return read_chunks, search


def test_research_rag_returns_ranked_source_passages(tmp_path: Path) -> None:
    read_chunks, search = _research_rag()
    (tmp_path / "evidence.md").write_text(
        "# Artifact lineage\n\nInvalidate stale descendants before assembly.\n\n"
        "# Blender validation\n\nCheck GLB magic bytes and render outputs.\n",
        encoding="utf-8",
    )

    results = search("stale descendants lineage", read_chunks(tmp_path), top_k=2)

    assert results
    assert results[0][1].heading == "Artifact lineage"
    assert "stale descendants" in results[0][1].text


def test_research_rag_empty_or_unmatched_query_returns_no_results(tmp_path: Path) -> None:
    read_chunks, search = _research_rag()
    (tmp_path / "evidence.md").write_text("# Evidence\n\nGPU measurements.\n", encoding="utf-8")
    chunks = read_chunks(tmp_path)

    assert search("", chunks) == []
    assert search("MCP permissions", chunks) == []
