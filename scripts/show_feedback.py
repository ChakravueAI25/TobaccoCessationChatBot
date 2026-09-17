"""Prints what participants have typed into the feedback screen.

Reads PostgreSQL directly, like `show_data.py`, so it answers even when the API is stopped.

### Why this is a script and not a SQL snippet in a document

`events.payload` is a JSON column that holds a JSON **string** - the wire contract sends the
payload as a string rather than a nested object, deliberately, and the column stores that string
as a JSON value. So it is double-encoded, and the query to unwrap it is
`(payload #>> '{}')::json ->> 'feedback'`, which is easy to write wrong and gives an empty
column rather than an error when you do. Doing it in Python with an explicit `json.loads` is the
same operation with a failure that says so.

### Free text, handled as free text

This is the only event type that carries anything a participant wrote in their own words. It is
here because they typed it in order to send it - everything else the study collects is
structured, and spec 16 58 forbids inferring anything from message text. Treat the output
accordingly: it is study data, and it may contain things they did not mean to identify them by,
however plainly the screen asked them not to.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import text  # noqa: E402

from api.app.database import engine  # noqa: E402

EVENT_TYPE = 'participant_feedback'


def unwrap(payload):
    """Returns the payload as a dict, whatever depth of encoding it arrived at."""
    for _ in range(3):
        if isinstance(payload, dict):
            return payload
        if not isinstance(payload, str):
            return {}
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return payload if isinstance(payload, dict) else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--csv', metavar='PATH', help='write to a CSV instead of the terminal')
    args = parser.parse_args()

    try:
        connection = engine.connect()
    except Exception as error:  # noqa: BLE001 - printing the cause is the whole point
        print('Cannot reach PostgreSQL: %s' % type(error).__name__)
        print(str(error).splitlines()[0][:180])
        print('\nIs the database running? Is DATABASE_URL right in .env?')
        print("A password with '@' in it must be percent-encoded as %40.")
        return 1

    with connection:
        rows = connection.execute(
            text(
                'SELECT participant_id, event_timestamp, payload FROM events '
                'WHERE event_type = :kind ORDER BY event_timestamp DESC LIMIT :limit'
            ),
            {'kind': EVENT_TYPE, 'limit': args.limit},
        ).all()

    entries = []
    for participant_id, when, payload in rows:
        body = unwrap(payload).get('feedback', '')
        if body:
            entries.append((participant_id, str(when)[:19], body))

    if args.csv:
        import csv
        with open(args.csv, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['participant_id', 'submitted_at', 'feedback'])
            writer.writerows(entries)
        print('%d entries -> %s' % (len(entries), args.csv))
        return 0

    if not entries:
        print('No feedback yet.')
        print('')
        print('If you expected some: it is a consent-gated research event, so a participant who')
        print('withdrew records nothing, and it uploads on the sync tick rather than instantly.')
        return 0

    print('%d feedback entries, newest first\n' % len(entries))
    for participant_id, when, body in entries:
        print('%s  %s' % (when, participant_id))
        for line in body.splitlines() or ['']:
            print('    %s' % line)
        print('')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
