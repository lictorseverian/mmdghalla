"""Base stock: the full item list per base, and low-stock tracking from data/stock.txt.

Each tracked base gets
  name     the name from stock.txt (overrides the pin-derived name)
  watch    {prefab: threshold}
  low      [[prefab, display name, count, threshold]] for items below threshold, worst first
Every base gets
  stock    [[prefab, display name, count, group]] for everything it holds, biggest first

Whether an item is "low" across runs (with hysteresis) is settled in feed.py, which has the
previous snapshot; here `low` is the plain count < threshold view of this save.
"""
import math, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, 'data', 'stock.txt')
RESTOCK = 1.2                   # a low item counts as restocked again at 120% of its threshold

_BASE = re.compile(r'base:\s*(.+?)\s*@\s*(-?\d+(?:\.\d+)?)\s*,?\s*(-?\d+(?:\.\d+)?)$', re.I)
_ITEM = re.compile(r'(\S+)\s+(\d+)$')


def load(path=PATH):
    """[{name, x, z, watch: {prefab: threshold}}]. A missing or broken file just tracks nothing."""
    out, cur = [], None
    if not os.path.exists(path):
        return out
    for ln, line in enumerate(open(path, encoding='utf-8', errors='replace'), 1):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        m = _BASE.match(line)
        if m:
            cur = dict(name=m[1], x=float(m[2]), z=float(m[3]), watch={})
            out.append(cur)
            continue
        m = _ITEM.match(line)
        if m and cur is not None and int(m[2]) > 0:
            cur['watch'][m[1]] = int(m[2])
            continue
        print(f'stock.txt:{ln}: ignored {line!r}', file=sys.stderr)
    return out


def annotate(bases, counts, item_name, group_of, cfg=None):
    """counts: one Counter per base (prefab -> count), parallel to bases."""
    cfg = load() if cfg is None else cfg
    for b, c in zip(bases, counts):
        b['stock'] = [[p, item_name(p), n, group_of(p)] for p, n in sorted(c.items(), key=lambda t: (-t[1], t[0])) if n > 0]
    for t in cfg:
        inside = [(b, c) for b, c in zip(bases, counts) if math.hypot(b['x'] - t['x'], b['z'] - t['z']) <= b['r']]
        if not inside:
            print(f"stock: no base at {t['x']:.0f}, {t['z']:.0f} for {t['name']}", file=sys.stderr)
            continue
        b, c = min(inside, key=lambda bc: math.hypot(bc[0]['x'] - t['x'], bc[0]['z'] - t['z']))
        b['name'] = t['name']
        b['watch'] = dict(t['watch'])
        low = [[p, item_name(p), c.get(p, 0), thr] for p, thr in t['watch'].items() if c.get(p, 0) < thr]
        b['low'] = sorted(low, key=lambda r: (r[2] / r[3], r[1]))


def tracked(bases):
    """Rows for the snapshot: [base name, prefab, count, threshold] for every watched item."""
    rows = []
    for b in bases:
        counts = {r[0]: r[2] for r in b.get('stock', [])}
        for p, thr in (b.get('watch') or {}).items():
            rows.append([b['name'], p, counts.get(p, 0), thr])
    return rows


def digest_key(bases):
    """Coarse view of watched stock for the rebuild digest: below threshold, or which half-threshold step."""
    return sorted((name, p, -1 if n < thr else min(2 * n // thr, 8)) for name, p, n, thr in tracked(bases))
