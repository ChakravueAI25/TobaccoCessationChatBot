Analytics scripts consume controlled JSON/CSV exports only. They must never write to the source PostgreSQL database.

Build the participant-level frame from an export:

```text
uv run python analytics/scripts/build_analysis_frame.py exports/events-20260831T060440Z.csv analytics/analysis_frame.csv
```

Then print the paired-analysis report:

```text
uv run python analytics/scripts/pilot_report.py analytics/analysis_frame.csv
```

The scripts intentionally do not accept `.xlsx` or database connection settings. Generate a
controlled CSV or JSON export from the backend first; this keeps the analysis reproducible and
prevents an analyst script from bypassing the export boundary.