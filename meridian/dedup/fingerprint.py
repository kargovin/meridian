"""The near-duplicate fingerprint (FR-I5, 2.1.2 §3.2).

Pure: text in, one integer out. No database, no configuration.

SimHash is a locality-sensitive hash — unlike SHA-256, similar inputs give *similar* outputs,
which is the whole point: a wire story re-hosted with an added credit line differs in a few
bits rather than entirely. Two fingerprints are compared by XOR and popcount, and the distance
that counts as "the same article" is a runtime knob the stage reads, not a constant here.

⚠️ The two parameters below are not the same kind of thing as that knob. ``h``, the distance,
only moves where the line is drawn and leaves every stored fingerprint valid. ``SHINGLE_WORDS``
and ``BITS`` change the fingerprints themselves, so altering either makes every value already
in the column incomparable with every new one — silently, because nothing errors and distances
merely stop meaning anything. They are constants, and changing one is a migration that
recomputes the column.
"""

import hashlib
from collections import Counter

from meridian.ingest.normalize import normalized_tokens

#: Words per shingle. Single words would match any two articles on one subject — they share
#: vocabulary — so what distinguishes a re-hosting from an independent report is phrasing, and
#: phrasing is word order. Longer shingles are destroyed wholesale by one inserted word and
#: leave a short article with almost none.
SHINGLE_WORDS = 3

#: Fingerprint width. 64 puts unrelated articles around 32 bits apart with a standard deviation
#: of 4 (measured over the corpus), which is the margin that makes a threshold of 3 safe.
BITS = 64


def fingerprint(body: str) -> int | None:
    """The body's 64-bit SimHash, or ``None`` if it is too short to have one.

    ``None`` rather than a fingerprint of zero. A body with fewer than ``SHINGLE_WORDS`` words
    produces no shingles, every accumulator stays at zero, and the natural result is 0 — which
    is distance 0 from every other such body, so two unrelated fragments would be declared
    identical. Acquire's minimum body length is a fragment guard measured in characters and
    does not stop a single long token getting here.

    Near matching simply skips a record with no fingerprint. Exact matching is unaffected:
    ``content_hash`` is defined for any body at all.
    """
    tokens = normalized_tokens(body)
    shingles = Counter(
        " ".join(tokens[i : i + SHINGLE_WORDS]) for i in range(len(tokens) - SHINGLE_WORDS + 1)
    )
    if not shingles:
        return None

    # Each bit is a hyperplane through shingle space; the accumulator records which side the
    # document falls on, weighted by how often the shingle occurs. Documents built from mostly
    # the same shingles fall the same side of mostly the same hyperplanes.
    accumulator = [0] * BITS
    for shingle, weight in shingles.items():
        # ⚠️ blake2b, never the built-in hash(): Python salts string hashing per process, so a
        # fingerprint computed after a restart would not compare with one already stored. The
        # bug is invisible in-process, which is why the test for it runs two subprocesses under
        # different PYTHONHASHSEED values. Cryptographic strength is irrelevant; stability and
        # uniform bits are the requirement, and digest_size gives exactly BITS with no slicing.
        digest = int.from_bytes(
            hashlib.blake2b(shingle.encode(), digest_size=BITS // 8).digest(), "big"
        )
        for bit in range(BITS):
            accumulator[bit] += weight if (digest >> bit) & 1 else -weight

    # A tie resolves to 0. Arbitrary, but it has to be fixed: the alternative is one body with
    # two fingerprints depending on nothing.
    return sum(1 << bit for bit in range(BITS) if accumulator[bit] > 0)


def distance(left: int, right: int) -> int:
    """Hamming distance between two fingerprints — how many of the ``BITS`` questions they
    answer differently.

    Here for tests and for reading; the stage does this in SQL (``bit_count``) so the search
    is one query over the stored column rather than a set rebuilt in each process's memory.
    """
    return (left ^ right).bit_count()
