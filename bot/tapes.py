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
import mmap
import re
import zlib

from . import config

_MAGIC = re.compile(rb"\x1f\x8b\x08")
# Sentinel between members so the row reader knows to reset its
# line buffer and header rather than splice two members together.
_MEMBER_START = object()


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
    fh = open(path, "rb")
    # mmap, not read_bytes: resync needs random access to the compressed file,
    # but not for it to be resident. A 200 MB tape should not cost 200 MB.
    raw = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    pos = 0
    n = len(raw)
    while pos is not None and pos < n:
        d = zlib.decompressobj(31)
        i = pos
        broke = False
        started = False
        try:
            while i < n:
                piece = raw[i:i + (1 << 20)]
                got = d.decompress(piece)
                if got:
                    if not started:
                        started = True
                        yield _MEMBER_START
                    # Stream it. Joining the whole member first cost 2.95 GB on
                    # a seven-million-row tape, which is fatal beside a live
                    # recorder on a small box.
                    yield got
                i += len(piece)
                if d.eof:
                    break
        except Exception:
            broke = True
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
    """Rows from a tape, recovering past damaged members.

    Parsed line by line out of a stream of chunks rather than by materialising
    a whole member, so memory stays flat however large the tape is. Reading one
    seven-million-row tennis tape used to peak at 2.95 GB, which is fatal on a
    small box sitting next to a live recorder.
    """
    try:
        with gzip.open(path, "rt") as fh:
            for row in csv.DictReader(fh):
                yield row
        return
    except (EOFError, OSError, gzip.BadGzipFile, zlib.error, csv.Error):
        pass                  # fall through to member recovery

    header = None
    buf = b""
    known = _known_headers()

    def emit(line):
        nonlocal header
        if not line:
            return None
        try:
            parts = next(csv.reader([line.decode("utf-8", errors="replace")]))
        except (csv.Error, StopIteration):
            return None
        if not parts:
            return None
        if parts[0] == "ts":          # a member can start with its own header
            header = parts
            return None
        cols = header if header is not None else list(config.BOOK_HEADER_FALLBACK)
        if len(parts) != len(cols):
            alt = known.get(len(parts))
            if alt is None:
                return None           # genuinely torn at a member edge
            return dict(zip(alt, parts))
        return dict(zip(cols, parts))

    for blob in _members(path):
        if blob is _MEMBER_START:
            # The previous member's last line may have had no trailing newline;
            # dropping it silently loses a row. Flush it, then reset - the old
            # header does not describe the new member's rows.
            row = emit(buf.rstrip(b"\r"))
            if row is not None:
                yield row
            buf, header = b"", None
            continue
        buf += blob
        if b"\n" not in buf:
            continue
        *lines, buf = buf.split(b"\n")
        for line in lines:
            row = emit(line.rstrip(b"\r"))
            if row is not None:
                yield row
    row = emit(buf.rstrip(b"\r"))     # trailing line when the member ended clean
    if row is not None:
        yield row


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
