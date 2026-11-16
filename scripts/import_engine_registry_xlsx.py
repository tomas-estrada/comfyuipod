#!/usr/bin/env python3
"""Export the Registry sheet of an .xlsx catalog to provenance-rich CSV.

This deliberately uses only Python's standard library. Cell values are copied
as data; formulas are never evaluated and workbook text is never interpreted
as instructions. The repository RAG indexes the resulting CSV row by row.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import posixpath
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REF_RE = re.compile(r"^([A-Z]+)")
PROVENANCE_COLUMNS = ("source_sheet", "source_excel_row", "source_workbook_sha256")


def _cell_column(cell_ref: str) -> int:
    match = CELL_REF_RE.match(cell_ref.upper())
    if not match:
        raise ValueError(f"invalid spreadsheet cell reference: {cell_ref!r}")
    result = 0
    for char in match.group(1):
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1


def _text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        return "".join(inline.itertext()) if inline is not None else ""
    value = cell.findtext(f"{{{MAIN_NS}}}v")
    if value is None:
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as exc:
            raise ValueError(f"invalid shared-string index: {value!r}") from exc
    # Formula result cells use their cached value. Formula expressions in <f>
    # are intentionally ignored; no workbook code is executed.
    return value


def _read_shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(item.itertext()) for item in root.findall(f"{{{MAIN_NS}}}si")]


def _registry_sheet_path(archive: ZipFile) -> str:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise ValueError("workbook is missing its workbook metadata") from exc

    registry = next(
        (
            sheet
            for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet")
            if sheet.get("name") == "Registry"
        ),
        None,
    )
    if registry is None:
        raise ValueError("workbook does not contain a Registry sheet")
    relationship_id = registry.get(f"{{{DOC_REL_NS}}}id")
    relation = next(
        (
            item
            for item in relationships.findall(f"{{{PKG_REL_NS}}}Relationship")
            if item.get("Id") == relationship_id
        ),
        None,
    )
    if relation is None:
        raise ValueError("Registry sheet has no workbook relationship")

    target = (relation.get("Target") or "").lstrip("/")
    member = posixpath.normpath(
        target if target.startswith("xl/") else posixpath.join("xl", target)
    )
    if not member.startswith("xl/") or member == "xl/..":
        raise ValueError("Registry sheet target escapes the workbook archive")
    return member


def import_registry(input_path: Path, output_path: Path) -> int:
    """Export Registry records, returning the number of non-empty engine rows."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input workbook and output CSV must be different files")
    source_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()

    try:
        with ZipFile(input_path) as archive:
            sheet_path = _registry_sheet_path(archive)
            sheet = ET.fromstring(archive.read(sheet_path))
            shared_strings = _read_shared_strings(archive)
    except BadZipFile as exc:
        raise ValueError("input is not a valid .xlsx workbook") from exc
    except KeyError as exc:
        raise ValueError(f"workbook is missing archive member {exc}") from exc

    rows = sheet.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row")
    header_cells: dict[int, str] | None = None
    header_row_number: int | None = None
    for row in rows:
        values = {
            _cell_column(cell.get("r", "")): _text(cell, shared_strings)
            for cell in row.findall(f"{{{MAIN_NS}}}c")
        }
        names = {value.strip().lower() for value in values.values() if value.strip()}
        if {"row_id", "engine"}.issubset(names):
            header_cells = values
            header_row_number = int(row.get("r", "0"))
            break
    if header_cells is None or header_row_number is None:
        raise ValueError("Registry sheet has no row containing row_id and engine headers")

    header = [
        header_cells[index].strip() for index in sorted(header_cells) if header_cells[index].strip()
    ]
    if len(header) != len(set(header)):
        raise ValueError("Registry sheet contains duplicate column headers")
    if set(PROVENANCE_COLUMNS).intersection(header):
        raise ValueError("Registry sheet uses a reserved provenance column name")
    header_indexes = [index for index in sorted(header_cells) if header_cells[index].strip()]
    engine_index = next(
        index for index in header_indexes if header_cells[index].strip().lower() == "engine"
    )
    output_fields = [*header, *PROVENANCE_COLUMNS]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            row_number = int(row.get("r", "0"))
            if row_number <= header_row_number:
                continue
            cells = {
                _cell_column(cell.get("r", "")): _text(cell, shared_strings)
                for cell in row.findall(f"{{{MAIN_NS}}}c")
            }
            if not cells.get(engine_index, "").strip():
                continue
            record = {
                name: cells.get(column, "")
                for name, column in zip(header, header_indexes, strict=True)
            }
            if not record.get("row_id", "").strip():
                raise ValueError(f"Registry row {row_number} has an engine but no row_id")
            record.update(
                source_sheet="Registry",
                source_excel_row=str(row_number),
                source_workbook_sha256=source_hash,
            )
            writer.writerow(record)
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source .xlsx workbook")
    parser.add_argument("output", type=Path, help="destination UTF-8 CSV")
    args = parser.parse_args(argv)
    try:
        count = import_registry(args.input, args.output)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Exported {count} Registry rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
