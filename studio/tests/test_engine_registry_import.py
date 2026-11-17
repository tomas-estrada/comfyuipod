from __future__ import annotations

import csv
import hashlib
import sys
import textwrap
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


def _importer():
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.import_engine_registry_xlsx import import_registry

    return import_registry


def _write_xlsx(
    path: Path,
    *,
    registry_sheet: str = "Registry",
    sheet_target: str = "worksheets/registry.xml",
) -> bytes:
    workbook = textwrap.dedent(
        f"""\
        <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="{registry_sheet}" sheetId="1" r:id="rId1"/></sheets>
        </workbook>
        """
    ).encode()
    relationships = textwrap.dedent(
        f"""\
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1"
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
            Target="{sheet_target}"/>
        </Relationships>
        """
    ).encode()
    shared_strings = textwrap.dedent(
        """\
        <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <si><t>row_id</t></si><si><t>engine</t></si><si><t>license</t></si>
          <si><t>open timeline</t></si><si><t>MIT</t></si>
        </sst>
        """
    ).encode()
    worksheet = textwrap.dedent(
        """\
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="2">
              <c r="B2" t="s"><v>0</v></c><c r="C2" t="s"><v>1</v></c>
              <c r="D2" t="s"><v>2</v></c>
            </row>
            <row r="3">
              <c r="B3"><v>8</v></c><c r="C3" t="s"><v>3</v></c>
              <c r="D3" t="s"><v>4</v></c>
            </row>
            <row r="4">
              <c r="B4"><f>999999+1</f><v>9</v></c>
              <c r="C4" t="inlineStr"><is><t>second engine</t></is></c>
              <c r="D4" t="inlineStr"><is><t>Apache-2.0</t></is></c>
            </row>
          </sheetData>
        </worksheet>
        """
    ).encode()
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/worksheets/registry.xml", worksheet)
    return path.read_bytes()


def test_import_registry_exports_rows_with_excel_provenance_and_source_hash(tmp_path: Path) -> None:
    import_registry = _importer()
    workbook = tmp_path / "registry.xlsx"
    data = _write_xlsx(workbook)
    output = tmp_path / "registry.csv"

    count = import_registry(workbook, output)

    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert count == 2
    assert [row["engine"] for row in rows] == ["open timeline", "second engine"]
    assert [row["row_id"] for row in rows] == ["8", "9"]
    assert [row["source_excel_row"] for row in rows] == ["3", "4"]
    assert all(row["source_sheet"] == "Registry" for row in rows)
    assert all(row["source_workbook_sha256"] == hashlib.sha256(data).hexdigest() for row in rows)
    # The cached formula value is copied as data; formula code is never evaluated.
    assert rows[1]["row_id"] == "9"

    from scripts.research_rag import read_csv_chunks, search

    results = search("second engine Apache-2.0", read_csv_chunks(tmp_path), top_k=1)
    assert results
    retrieved = results[0][1]
    assert retrieved.external is True
    assert retrieved.heading == "second engine"
    assert dict(retrieved.provenance)["title"] == "second engine"
    assert "source_excel_row: 4" in retrieved.text
    assert "source_sheet: Registry" in retrieved.text
    assert "source_workbook_sha256" in retrieved.text


def test_import_registry_rejects_workbooks_without_registry_sheet(tmp_path: Path) -> None:
    import_registry = _importer()
    workbook = tmp_path / "registry.xlsx"
    _write_xlsx(workbook, registry_sheet="Other")
    output = tmp_path / "registry.csv"

    with pytest.raises(ValueError, match="Registry"):
        import_registry(workbook, output)


def test_import_registry_rejects_sheet_path_outside_workbook_archive(tmp_path: Path) -> None:
    import_registry = _importer()
    workbook = tmp_path / "registry.xlsx"
    _write_xlsx(workbook, sheet_target="../../outside.xml")

    with pytest.raises(ValueError, match="escapes the workbook archive"):
        import_registry(workbook, tmp_path / "registry.csv")


def test_import_registry_rejects_oversized_uncompressed_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import scripts.import_engine_registry_xlsx as importer

    monkeypatch.setattr(importer, "MAX_MEMBER_UNCOMPRESSED_BYTES", 64)
    workbook = tmp_path / "oversized.xlsx"
    with ZipFile(workbook, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"x" * 65)

    with pytest.raises(ValueError, match="uncompressed member exceeds"):
        importer.import_registry(workbook, tmp_path / "registry.csv")
