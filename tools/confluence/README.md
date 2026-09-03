# Confluence authoring toolchain

Stop hand-writing XHTML/ADF. Author phase docs as Markdown in `docs/`, render
deterministically, push over REST. The page body never enters model context.

## Why

| Route | What the model emits for a 60k-char page | Notes |
|---|---|---|
| MCP `updateConfluencePage` with HTML | ~65k chars markup | inline in a tool call; the whole body is retyped for *any* edit |
| Raw ADF JSON | ~135k chars | worst case; JSON overhead is ~2.2× the prose |
| **This toolchain** | **~28k chars Markdown** | ~5× cheaper than ADF, and native nodes are guaranteed correct |
| **Editing an existing doc** | **~200 chars** | Edit tool touches only changed lines, then re-push |

That last row is the real win. Revisions stop costing a full re-emission.

## Use

```sh
python3 tools/confluence/push.py docs/phase-3-sprint-plan.md            # dry run: census + size
python3 tools/confluence/push.py docs/phase-3-sprint-plan.md --publish  # write + verify
python3 tools/confluence/check.py                                       # all docs vs live pages
python3 tools/confluence/check.py docs/phase-3-sprint-plan.md --verbose  # one, with the diff
```

Frontmatter picks the target: `page_id` updates, `parent_id` creates. After a
write, push re-fetches and diffs the node-type census against what was sent —
a mismatch means Confluence rewrote something and needs a look.

## Confluence is the source of truth

These `.md` files are working copies. The page is what counts, and an
already-published page is often amended by a **structural ADF patch** that never
touches the local Markdown. The copy then falls behind silently: it still
renders, still pushes, and the push would delete whatever the page gained.

`check.py` compares each working copy against its live page — flattened text
plus the node census, **status-lozenge text included**, so a stale revision
badge is caught. It deliberately does not compare raw ADF: Confluence assigns
its own `localId`s on write, so byte equality is always false.

`push.py` stamps **`page_version`** into the frontmatter after every publish and
checks it before the next one. If the page moved underneath the working copy the
publish is **refused** (exit 1, nothing written) with a pointer at `check.py`;
`--force` overrides once you know what you would overwrite.

⚠️ A working copy carrying a real `page_id` is live ammunition. Copying it
elsewhere does not make it safe — `--publish` still writes to the real page.

⚠️ **Quote any frontmatter value containing `: `** — most of our titles do
(`title: "Phase 3 — Delivery Plan: epic breakdown…"`). `parse()` splits on the
first colon and is happy either way, but editors and CI that run a real YAML
parser reject the unquoted form as a nested mapping. `parse()` strips one layer
of matching quotes, so quoting never reaches the page title or version message.

`adf.py` is also importable for structural patching of pages that are *not* yet
mirrored locally (`adf.census`, `adf.inline`, `adf.render`).

## Format

See `sample.md` — it exercises every supported node. Beyond CommonMark:

- `::doc-control` … `::` — the label/value document-control header
- `:::panel info|note|warning|success|error` … `:::` — panels, containing blocks
- `::decisions` … `::` — decision lists · `::tasks` … `::` with `[ ]`/`[x]`
- `{status:green|APPROVED}` — status lozenge · `@2026-07-21` — native date node

## Not covered

`layoutSection`, `expand`, media/attachments, inline comments. Pages using
those (the PRD has one `layoutSection`) should keep using structural ADF
patching for now — see the `reference-atlassian-rest` memory.

## Migration stance

New docs are authored here from the start. Existing Confluence pages stay
canonical and get patched structurally; convert one to a local `.md` only when
it needs a rewrite big enough to pay for the conversion, and verify by diffing
the rendered census against the live page first.
