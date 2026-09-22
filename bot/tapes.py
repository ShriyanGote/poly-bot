"""Shared tape loading.

Tapes are gzip files appended to across restarts, which leaves concatenated
gzip members. Python's gzip reader stops at the first damaged member boundary,
which silently hid most of the weekend's data (one tennis tape read 18,780 of
103,658 rows). We therefore decompress member-by-member and skip only the
members that are actually broken.
"""

import csv
import gzip
import io
import re
import zlib

from . import config

_MAGIC = re.compile(rb"\x1f\x8b\x08")


def tape_files(prefix="tape", sport=None):
    """Matching tape files. `sport` filters to one sport."""
    pat = f"{prefix}-{sport}-*.csv.gz" if sport else f"{prefix}-*.csv.gz"
    return sorted(config.DATA.glob(pat))


def _members(path):
    """Decompressed bytes of every readable gzip member.

    Members are walked with zlib's own `unused_data` rather than by scanning
    for the 1f 8b 08 magic: that byte sequence occurs inside compressed data
    by chance, so scanning invents false boundaries and corrupts good members.
    When a member really is truncated we resync on the next magic AFTER it.
    """
    raw = path.read_bytes()
    pos = 0
    n = len(raw)
    while pos < n:
        d = zlib.decompressobj(31)
        try:
            out = d.decompress(raw[pos:])
            if out:
                yield out
            rest = d.unused_data
            if not rest:
                return
            pos = n - len(rest)
        except Exception:
            # Truncated member: emit whatever decoded, then resync forward.
            try:
                partial = d.flush()
                if partial:
                    yield partial
            except Exception:
                pass
            m = _MAGIC.search(raw, pos + 3)
            if not m:
                return
            pos = m.start()


def _rows(path):
    """Rows from a tape, recovering past damaged members."""
    try:
        with gzip.open(path, "rt") as fh:
            for row in csv.DictReader(fh):
                yield row
        return
    except (EOFError, OSError, gzip.BadGzipFile, zlib.error, csv.Error):
        pass                  # fall through to member recovery

    header = None
    for blob in _members(path):
        text = blob.decode("utf-8", errors="replace")
        rdr = csv.reader(io.StringIO(text))
        for parts in rdr:
            if not parts:
                continue
            if parts[0] == "ts":          # a member can start with its own header
                header = parts
                continue
            if header is None:
                header = list(config.BOOK_HEADER_FALLBACK)
            if len(parts) != len(header):
                continue                  # torn line at a member edge
            yield dict(zip(header, parts))


def read(prefix="tape", sport=None):
    for f in tape_files(prefix, sport):
        for row in _rows(f):
            if sport and row.get("sport") and row["sport"] != sport:
                continue
            yield row


def sports_present(prefix="tape"):
    out = set()
    for f in tape_files(prefix):
        parts = f.stem.replace(".csv", "").split("-")
        if len(parts) >= 5:
            out.add(parts[1])
    return sorted(out)
