"""Sequential reader for object records (ZDOs) in Valheim 1.0 .chunk files.

chunk  = [worldVersion u16][count i32][record]*[tail]
record = [flags u16]
         [position: 3 x f32, or 4 bytes when flags bit 13 is set (per-zone housekeeping records)]
         [prefab hash u32]
         [rotation if bit 12: u16; high bit set = yaw only (2 bytes), else 4 bytes]
         [connection if bit 0: type u8 + hash i32]
         [field groups, each present when its bit is set, in this order:
            bit1 floats(4) bit2 vec3(12) bit3 quat(16) bit4 ints(4) bit5 longs(8)
            bit6 strings (7-bit length + utf8) bit7 byte arrays (i32 length + bytes)
          each group = [count: u8, high bit -> 15-bit] + count x ([key hash u32][value])]
bit 8 = persistent (always set in saves). Worked out against real saves: 99.98% of records
decode exactly and the leftovers are zone-housekeeping records at chunk ends.
"""
import math, struct

_GROUPS = ((1, 'f', 4), (2, 'v', 12), (3, 'q', 16), (4, 'i', 4), (5, 'l', 8), (6, 's', 0), (7, 'b', 0))


def _num(b, p):
    n = b[p]; p += 1
    if n & 128:
        n = ((n & 127) << 8) | b[p]; p += 1
    return n, p


def _len7(b, p):
    ln = sh = 0
    while True:
        c = b[p]; p += 1
        ln |= (c & 127) << sh; sh += 7
        if c < 128:
            return ln, p


def decode(b, p):
    fl, = struct.unpack_from('<H', b, p); p += 2
    if fl & (1 << 13):
        pos = None; p += 4
    else:
        pos = struct.unpack_from('<fff', b, p); p += 12
    prefab, = struct.unpack_from('<I', b, p); p += 4
    if fl & (1 << 12):
        v, = struct.unpack_from('<H', b, p)
        p += 2 if v & 0x8000 else 4
    if fl & 1:
        p += 5
    fields = {}
    for bit, kind, size in _GROUPS:
        if not fl & (1 << bit):
            continue
        cnt, p = _num(b, p)
        if cnt == 0 or cnt > 500:
            raise ValueError('bad count')
        grp = fields.setdefault(kind, {})
        for _ in range(cnt):
            key, = struct.unpack_from('<I', b, p); p += 4
            if kind == 's':
                ln, p = _len7(b, p); grp[key] = b[p:p + ln].decode('utf-8', 'replace'); p += ln
            elif kind == 'b':
                ln, = struct.unpack_from('<i', b, p); p += 4
                if ln < 0 or ln > 1 << 24:
                    raise ValueError('bad length')
                grp[key] = (p, ln); p += ln
            elif kind == 'f':
                grp[key] = struct.unpack_from('<f', b, p)[0]; p += 4
            elif kind == 'i':
                grp[key] = struct.unpack_from('<i', b, p)[0]; p += 4
            elif kind == 'l':
                grp[key] = struct.unpack_from('<q', b, p)[0]; p += 8
            else:
                p += size
    if p > len(b):
        raise EOFError
    return p, fl, pos, prefab, fields


def _plausible(b, p):
    if p + 10 > len(b):
        return False
    fl, = struct.unpack_from('<H', b, p)
    if not fl & 0x100 or fl & 0xC000:
        return False
    if fl & (1 << 13):
        return True
    if p + 18 > len(b):
        return False
    return all(math.isfinite(v) and abs(v) < 30000 for v in struct.unpack_from('<fff', b, p + 2))


def records(b):
    """Yield (flags, pos, prefab, fields) for every record in a chunk file's bytes."""
    count, = struct.unpack_from('<i', b, 2)
    p, n = 6, 0
    while n < count and p < len(b) - 10:
        try:
            q, fl, pos, prefab, fields = decode(b, p)
            if n + 1 < count and not _plausible(b, q):
                raise ValueError('misaligned')
        except Exception:
            q = p + 1                                      # resync on the next plausible record
            while q < len(b) - 10 and not _plausible(b, q):
                q += 1
            p = q
            continue
        yield fl, pos, prefab, fields
        n += 1; p = q
