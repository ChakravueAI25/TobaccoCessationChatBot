import argparse
import csv
import json
from pathlib import Path
from sqlalchemy import create_engine, text


def export_events(database_url: str, output: Path, format_name: str = "json") -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with create_engine(database_url).connect() as connection:
        rows = [dict(row._mapping) for row in connection.execute(text("SELECT event_id, participant_id, event_type, event_timestamp, received_at, schema_version, payload FROM events ORDER BY received_at"))]
    for row in rows:
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
    if format_name == "json":
        output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    elif format_name == "csv":
        with output.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=rows[0].keys() if rows else ["event_id"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        raise ValueError("XLSX export requires an approved spreadsheet dependency")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args()
    print(export_events(args.database_url, args.output, args.format))