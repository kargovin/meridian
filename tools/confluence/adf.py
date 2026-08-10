"""Markdown-with-directives -> Atlas Doc Format.

Covers exactly the node set this project uses (see CLAUDE.md conventions):
headings, paragraphs, bullet/ordered lists, tables, panels, status lozenges,
decision lists, task lists, code blocks, date nodes, links and inline marks.

Authoring format
----------------
    ---                         YAML-ish frontmatter: page_id, title, ...
    key: value
    ---

    ::doc-control               label/value table, first column bold
    Author: Jordan Osei
    Status: {status:green|APPROVED}
    ::

    ## 2. Heading

    Prose with **strong**, *em*, `code`, [link](https://x), {status:red|OPEN}
    and @2026-07-21 date nodes.

    :::panel warning            panel types: info note warning success error
    Body block(s).
    :::

    ::decisions                 -> decisionList
    J1 - Triton-class serving. DECIDED
    ::

    ::tasks                     -> taskList; [x] marks done
    [ ] Marcus to date the KR3 coverage floor
    ::

    | Rev | Date |              pipe tables; --- separator marks a header row
    | --- | ---- |
    | 19  | @2026-07-21 |

    ```python                   -> codeBlock
    x = 1
    ```

Python 3.8 compatible. No third-party dependencies.
"""

import datetime
import json
import re

PANEL_TYPES = {"info", "note", "warning", "success", "error"}


# --------------------------------------------------------------- inline

_INLINE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<link>\[[^\]]+\]\([^)]+\))"
    r"|(?P<status>\{status:[a-z]+\|[^}]+\})"
    r"|(?P<date>@\d{4}-\d{2}-\d{2})"
    r"|(?P<strong>\*\*[^*]+\*\*)"
    r"|(?P<em>(?<!\*)\*(?!\*)[^*]+\*(?!\*))"
)


def _date_node(iso):
    y, m, d = (int(p) for p in iso.split("-"))
    ts = datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp()
    return {"type": "date", "attrs": {"timestamp": str(int(ts) * 1000)}}


def _text(s, marks=None):
    n = {"type": "text", "text": s}
    if marks:
        n["marks"] = marks
    return n


def inline(s, marks=None):
    """Parse inline markup into a list of ADF inline nodes."""
    marks = marks or []
    out = []
    pos = 0
    for m in _INLINE.finditer(s):
        if m.start() > pos:
            out.append(_text(s[pos:m.start()], marks))
        kind = m.lastgroup
        raw = m.group()
        if kind == "code":
            # ADF: `code` is exclusive with strong/em/strike — only `link` may
            # ride along. Combining them is rejected with a bare INVALID_INPUT.
            keep = [m for m in marks if m["type"] == "link"]
            out.append(_text(raw[1:-1], keep + [{"type": "code"}]))
        elif kind == "strong":
            out.extend(inline(raw[2:-2], marks + [{"type": "strong"}]))
        elif kind == "em":
            out.extend(inline(raw[1:-1], marks + [{"type": "em"}]))
        elif kind == "date":
            out.append(_date_node(raw[1:]))
        elif kind == "status":
            colour, label = raw[len("{status:"):-1].split("|", 1)
            out.append({"type": "status", "attrs": {
                "color": colour, "style": "bold", "text": label}})
        elif kind == "link":
            label, href = re.match(r"\[([^\]]+)\]\(([^)]+)\)", raw).groups()
            out.append(_text(label, marks + [{"type": "link", "attrs": {"href": href}}]))
        pos = m.end()
    if pos < len(s):
        out.append(_text(s[pos:], marks))
    return out or [_text("", marks)] if s else out


def para(s):
    return {"type": "paragraph", "content": inline(s)}


# --------------------------------------------------------------- blocks

class _Reader:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def next(self):
        line = self.peek()
        self.i += 1
        return line

    def done(self):
        return self.i >= len(self.lines)

    def until(self, terminator):
        """Consume lines up to and including a line equal to `terminator`."""
        body = []
        while not self.done():
            line = self.next()
            if line.strip() == terminator:
                return body
            body.append(line)
        raise ValueError("unterminated block, expected %r" % terminator)


def _cell(text, header=False):
    return {"type": "tableHeader" if header else "tableCell",
            "attrs": {"colspan": 1, "rowspan": 1},
            "content": [para(text)] if text.strip() else [{"type": "paragraph", "content": []}]}


def _split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table(r):
    rows = []
    while r.peek() is not None and r.peek().lstrip().startswith("|"):
        rows.append(_split_row(r.next()))
    header = len(rows) > 1 and all(set(c) <= set("-: ") and c for c in rows[1])
    if header:
        body = [rows[0]] + rows[2:]
    else:
        body = rows
    content = []
    for idx, cells in enumerate(body):
        is_head = header and idx == 0
        content.append({"type": "tableRow",
                        "content": [_cell(c, is_head) for c in cells]})
    return {"type": "table", "attrs": {"layout": "default"}, "content": content}


