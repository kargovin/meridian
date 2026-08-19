# Source rosters

`python -m meridian.db.seed seeds/<roster>.json`

`sources.example.json` shows the shape and is not a roster — the hostnames are reserved
example domains. The v1 roster is not in this repository: each source's rights level is a
Legal determination, and a file here asserting `body_text` for a real publisher would be
that determination made by whoever edited the file.

Seeding **inserts and never updates**. A source already present keeps its current
`enabled` and `rights_level`, so re-running this on deploy cannot undo a stop-ingestion
change made through the admin surface.
