"""Research export.

Spec 23 §11 is the constraint that shapes this: never copy PostgreSQL's data files, use
controlled application exports, and support CSV, JSON and XLSX. Spec 23 §14 adds that an
export must not corrupt the source — so every query here is read-only and every file is
written to a fresh path rather than updated in place.

XLSX is written without openpyxl. A .xlsx is a zip of XML parts, and emitting the four
minimal parts is ~60 lines with no dependency; pulling in openpyxl for one sheet of strings
would be the larger cost. If styling or formulas are ever needed, that trade flips.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Event, Participant, SyncBatch

EXPORT_FORMATS = ("csv", "json", "xlsx")

# The research dataset's column order. Fixed rather than derived from the model so a column
# added to the table cannot silently reorder an analyst's existing import script.
EVENT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "participant_id",
    "session_id",
    "batch_id",
    "event_type",
    "event_timestamp",
    "created_at",
    "received_at",
    "schema_version",
    "payload",
)


@dataclass(frozen=True)
class ExportScope:
    """What to include. All fields optional; omitting everything exports the whole dataset."""

    participant_id: str | None = None
    event_type: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "participant_id": self.participant_id,
            "event_type": self.event_type,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
        }


def _isoformat(value: Any) -> Any:
    """One value, flattened to something a CSV cell or a JSON file can carry.

    `payload` is stored decoded so it can be queried in SQL, which means it arrives here as a
    dict. `csv.DictWriter` would write Python's repr of that -- `{'intensity': '6'}`, single
    quotes and all -- which is not JSON and not parseable by anything downstream.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def event_rows(db: Session, scope: ExportScope, limit: int | None = None) -> list[dict[str, Any]]:
    """Read-only projection of the events table, oldest first.

    `limit` exists for the quick-look CSV and nothing else. **Exports are deliberately
    uncapped**: an export that silently stopped at N rows is a study dataset with a hole in it
    that nothing downstream can detect, which is a far worse outcome than a slow export, so
    `run_export` never passes one.
    """
    statement = select(Event).order_by(Event.event_timestamp.asc(), Event.event_id.asc())

    if scope.participant_id:
        statement = statement.where(Event.participant_id == scope.participant_id)
    if scope.event_type:
        statement = statement.where(Event.event_type == scope.event_type)
    if scope.since:
        statement = statement.where(Event.event_timestamp >= scope.since)
    if scope.until:
        statement = statement.where(Event.event_timestamp <= scope.until)

    if limit is not None:
        statement = statement.limit(limit)

    return [
        {column: _isoformat(getattr(event, column)) for column in EVENT_COLUMNS}
        for event in db.scalars(statement)
    ]


def participant_summary(db: Session) -> list[dict[str, Any]]:
    """One row per participant with the counts the research team actually asks for."""
    event_counts = dict(
        db.execute(
            select(Event.participant_id, func.count(Event.event_id)).group_by(Event.participant_id)
        ).all()
    )
    batch_counts = dict(
        db.execute(
            select(SyncBatch.participant_id, func.count(SyncBatch.batch_id)).group_by(
                SyncBatch.participant_id
            )
        ).all()
    )
    last_events = dict(
        db.execute(
            select(Event.participant_id, func.max(Event.event_timestamp)).group_by(
                Event.participant_id
            )
        ).all()
    )

    rows = []
    for participant in db.scalars(select(Participant).order_by(Participant.participant_id)):
        rows.append(
            {
                "participant_id": participant.participant_id,
                "study_identifier": participant.study_identifier,
                "app_version": participant.app_version,
                "first_seen_at": _isoformat(participant.first_seen_at),
                "last_seen_at": _isoformat(participant.last_seen_at),
                "event_count": event_counts.get(participant.participant_id, 0),
                "batch_count": batch_counts.get(participant.participant_id, 0),
                "last_event_at": _isoformat(last_events.get(participant.participant_id)),
            }
        )
    return rows


def dataset_stats(db: Session) -> dict[str, Any]:
    """Headline counts for the dashboard."""
    events_by_type = dict(
        db.execute(
            select(Event.event_type, func.count(Event.event_id))
            .group_by(Event.event_type)
            .order_by(func.count(Event.event_id).desc())
        ).all()
    )
    return {
        "participants": db.scalar(select(func.count(Participant.participant_id))) or 0,
        "events": db.scalar(select(func.count(Event.event_id))) or 0,
        "batches": db.scalar(select(func.count(SyncBatch.batch_id))) or 0,
        "events_by_type": events_by_type,
        "latest_event_at": _isoformat(db.scalar(select(func.max(Event.event_timestamp)))),
        "latest_sync_at": _isoformat(db.scalar(select(func.max(SyncBatch.received_at)))),
    }


# --------------------------------------------------------------------------- writers


def write_csv(rows: Sequence[dict[str, Any]], columns: Sequence[str], destination: Path) -> Path:
    # newline="" per the csv module's contract; without it Windows writes \r\r\n.
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return destination


def write_json(rows: Sequence[dict[str, Any]], destination: Path) -> Path:
    destination.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination


def _column_name(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. Needed because the event table is wider than 26 columns."""
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _sheet_xml(rows: Iterable[Sequence[str]]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for row_index, row in enumerate(rows, start=1):
        parts.append(f'<row r="{row_index}">')
        for column_index, value in enumerate(row):
            reference = f"{_column_name(column_index)}{row_index}"
            # Everything is written as an inline string: the payload column holds JSON, and
            # letting Excel type-guess turns ids like "0001" into numbers and mangles them.
            parts.append(
                f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
                f"{escape(value)}</t></is></c>"
            )
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def write_xlsx(
    rows: Sequence[dict[str, Any]], columns: Sequence[str], destination: Path, sheet_name: str = "events"
) -> Path:
    table: list[Sequence[str]] = [list(columns)]
    for row in rows:
        table.append(["" if row.get(column) is None else str(row.get(column)) for column in columns])

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", _sheet_xml(table))
    return destination


def run_export(
    db: Session,
    export_directory: Path,
    export_format: str,
    scope: ExportScope,
    now: datetime | None = None,
) -> tuple[Path, int]:
    """Writes one export file and returns its path and row count."""
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format: {export_format}")

    export_directory.mkdir(parents=True, exist_ok=True)
    rows = event_rows(db, scope)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{scope.participant_id}" if scope.participant_id else ""
    destination = export_directory / f"events-{stamp}{suffix}.{export_format}"

    if export_format == "csv":
        write_csv(rows, EVENT_COLUMNS, destination)
    elif export_format == "json":
        write_json(rows, destination)
    else:
        write_xlsx(rows, EVENT_COLUMNS, destination)

    return destination, len(rows)


def csv_bytes(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> bytes:
    """In-memory CSV, for streaming a download without touching disk."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
