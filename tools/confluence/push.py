"""Render a local .md source and push it to Confluence, then verify.

    python3 tools/confluence/push.py docs/phase-3-sprint-plan.md            # dry run
    python3 tools/confluence/push.py docs/phase-3-sprint-plan.md --publish
    python3 tools/confluence/push.py docs/phase-3-sprint-plan.md --publish --force

Frontmatter drives the target:
    page_id           existing page to update (omit to create)
    page_version      the page version this working copy was last in step with
    parent_id         parent when creating
    title             page title
    version_message   Confluence version comment

`page_version` is written back after every publish and checked before the next
one. Confluence is the source of truth and these files are working copies, so a
page that moved underneath us — most often a structural ADF patch, which is how
an already-published page normally gets amended — means this copy is behind and
pushing it would silently delete whatever the page gained. The publish refuses;
`--force` overrides once you know what you are overwriting. Use check.py to see
the difference first.

Credentials come from ~/.config/atlassian/{token,env}. The page body never
passes through the model's context in either direction.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adf  # noqa: E402

SPACE_ID = "196612"  # SD — Software Development


def _conf():
    home = os.path.expanduser("~")
    token = open(os.path.join(home, ".config/atlassian/token")).read().strip()
    env = {}
    for line in open(os.path.join(home, ".config/atlassian/env")):
        if "=" in line:
            k, _, v = line.strip().partition("=")
            env[k] = v
    return env["ATLASSIAN_EMAIL"], token, env["ATLASSIAN_SITE"]


def _curl(method, path, payload=None):
    email, token, site = _conf()
    cmd = ["curl", "-sS", "-u", "%s:%s" % (email, token), "-X", method,
           "-H", "Content-Type: application/json", site + path]
    # The payload goes via a temp file, not -d: a grown phase doc renders past
    # ARG_MAX and execve fails with "Argument list too long" before any request.
    tmp = None
    try:
        if payload is not None:
            fd, tmp = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            cmd += ["-d", "@" + tmp]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    finally:
        if tmp:
            os.unlink(tmp)
    return json.loads(out) if out.strip() else {}


def stamp(src_path, key, value):
    """Set a frontmatter key in place, inserting it if absent.

    Only the frontmatter block is touched: the scan stops at its closing ``---``,
    so a body line that happens to start with the key is left alone.
    """
    src = open(src_path).read()
    assert src.startswith("---\n"), "no frontmatter in %s" % src_path
    head, sep, body = src[4:].partition("\n---\n")
    assert sep, "unterminated frontmatter in %s" % src_path
    out, seen = [], False
    for line in head.split("\n"):
        if line.split(":", 1)[0].strip() == key:
            out.append("%s: %s" % (key, value))
            seen = True
        else:
            out.append(line)
    if not seen:
        out.insert(1 if out else 0, "%s: %s" % (key, value))
    open(src_path, "w").write("---\n" + "\n".join(out) + "\n---\n" + body)


def fetch(page_id):
    return _curl("GET", "/wiki/api/v2/pages/%s?body-format=atlas_doc_format" % page_id)


def main():
    src_path = sys.argv[1]
    publish = "--publish" in sys.argv
    meta, doc = adf.render(open(src_path).read())
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))

    print("source   : %s (%d chars)" % (src_path, len(open(src_path).read())))
    print("rendered : %d chars ADF" % len(body))
    print("census   : %s" % adf.census(doc))

    page_id = meta.get("page_id")
    if not publish:
        print("\nDRY RUN — pass --publish to write. Target: %s"
              % ("page %s" % page_id if page_id else "new page"))
        return

    payload_body = {"representation": "atlas_doc_format", "value": body}
    if page_id:
        live = fetch(page_id)
        seen = meta.get("page_version")
        now = live["version"]["number"]
        if seen is None:
            print("note     : no page_version recorded — stamping v%d, not checked"
                  % now)
        elif int(seen) != now:
            print("\nREFUSED  : page %s is at v%d; this working copy was last in "
                  "step with v%s." % (page_id, now, seen))
            print("           The page moved without this file — most likely a "
                   "structural patch. Publishing now would delete whatever it "
                   "gained.")
            print("           Look first:  python3 tools/confluence/check.py %s"
                  % src_path)
            print("           Override:    add --force once you know what you are "
                  "overwriting.")
            if "--force" not in sys.argv:
                sys.exit(1)
            print("           --force given; overwriting v%d." % now)
        nxt = now + 1
        res = _curl("PUT", "/wiki/api/v2/pages/%s" % page_id, {
            "id": page_id, "status": "current",
            "title": meta.get("title", live["title"]),
            "body": payload_body,
            "version": {"number": nxt, "message": meta.get("version_message", "")}})
    else:
        res = _curl("POST", "/wiki/api/v2/pages", {
            "spaceId": SPACE_ID, "status": "current",
            "title": meta["title"],
            "parentId": meta.get("parent_id"),
            "body": payload_body})

    pid = res.get("id")
    if not pid:
        print("FAILED: %s" % json.dumps(res)[:400])
        sys.exit(1)

    if not page_id:
        # Without this the next --publish creates a SECOND page instead of updating.
        stamp(src_path, "page_id", pid)
        print("page_id  : %s written back to %s" % (pid, src_path))

    back = fetch(pid)
    live_doc = json.loads(back["body"]["atlas_doc_format"]["value"])
    sent, got = adf.census(doc), adf.census(live_doc)
    drift = {k: (sent.get(k), got.get(k))
             for k in set(sent) | set(got) if sent.get(k) != got.get(k)}

    live_version = back["version"]["number"]
    # Recorded last, so a failed verify still leaves the file pointing at what is
    # actually on the page rather than at a version that never existed.
    stamp(src_path, "page_version", live_version)

    print("\npage     : %s v%s" % (pid, live_version))
    print("verify   : %s" % ("census matches" if not drift else "DRIFT %s" % drift))
    print("stamped  : page_version %s -> %s" % (live_version, src_path))


if __name__ == "__main__":
    main()
