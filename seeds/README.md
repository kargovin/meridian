# Source rosters

`python -m meridian.db.seed seeds/<roster>.json`

A roster entry is a **publisher** with its **feeds** nested inside it. Rights, jurisdiction and
the rate limit are determinations about an outlet; a URL and how to read it are facts about one
feed, and a publisher can have several — section feeds are the ordinary case.

The feed URL is not derivable from the home URL and must be given. One roster publisher serves
its feed from a different host than its site, another's published feed path 404s, and a third
has no usable feed at all and is discovered by news sitemap instead.

`sources.example.json` shows the shape and is not a roster — the hostnames are reserved example
domains. The v1 roster is not in this repository: each publisher's rights level and
`permitted_to_ingest` are Legal determinations, and a file here asserting `body_text` for a real
publisher would be that determination made by whoever edited the file.

`permitted_to_ingest` is separate from `rights_level` and from `enabled`. It records that a
publisher's terms forbid ingestion at all — which is not a lower rung on the
`body_text`/`headline_only` ladder, and is not the operational stop that gets lifted when an
incident passes.

`user_agent` is optional and overrides the default per publisher. Set it if a publisher stops
responding: a User-Agent carrying a contact URL is black-holed by some edges, and the request
hangs to the timeout rather than being refused, so it reads as an outage rather than a block.

Seeding **inserts and never updates**. A publisher already present keeps its current `enabled`,
`permitted_to_ingest` and `rights_level`, and its feeds are left alone with it — so re-running
this on deploy cannot undo a stop-ingestion change made through the admin surface.
