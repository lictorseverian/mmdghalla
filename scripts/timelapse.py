"""Exploration timelapse: remember the day each explored pixel was first charted.

State (kept with the activity feed on the `data` branch):
  firstseen.bin.gz   uint16 per explored-grid pixel: index of the frame it first appeared in, 65535 = never
  timelapse.json     {"frames": [{"date": "2026-09-25", "day": 310, "km2": 18.1, "t": <epoch>}, ...]}

One frame per calendar day. Frame 0 holds everything charted before recording started.
"""
import base64, datetime, gzip, io, json, os
from zoneinfo import ZoneInfo
import numpy as np
from PIL import Image

import vkw

NONE = 65535


def update(state_dir, ex, day, ts, tz):
    fs_path, tl_path = os.path.join(state_dir, 'firstseen.bin.gz'), os.path.join(state_dir, 'timelapse.json')
    date = datetime.datetime.fromtimestamp(ts, ZoneInfo(tz)).date().isoformat()
    km2 = round(float(ex.sum() * vkw.PX_M ** 2 / 1e6), 2)
    try:
        fs = np.frombuffer(gzip.decompress(open(fs_path, 'rb').read()), np.uint16).reshape(vkw.GRID, vkw.GRID).copy()
        frames = json.load(open(tl_path))['frames']
    except Exception:
        fs, frames = None, []
    if fs is None or not frames:
        fs = np.full((vkw.GRID, vkw.GRID), NONE, np.uint16)
        frames = []
    if not frames or frames[-1]['date'] != date:
        frames.append(dict(date=date, day=day, km2=km2, t=ts))
    idx = len(frames) - 1
    fs[ex & (fs == NONE)] = idx
    frames[-1].update(day=day, km2=km2, t=ts)
    os.makedirs(state_dir, exist_ok=True)
    open(fs_path, 'wb').write(gzip.compress(fs.tobytes(), 9, mtime=0))
    json.dump(dict(frames=frames), open(tl_path, 'w'))
    return fs, frames


def for_page(fs, frames, gx0, gx1, gy0, gy1):
    """Crop to the map's box, north up, as a greyscale PNG data URI: value = frame index (255 = never)."""
    crop = fs[max(gy0, 0):gy1, max(gx0, 0):gx1]
    v = np.where(crop == NONE, 255, np.minimum(crop, 254)).astype(np.uint8)[::-1]
    buf = io.BytesIO()
    Image.fromarray(v, 'L').save(buf, 'PNG', optimize=True)
    return dict(png='data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode(),
                frames=frames)


# ---------------------------------------------------------------- objects through time
# history.json.gz: {"pins": {key: frame}, "portals": {...}, "graves": {...}, "pieces": {key: [frame, ...]}}
# frame = index of the day the object was first seen. Pieces can share a spot (stacked floors),
# so each piece key keeps one frame per copy.

def _keys(snap):
    return dict(
        pins=['%s|%d|%d|%d' % (p[0], round(p[1]), round(p[2]), p[3]) for p in snap['pins']],
        portals=['%s|%d|%d' % (p[0], round(p[1]), round(p[2])) for p in snap['portals']],
        graves=['%s|%d|%d|%d' % (g[0], round(g[1]), round(g[2]), g[3]) for g in snap['graves']],
        pieces=['%s|%s|%s|%s' % (p[0], p[1], p[2], p[3]) for p in snap['pieces']],
    )


def _frame_of(t, frames, tz):
    d = datetime.datetime.fromtimestamp(t, ZoneInfo(tz)).date().isoformat()
    idx = 0
    for i, f in enumerate(frames):
        if f['date'] <= d:
            idx = i
    return idx


def _backfill(snap, feed, frames, tz):
    """First run: everything that exists counts as frame 0, except what the activity feed saw appear later."""
    import collections
    keys = _keys(snap)
    hist = dict(pins={k: 0 for k in keys['pins']}, portals={k: 0 for k in keys['portals']},
                graves={k: 0 for k in keys['graves']}, pieces={})
    for k in keys['pieces']:
        hist['pieces'].setdefault(k, []).append(0)
    pin_at = collections.defaultdict(list)
    for k in keys['pins']:
        n, x, z, t = k.rsplit('|', 3); pin_at[(int(x), int(z))].append(k)
    portal_at = {(int(k.rsplit('|', 2)[1]), int(k.rsplit('|', 2)[2])): k for k in keys['portals']}
    piece_at = collections.defaultdict(list)
    for p, k in zip(snap['pieces'], keys['pieces']):
        piece_at[(p[0], p[2])].append(k)
    for e in sorted(feed.get('events', []), key=lambda e: e['t']):
        f = _frame_of(e['t'], frames, tz)
        if f == 0:
            continue
        if e['kind'] == 'pins':
            for x, z, _label in e.get('marks', []):
                for k in pin_at.get((round(x), round(z)), []):
                    hist['pins'][k] = f
        elif e['kind'] == 'portal' and e.get('x') is not None:
            for (x, z), k in portal_at.items():
                if abs(x - e['x']) <= 1 and abs(z - e['z']) <= 1:
                    hist['portals'][k] = f
        elif e['kind'] == 'death' and e.get('x') is not None:
            for k in hist['graves']:
                o, x, z, _d = k.rsplit('|', 3)
                if o == e.get('who') and abs(int(x) - e['x']) <= 1 and abs(int(z) - e['z']) <= 1:
                    hist['graves'][k] = f
        elif e['kind'] in ('build', 'newbase'):
            for x, z in e.get('pts', []):
                for k in piece_at.get((x, z), []):
                    fr = hist['pieces'][k]
                    if 0 in fr:                           # one copy at this spot appeared that day
                        fr[fr.index(0)] = f
                        break
    return hist


def objects(state_dir, snap, feed, frames, tz):
    """Update the history with today's objects. Returns first-seen frames aligned with the snapshot's lists."""
    path = os.path.join(state_dir, 'history.json.gz')
    idx = len(frames) - 1
    try:
        hist = json.loads(gzip.decompress(open(path, 'rb').read()))
    except Exception:
        hist = _backfill(snap, feed, frames, tz)
    keys = _keys(snap)
    out = {}
    for kind in ('pins', 'portals', 'graves'):
        h = hist.setdefault(kind, {})
        for k in keys[kind]:
            h.setdefault(k, idx)
        out[kind] = [h[k] for k in keys[kind]]
    hp, used = hist.setdefault('pieces', {}), {}
    fr = []
    for k in keys['pieces']:
        lst = hp.setdefault(k, [])
        n = used.get(k, 0)
        if n >= len(lst):
            lst.append(idx)
        fr.append(lst[n]); used[k] = n + 1
    for k in list(hp):                                     # forget copies that no longer exist
        if k not in used:
            del hp[k]
        else:
            hp[k] = hp[k][:used[k]]
    for kind in ('pins', 'portals', 'graves'):
        live = set(keys[kind])
        hist[kind] = {k: v for k, v in hist[kind].items() if k in live}
    out['pieces'] = fr
    open(path, 'wb').write(gzip.compress(json.dumps(hist, separators=(',', ':')).encode(), 9, mtime=0))
    return out
