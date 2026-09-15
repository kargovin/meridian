# Source rosters

`python -m meridian.db.seed seeds/<roster>.json`

A roster entry is a **publisher** with its **feeds** nested inside it. Rights, jurisdiction and
the rate limit are determinations about an outlet; a URL and how to read it are facts about one
feed, and a publisher can have several — section feeds are the ordinary case.

The feed URL is not derivable from the home URL and must be given. One roster publisher serves
its feed from a different host than its site, another's published feed path 404s, and a third
has no usable feed at all and is discovered by news sitemap instead.

## The files

- **`v1.json`** — the v1 roster. Twenty-two publishers: eight whose licence permits storing and
  summarizing the article body (`body_text`), six whose terms permit showing the feed but not
  using the body (`headline_only`), and eight whose terms refuse ingestion outright
  (`permitted_to_ingest: false`). Every one carries a `determination` naming the clause it rests
  on, where it was read and when.
- **`sources.example.json`** — the shape, on reserved example domains. Not a roster.

## What the file must say

Two of the fields are **human determinations read out of a publisher's terms**, and no code can
derive either:

- **`permitted_to_ingest`** — may we touch this outlet at all? It records that a publisher's terms
  forbid ingestion — which is not a lower rung on the `body_text`/`headline_only` ladder, and is
  not the operational stop (`enabled`) that gets lifted when an incident passes. ⚠️ **It is
  required on every publisher, never defaulted from the file.** The column defaults to true in
  the database, so an omitted key and a considered "yes" would be the same bytes on disk and a
  reader could not tell which publishers were actually looked at.
- **`rights_level`** — assuming yes, how much of each article may we hold? `body_text` means we
  may store the body and generate a summary from it; `headline_only` means headline, blurb and
  link from the feed, and the body is never fetched or stored.

Each publisher also carries a **`determination`**: `read_on` (an ISO date), `basis` (the clause,
quoted or closely paraphrased), `sources` (the URLs it was read from) and optionally
`conditions` (attribution wording, non-commercial-only, ShareAlike). It is validated and then
discarded — there is no column for it. **The file is its record**, and the point of it is that
the next person re-checks the cited page rather than re-deciding from scratch.

A publisher whose terms refuse us **stays in the file**. Deleting the entry loses the
determination, and the next person adds the outlet back as a new source having never seen it.

**Unknown keys are an error**, on publishers, feeds and determinations alike. A misspelled
`permitted_to_ingest` that parsed clean would fall through to the database default and poll a
publisher the file meant to refuse. `note` is the one free-text field allowed on a feed.

## What decides the rights level

The licence **variant**, not the licence family. Creative Commons `BY` and `BY-SA` permit
derivatives; `-ND` (NoDerivatives) does not, and a generated summary is a derivative. `-NC`
(NonCommercial) is a condition, not a refusal — it is what makes openDemocracy and WHO usable
for a personal, non-commercial deployment, and it is why those two determinations say they must
be re-made before any commercial or public launch.

`robots.txt` and the terms are different artifacts and can disagree in both directions. Several
mainstream publishers block no AI agents in robots.txt and refuse automated access in their
terms; one permits the paths and forbids the use. The terms decide.

## Acquisition tier vs rights

`acquisition_tier` says *how* a body would be obtained; `rights_level` says *whether* it may be.
They are set independently, and **the code does not cross-check them**: discovery stores the
body of any `1_full_feed` feed without consulting rights. So a headline-only publisher whose feed
happens to ship full text is given `3_extraction` here, not `1_full_feed`, and a test over this
file holds that rule. It is a rule about the file's contents, not a guard in the system — an
operator can still set a headline-only publisher's feed to `1_full_feed` through the admin
surface, and the next poll would store bodies. The code guard is an open architecture item.

## Operational notes

`user_agent` is optional and overrides the default per publisher. Set it if a publisher stops
responding: a User-Agent carrying a contact URL is black-holed by some edges, and the request
hangs to the timeout rather than being refused, so it reads as an outage rather than a block.

Seeding **inserts and never updates**. A publisher already present keeps its current `enabled`,
`permitted_to_ingest` and `rights_level`, and its feeds are left alone with it — so re-running
this on deploy cannot undo a stop-ingestion change made through the admin surface. Matching is
on `home_url`, which is not canonicalised: a trailing slash or a `www.` is a different publisher.
Fix the file, not the registry.