def _list(r, ordered):
    bullet = re.compile(r"^(\s*)(?:[-*]|\d+\.)\s+(.*)$")
    items = []
    base = None
    while r.peek() is not None:
        m = bullet.match(r.peek())
        if not m:
            break
        indent = len(m.group(1))
        if base is None:
            base = indent
        if indent < base:
            break
        if indent > base:
            nested = _list(r, ordered=False)
            if items:
                items[-1]["content"].append(nested)
            continue
        r.next()
        items.append({"type": "listItem", "content": [para(m.group(2))]})
    return {"type": "orderedList" if ordered else "bulletList", "content": items}


def _blocks(lines):
    """Parse a list of source lines into a list of ADF block nodes."""
    r = _Reader(lines)
    out = []
    while not r.done():
        line = r.peek()
        stripped = line.strip()

        if not stripped:
            r.next()
            continue

        if stripped.startswith(":::panel"):
            ptype = stripped.split()[1] if len(stripped.split()) > 1 else "info"
            if ptype not in PANEL_TYPES:
                raise ValueError("unknown panel type %r" % ptype)
            r.next()
            out.append({"type": "panel", "attrs": {"panelType": ptype},
                        "content": _blocks(r.until(":::"))})
            continue

        if stripped == "::doc-control":
            r.next()
            rows = []
            for entry in r.until("::"):
                if not entry.strip():
                    continue
                label, _, value = entry.partition(":")
                rows.append({"type": "tableRow", "content": [
                    {"type": "tableHeader", "attrs": {"colspan": 1, "rowspan": 1},
                     "content": [{"type": "paragraph", "content": inline(label.strip(), [{"type": "strong"}])}]},
                    _cell(value.strip())]})
            out.append({"type": "table", "attrs": {"layout": "default"}, "content": rows})
            continue

        if stripped == "::decisions":
            r.next()
            items = [{"type": "decisionItem", "attrs": {"state": "DECIDED"},
                      "content": inline(d.strip())}
                     for d in r.until("::") if d.strip()]
            out.append({"type": "decisionList", "attrs": {}, "content": items})
            continue

        if stripped == "::tasks":
            r.next()
            items = []
            for t in r.until("::"):
                t = t.strip()
                if not t:
                    continue
                state = "DONE" if t[:3].lower() == "[x]" else "TODO"
                body = t[3:].strip() if t.startswith("[") else t
                items.append({"type": "taskItem", "attrs": {"state": state},
                              "content": inline(body)})
            out.append({"type": "taskList", "attrs": {}, "content": items})
            continue

        if stripped.startswith("```"):
            lang = stripped[3:].strip() or None
            r.next()
            body = r.until("```")
            node = {"type": "codeBlock", "content": [_text("\n".join(body))]}
            if lang:
                node["attrs"] = {"language": lang}
            out.append(node)
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            r.next()
            out.append({"type": "heading", "attrs": {"level": min(level, 6)},
                        "content": inline(stripped[level:].strip())})
            continue

        if stripped == "---":
            r.next()
            out.append({"type": "rule"})
            continue

        if stripped.startswith("|"):
            out.append(_table(r))
            continue

        if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line):
            out.append(_list(r, ordered=bool(re.match(r"^\s*\d+\.", line))))
            continue

        # paragraph: consume until blank line or a line that starts a new block
        buf = []
        while r.peek() is not None and r.peek().strip():
            nxt = r.peek().strip()
            if buf and (nxt.startswith(("#", "|", ":::", "::", "```"))
                        or re.match(r"^\s*(?:[-*]|\d+\.)\s+", r.peek())):
                break
            buf.append(r.next().strip())
        out.append(para(" ".join(buf)))
    return out


# --------------------------------------------------------------- document

def parse(src):
    """Split frontmatter from body. Returns (meta dict, body text)."""
    meta = {}
    if src.startswith("---\n"):
        end = src.index("\n---\n", 3)
        for line in src[4:end].splitlines():
            if line.strip():
                k, _, v = line.partition(":")
                v = v.strip()
                if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                meta[k.strip()] = v
        src = src[end + 5:]
    return meta, src


def render(src):
    """Markdown-with-directives -> (meta, ADF doc)."""
    meta, body = parse(src)
    return meta, {"type": "doc", "version": 1, "content": _blocks(body.splitlines())}


def census(adf):
    """Node-type counts, for verifying a render or a round trip."""
    counts = {}

    def walk(n):
        counts[n["type"]] = counts.get(n["type"], 0) + 1
        for c in n.get("content", []) or []:
            if isinstance(c, dict):
                walk(c)

    walk(adf)
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    import sys
    meta, doc = render(open(sys.argv[1]).read())
    print(json.dumps(doc, ensure_ascii=False, indent=1))
    print("\nmeta:", meta, file=sys.stderr)
    print("census:", census(doc), file=sys.stderr)
