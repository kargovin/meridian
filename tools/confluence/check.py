"""Compare a local .md working copy against the live Confluence page.

    python3 tools/confluence/check.py                 # every docs/*.md with a page_id
    python3 tools/confluence/check.py docs/foo.md     # just this one
    python3 tools/confluence/check.py --verbose       # show the differing lines

Confluence is the source of truth; these files are working copies. The failure
this guards is a page edited live — or patched structurally, which is how every
already-published page gets amended — while the working copy stays behind. That
drift is silent: the file still renders, still pushes, and pushing it would
overwrite whatever the page has gained since.

Comparison is on flattened text and the node census, not raw ADF. Confluence
assigns its own `localId` to task items and status lozenges on write, and a
re-render produces different ones, so byte equality is always false and would
make this tool useless. Status lozenge TEXT is included in the comparison —
a stale revision badge is exactly the drift worth catching.

Exit 0 = every checked doc matches. Exit 1 = at least one has drifted.
"""

import difflib
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adf  # noqa: E402
import push  # noqa: E402


def flatten(node, out):
    """Text a reader would see, one line per block, with lozenges included."""
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        out.append(node.get("text", ""))
        return
    if kind == "status":
        out.append("{%s}" % attrs.get("text", ""))
        return
    if kind == "date":
        out.append("<date %s>" % attrs.get("timestamp", ""))
        return
    if kind == "hardBreak":
        out.append(" ")
        return
    for child in node.get("content") or []:
        flatten(child, out)
    if kind in ("paragraph", "heading", "listItem", "taskItem", "decisionItem",
                "tableRow", "codeBlock", "rule"):
        out.append("\n")


def lines(doc):
    out = []
    flatten(doc, out)
    text = "".join(out)
    return [" ".join(ln.split()) for ln in text.split("\n") if ln.strip()]


def check(path, verbose=False):
    meta, local_doc = adf.render(open(path).read())
    page_id = meta.get("page_id")
    if not page_id:
        print("· %-46s no page_id — never published, nothing to compare" % path)
        return True

    page = push.fetch(page_id)
    live_doc = json.loads(page["body"]["atlas_doc_format"]["value"])

    a, b = lines(local_doc), lines(live_doc)
    census_local, census_live = adf.census(local_doc), adf.census(live_doc)
    same_text = a == b
    same_census = census_local == census_live

    label = "%s -> %s v%s" % (path, page_id, page["version"]["number"])
    if same_text and same_census:
        print("✓ IN SYNC   %s  (%d blocks)" % (label, len(a)))
        return True

    print("✗ DRIFTED   %s" % label)
    if not same_census:
        only_local = {k: v for k, v in census_local.items() if census_live.get(k) != v}
        only_live = {k: v for k, v in census_live.items() if census_local.get(k) != v}
        print("    census  local=%s  live=%s" % (only_local, only_live))
    if not same_text:
        diff = list(difflib.unified_diff(b, a, "live", "local", lineterm="", n=0))
        changed = [d for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---")]
        print("    text    %d block(s) differ" % len(changed))
        show = changed if verbose else changed[:6]
        for d in show:
            print("      %s %s" % (d[0], d[1:][:150]))
        if len(changed) > len(show):
            print("      … %d more (pass --verbose)" % (len(changed) - len(show)))
    return False


def main():
    verbose = "--verbose" in sys.argv
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not paths:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        paths = [os.path.relpath(x)
                 for x in sorted(glob.glob(os.path.join(root, "docs", "*.md")))]
    ok = all([check(p, verbose) for p in paths])
    if not ok:
        print("\nDrift found. Confluence is the source of truth: bring the working "
              "copy up to the page, never the other way round without checking "
              "what the page has gained.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
