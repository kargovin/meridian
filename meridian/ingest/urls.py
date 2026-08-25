"""Reducing an article URL to one stable form (RFC §5.1, FR-I4).

``canonical_record.url_canonical`` is UNIQUE, and that constraint is what stops one article
arriving twice. It only works if the value is already canonical **when the row is inserted** —
normalising afterwards is too late, because the second row exists by then. So this runs during
discovery, before the write, and not in a later stage.

⚠️ The two directions of error are not symmetric, and the list below is built around that.
Stripping a parameter that carries meaning **merges two different articles into one record** and
the loss is silent. Failing to strip a tracking parameter merely leaves a duplicate, which dedup
is built to collapse. Under-stripping is therefore the safe direction, and every entry here is a
parameter we can point at on a real source rather than one that looked disposable.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Families of tracking parameters, matched by prefix.
#:
#: ``utm_`` is the cross-industry standard. ``at_`` and ``ns_`` are the families BBC decorates
#: its feed links with (``at_medium=RSS``, ``at_campaign``, ``ns_mchannel``); no news source we
#: poll uses either prefix for anything an article needs.
_TRACKING_PREFIXES = ("utm_", "at_", "ns_")

#: Individual tracking parameters, each observed on a real publisher or ad network.
_TRACKING_PARAMS = frozenset(
    {
        # Al Jazeera decorates every feed link with this one — the case that made the
        # UNIQUE(url_canonical) constraint stop collapsing anything.
        "traffic_source",
        # Ad-network click identifiers, appended to outbound links.
        "fbclid",
        "gclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "dclid",
        "yclid",
        "igshid",
        # Campaign identifiers used by mainstream newsrooms.
        "cmp",
        "cmpid",
        "ito",
        "ocid",
        "smid",
        "icid",
        "ncid",
        # Mailing-list identifiers.
        "mc_cid",
        "mc_eid",
        # Analytics.
        "_ga",
        "spm",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """One stable form of an article URL.

    Scheme and host are lower-cased (both are case-insensitive per RFC 3986), a default port is
    dropped, tracking parameters are removed, the parameters that remain are sorted, and the
    fragment is discarded — a fragment is never sent to the server, so two URLs differing only
    there are the same request.

    ⚠️ The path is left exactly as it is, including a trailing slash. ``/a`` and ``/a/`` are
    genuinely different resources to some servers, so folding them would be the merging kind of
    mistake this module is arranged to avoid.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    ]
    # Sorted so that the same parameters in a different order are one URL — a feed and a share
    # link routinely disagree about order.
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, parts.path, query, ""))
