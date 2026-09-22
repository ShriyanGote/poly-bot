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


def _looks_like_tape(blob):
    """Does this decode like our CSV, or did we resync onto noise?

    1f 8b 08 occurs by chance inside compressed data, so a candidate boundary
    has to prove itself. Real tape text is printable ASCII with commas and an
    ISO timestamp; random deflate output decoded as gzip is neither.
    """
    head = blob[:2048]
    if not head or b"," not in head:
        return False
    printable = sum(1 for c in head if 9 <= c <= 126)
    return printable / len(head) > 0.95 and b"-" in head and b":" in head


def _resync(raw, start):
    """Next byte offset that really begins a gzip member, or None."""
    pos = start
    while True:
        m = _MAGIC.search(raw, pos)
        if not m:
            return None
        cand = m.start()
        d = zlib.decompressobj(31)
        try:
            out = d.decompress(raw[cand:cand + 262144])
        except Exception:
            pos = cand + 3
            continue
        if _looks_like_tape(out):
            return cand
        pos = cand + 3


def _members(path):
    """Decompressed bytes of every readable gzip member.

    Two things used to throw data away. A single decompress() of a whole
    member raises on the first corrupt byte and discards everything already
    decoded, so one bad member cost all of it - and resync then scanned for
    the next magic bytes without checking they began a real member, so it
    landed on noise and skipped past good data. Together those read back
    661,850 of the 3,717,992 rows recorded in a day.

    So decode incrementally, keep whatever came before the damage, and make a
    candidate boundary prove it decodes into tape text before trusting it.
    """
    raw = path.read_bytes()
    pos = 0
    n = len(raw)
    while pos is not None and pos < n:
        d = zlib.decompressobj(31)
        chunks = []
        i = pos
        broke = False
        try:
            while i < n:
                piece = raw[i:i + (1 << 20)]
                got = d.decompress(piece)
                if got:
                    chunks.append(got)
                i += len(piece)
                if d.eof:
                    break
        except Exception:
            broke = True
        if chunks:
            yield b"".join(chunks)
        if not broke and d.eof:
            # unused_data holds only what was left over from the chunks
            # actually fed, so the next member starts relative to i - not to
            # the end of the file. Getting this wrong skipped whole members.
            pos = i - len(d.unused_data)
            if pos >= n:
                return
            continue
        pos = _resync(raw, pos + 3)


def _known_headers():
    """{column count: header} for every layout a tape may have on disk.

    A column added mid-day means rows written afterwards are longer than the
    header row at the top of the file - and a length mismatch used to drop
    them in silence. Adding "score" cost ~70% of one day's tennis rows before
    this was noticed, so match a row to the layout of its own width instead.
    """
    from .storage import BOOK_HEADER
    out = {}
    for h in (config.BOOK_HEADER_FALLBACK, BOOK_HEADER):
        out[len(h)] = list(h)
    return out


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
                alt = _known_headers().get(len(parts))
                if alt is None:
                    continue              # genuinely torn at a member edge
                yield dict(zip(alt, parts))
                continue
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
