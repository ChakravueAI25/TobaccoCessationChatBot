"""Build a participant-level analysis frame from a controlled research export."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

REQUIRED_EVENT_COLUMNS = {"participant_id", "event_type", "event_timestamp", "payload"}
FRAME_COLUMNS = [
    "participant_id",
    "cpd_baseline",
    "cpd_endline",
    "readiness_baseline",
    "readiness_endline",
    "craving_week1_mean",
    "craving_lastweek_mean",
    "sessions",
    "turns",
    "cravings_handled",
    "lapses",
    "coping_selected_n",
    "safety_events",
    "fallback_rate",
    "completed",
    "quit_attempt_made",
    "abstinent_7day",
    "days_active",
    "first_seen",
    "last_seen",
]


def load_events(path: Path) -> pd.DataFrame:
    """Load only the CSV/JSON formats produced by the backend export endpoints."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        events = pd.read_csv(path, dtype=str)
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("events", data)
        if not isinstance(data, list):
            raise ValueError("JSON export must contain a list of event rows")
        events = pd.DataFrame(data)
    else:
        raise ValueError("analytics inputs must be controlled CSV or JSON exports")

    missing = REQUIRED_EVENT_COLUMNS - set(events.columns)
    if missing:
        raise ValueError(f"export is missing required columns: {', '.join(sorted(missing))}")
    if events.empty:
        return events.assign(_timestamp=pd.Series(dtype="datetime64[ns, UTC]"))

    events = events.copy()
    events["_timestamp"] = pd.to_datetime(events["event_timestamp"], utc=True, errors="coerce")
    if events["_timestamp"].isna().any():
        raise ValueError("export contains an invalid event_timestamp")
    events["_payload"] = events["payload"].map(_parse_payload)
    return events


def _parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_payload_value(rows: pd.DataFrame, event_type: str, key: str) -> float | str | None:
    matching = rows.loc[rows["event_type"] == event_type, "_payload"]
    for payload in matching:
        value = payload.get(key)
        number = _number(value)
        if number is not None:
            return number
        if value not in (None, ""):
            return str(value)
    return None


def _mean_craving(rows: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> float | None:
    matching = rows.loc[
        (rows["event_type"] == "craving_reported")
        & (rows["_timestamp"] >= start)
        & (rows["_timestamp"] <= end),
        "_payload",
    ]
    values = [_number(payload.get("intensity")) for payload in matching]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def build_analysis_frame(events: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for participant_id, participant_events in events.groupby("participant_id", sort=True):
        participant_events = participant_events.sort_values("_timestamp")
        first_seen = participant_events["_timestamp"].iloc[0]
        last_seen = participant_events["_timestamp"].iloc[-1]
        last_week_start = max(first_seen, last_seen - pd.Timedelta(days=7))
        turns = participant_events["event_type"].eq("conversation_turn").sum()
        fallback_turns = sum(
            str(payload.get("response_source", "")).upper() == "FALLBACK"
            for payload in participant_events.loc[
                participant_events["event_type"] == "conversation_turn", "_payload"
            ]
        )
        model_failures = participant_events["event_type"].eq("model_failure").sum()
        fallback_rate = (fallback_turns or model_failures) / turns if turns else None
        endline = participant_events.loc[participant_events["event_type"] == "endline_survey", "_payload"]

        rows.append(
            {
                "participant_id": participant_id,
                "cpd_baseline": _first_payload_value(participant_events, "baseline_survey", "cpd_baseline"),
                "cpd_endline": _first_payload_value(participant_events, "endline_survey", "cpd_endline"),
                "readiness_baseline": _first_payload_value(participant_events, "baseline_survey", "readiness_baseline"),
                "readiness_endline": _first_payload_value(participant_events, "endline_survey", "readiness_endline"),
                "craving_week1_mean": _mean_craving(participant_events, first_seen, first_seen + pd.Timedelta(days=7)),
                "craving_lastweek_mean": _mean_craving(participant_events, last_week_start, last_seen),
                "sessions": participant_events["event_type"].eq("session_started").sum(),
                "turns": turns,
                "cravings_handled": participant_events["event_type"].eq("craving_reported").sum(),
                "lapses": participant_events["event_type"].eq("lapse_reported").sum(),
                "coping_selected_n": participant_events["event_type"].eq("coping_selected").sum(),
                "safety_events": participant_events["event_type"].eq("safety_event").sum(),
                "fallback_rate": fallback_rate,
                "completed": "Y" if not endline.empty else "N",
                "quit_attempt_made": _first_payload_value(participant_events, "endline_survey", "quit_attempt_made"),
                "abstinent_7day": _first_payload_value(participant_events, "endline_survey", "abstinent_7day"),
                "days_active": participant_events["_timestamp"].dt.date.nunique(),
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
            }
        )
    return pd.DataFrame(rows, columns=FRAME_COLUMNS)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="controlled events CSV or JSON export")
    parser.add_argument("output", type=Path, nargs="?", default=Path("analysis_frame.csv"))
    args = parser.parse_args(argv)
    frame = build_analysis_frame(load_events(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output, index=False)
    print(f"wrote {len(frame)} participants to {args.output}")


if __name__ == "__main__":
    main()
