"""Activity feed: snapshot the world each run and turn the differences since last run into events.

State lives in a directory (the workflow keeps it on a `data` branch):
  snapshot.json.gz   what the world looked like at the last run
  feed.json          {"since": <epoch>, "events": [...]}, newest first

The first run only writes a snapshot, so the feed starts empty.
"""
import base64, collections, gzip, json, math, os, time, zlib
import numpy as np

import vkw

VERSION = 1
KEEP = 400                  # events kept in feed.json
CELL = 48                   # metres per cell for newly charted areas (4 x 4 explored-grid pixels)


# ---------------------------------------------------------------- snapshot
def snapshot(ex, pins, objs, bosses, day, ts):
    packed = zlib.compress(np.packbits(ex).tobytes(), 9)
    return dict(
        v=VERSION, ts=ts, day=day,
        explored=base64.b64encode(packed).decode(),
        pins=[[p['name'], round(p['x'], 1), round(p['z'], 1), p['type'], p['checked'], owner_name(p, objs)] for p in pins],
        pieces=[list(p) for p in objs['raw']['pieces']],
        graves=[[g['owner'], g['x'], g['z'], g['day'], sum(r[1] for r in g['items']), g.get('where', '')]
                for g in objs['graves']],
        ships=[[s['kind'], s['x'], s['z'], s['lost']] for s in objs['ships']],
        portals=[[p['tag'], p['x'], p['z']] for p in objs['portals']],
        bosses=[b['key'] for b in bosses if b['done']],
        bases=[[b['name'], b['x'], b['z'], b['r'], b['pieces']] for b in objs['bases']],
    )


def owner_name(pin, objs):
    return objs.get('_idnames', {}).get(pin.get('owner'), '')


def explored(snap):
    bits = np.frombuffer(zlib.decompress(base64.b64decode(snap['explored'])), np.uint8)
    return np.unpackbits(bits)[:vkw.GRID * vkw.GRID].reshape(vkw.GRID, vkw.GRID).astype(bool)


def load_state(state_dir):
    snap, feed = None, None
    sp, fp = os.path.join(state_dir, 'snapshot.json.gz'), os.path.join(state_dir, 'feed.json')
    if os.path.exists(sp):
        snap = json.loads(gzip.decompress(open(sp, 'rb').read()))
        if snap.get('v') != VERSION:
            snap = None                                  # format changed: re-baseline quietly
    if os.path.exists(fp):
        feed = json.load(open(fp))
    return snap, feed


def save_state(state_dir, snap, feed):
    os.makedirs(state_dir, exist_ok=True)
    open(os.path.join(state_dir, 'snapshot.json.gz'), 'wb').write(
        gzip.compress(json.dumps(snap, separators=(',', ':'), ensure_ascii=False).encode(), 9, mtime=0))
    json.dump(feed, open(os.path.join(state_dir, 'feed.json'), 'w'), separators=(',', ':'), ensure_ascii=False)


# ---------------------------------------------------------------- naming places
class Places:
    def __init__(self, snap):
        self.bases = [dict(name=n, x=x, z=z, r=r) for n, x, z, r, _ in snap['bases']]
        self.pins = [(vkw.pretty(n), x, z) for n, x, z, t, c, o in snap['pins']
                     if n.strip() and not n.startswith('$') and len(n.strip()) >= 3]

    def base_at(self, x, z, pad=20):
        for b in self.bases:
            if math.hypot(b['x'] - x, b['z'] - z) <= b['r'] + pad:
                return b
        return None

    def name(self, x, z):
        b = self.base_at(x, z)
        if b and b['name']:
            return b['name']
        best = min(self.pins, key=lambda p: math.hypot(p[1] - x, p[2] - z), default=None)
        if best and math.hypot(best[1] - x, best[2] - z) < 400:
            return 'near ' + best[0]
        return f'at {round(x)}, {round(z)}'

    def at(self, x, z):
        """'at Vila Nova' / 'near Troll Cave' / 'at 120, -40' — for use after a verb."""
        n = self.name(x, z)
        return n if n.startswith(('near ', 'at ')) else 'at ' + n


def km(m):
    return f'{m / 1000:.1f} km' if m >= 1000 else f'{round(m)} m'


def names_list(xs, n=4):
    xs = list(dict.fromkeys(xs))
    return ', '.join(xs[:n]) + (f' and {len(xs) - n} more' if len(xs) > n else '')


# ---------------------------------------------------------------- diff
def mostly(ks):
    stone = sum(any(w in k[3] for w in ('stone', 'grausten', 'marble')) for k in ks)
    return ' (mostly stone)' if stone > len(ks) * 0.6 else ''


def diff(prev, cur):
    P = Places(cur)
    ev = []
    add = lambda kind, text, **kw: ev.append(dict(kind=kind, text=text, **kw))

    # bosses
    names = dict(eikthyr='Eikthyr', gdking='The Elder', bonemass='Bonemass', dragon='Moder', goblinking='Yagluth',
                 queen='The Queen', fader='Fader')
    for k in cur['bosses']:
        if k not in prev['bosses']:
            add('boss', f'{names.get(k, k.title())} has been defeated!')

    # buildings: multiset diff on exact piece placements
    key = lambda p: (p[0], p[1], p[2], p[3])
    pc, pp = collections.Counter(map(key, cur['pieces'])), collections.Counter(map(key, prev['pieces']))
    builder = {key(p): p[4] for p in cur['pieces']}
    added = [k for k, n in (pc - pp).items() for _ in range(n)]
    removed = [k for k, n in (pp - pc).items() for _ in range(n)]
    prev_bases = [dict(x=x, z=z, r=r) for _, x, z, r, _ in prev['bases']]
    new_base_ids = set()
    for b in P.bases:
        if not any(math.hypot(b['x'] - o['x'], b['z'] - o['z']) <= b['r'] + o['r'] for o in prev_bases):
            new_base_ids.add(id(b))
    groups = collections.defaultdict(list)
    for k in added:
        b = P.base_at(k[0], k[2], pad=5)
        groups[(builder.get(k, 'Someone'), id(b) if b else None)].append(k)
    founded = collections.defaultdict(list)                      # new bases: one event each, all builders
    for (who, bid), ks in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        b = next((b for b in P.bases if id(b) == bid), None)
        if b is not None and bid in new_base_ids:
            founded[bid].append((who, ks))
            continue
        if len(ks) < 3:
            continue
        x = sum(k[0] for k in ks) / len(ks); z = sum(k[2] for k in ks) / len(ks)
        add('build', f'{who} placed {len(ks)} pieces {P.at(x, z)}{mostly(ks)}',
            who=who, x=round(x, 1), z=round(z, 1), n=len(ks), pts=[[k[0], k[2]] for k in ks][:400])
    for bid, parts in founded.items():
        b = next(b for b in P.bases if id(b) == bid)
        ks = [k for _, g in parts for k in g]
        if len(ks) < 10:
            continue
        where = b['name'] or P.name(b['x'], b['z']).replace('at ', '', 1)
        helpers = [w for w, _ in parts[1:]]
        withh = f' with {names_list(helpers, 3)}' if helpers else ''
        add('newbase', f'{parts[0][0]} founded a new base{withh}: {where}, {len(ks)} pieces so far{mostly(ks)}',
            who=parts[0][0], x=round(b['x'], 1), z=round(b['z'], 1), n=len(ks), pts=[[k[0], k[2]] for k in ks][:400])
    rgroups = collections.defaultdict(list)
    for k in removed:
        b = P.base_at(k[0], k[2], pad=5)
        rgroups[P.name(b['x'], b['z']) if b else P.name(k[0], k[2])].append(k)
    for where, ks in rgroups.items():
        if len(ks) >= 10:
            x = sum(k[0] for k in ks) / len(ks); z = sum(k[2] for k in ks) / len(ks)
            place = where if where.startswith(('near ', 'at ')) else 'at ' + where
            add('demolish', f'{len(ks)} pieces were torn down or destroyed {place}', x=round(x, 1), z=round(z, 1), n=len(ks))

    # deaths and recovered graves
    gk = lambda g: (g[0], round(g[1]), round(g[2]), g[3])
    prev_g, cur_g = {gk(g): g for g in prev['graves']}, {gk(g): g for g in cur['graves']}
    for k, g in cur_g.items():
        if k not in prev_g:
            where = (g[5] + ' ') if len(g) > 5 and g[5] else ''
            add('death', f"{g[0]} died {where}{P.at(g[1], g[2])}" + (f", leaving {g[4]} items in the grave" if g[4] else ''),
                who=g[0], x=g[1], z=g[2])
    for k, g in prev_g.items():
        if k not in cur_g:
            add('recovered', f"{g[0]}'s grave {P.at(g[1], g[2])} was emptied", who=g[0], x=g[1], z=g[2])

    # portals
    tags_prev = collections.Counter(p[0] for p in prev['portals'] if p[0])
    tags_cur = collections.defaultdict(list)
    for p in cur['portals']:
        if p[0]:
            tags_cur[p[0]].append(p)
    pk = lambda p: (p[0], round(p[1]), round(p[2]))
    prev_pk = {pk(p) for p in prev['portals']}
    for tag, ps in tags_cur.items():
        if len(ps) == 2 and tags_prev.get(tag, 0) != 2:
            a, b = ps
            add('portal', f"Portal link “{tag}” opened: {P.name(a[1], a[2])} ↔ {P.name(b[1], b[2])}, "
                          f"{km(math.hypot(a[1] - b[1], a[2] - b[2]))}", x=b[1], z=b[2],
                line=[[a[1], a[2]], [b[1], b[2]]])
        else:
            for p in ps:
                if pk(p) not in prev_pk:
                    add('portal', f"New portal “{tag}” {P.at(p[1], p[2])}", x=p[1], z=p[2])

    # pins recorded and crossed off
    pinkey = lambda p: (p[0], round(p[1]), round(p[2]), p[3])
    prev_pins = {pinkey(p): p for p in prev['pins']}
    new_by = collections.defaultdict(list)
    crossed = []
    for p in cur['pins']:
        old = prev_pins.get(pinkey(p))
        if old is None:
            new_by[p[5] or 'Someone'].append(p)
        elif p[4] and not old[4]:
            crossed.append(p)
    for who, ps in new_by.items():
        labels = [vkw.pretty(p[0]) for p in ps if p[0].strip()]
        what = f': {names_list(labels)}' if labels else ''
        one = len(ps) == 1
        add('pins', f'{who} recorded {"a new pin" if one else f"{len(ps)} new pins"}{what}', who=who,
            x=ps[0][1] if one else None, z=ps[0][2] if one else None,
            marks=[[p[1], p[2], vkw.pretty(p[0])] for p in ps][:60])
    if crossed:
        labels = [vkw.pretty(p[0]) or 'a pin' for p in crossed]
        add('crossed', f'Crossed off on the map: {names_list(labels)}',
            marks=[[p[1], p[2], vkw.pretty(p[0])] for p in crossed][:60])

    # newly charted ground
    ec, ep = explored(cur), explored(prev)
    new = ec & ~ep
    if new.sum() * vkw.PX_M ** 2 >= 20_000:                  # ignore < 0.02 km²
        g = vkw.GRID // (CELL // vkw.PX_M)
        coarse = new.reshape(g, CELL // vkw.PX_M, g, CELL // vkw.PX_M).any(axis=(1, 3))
        ys, xs = np.nonzero(coarse)
        cells = [[int((x + 0.5) * CELL - vkw.GRID * vkw.PX_M / 2), int((y + 0.5) * CELL - vkw.GRID * vkw.PX_M / 2)]
                 for y, x in zip(ys, xs)]
        cx = sum(c[0] for c in cells) / len(cells); cz = sum(c[1] for c in cells) / len(cells)
        add('charted', f'{new.sum() * vkw.PX_M ** 2 / 1e6:.2f} km² of new ground charted, mostly {P.name(cx, cz).replace("at ", "around ", 1)}',
            x=round(cx), z=round(cz), cells=cells[:3000])

    # ships and carts: match each to the nearest one of the same kind
    left = [list(s) for s in prev['ships']]
    for s in cur['ships']:
        same = [o for o in left if o[0] == s[0]]
        o = min(same, key=lambda o: math.hypot(o[1] - s[1], o[2] - s[2]), default=None)
        noun = 'cart' if s[0] == 'Cart' else s[0].lower()
        if o is None:
            add('ship', f'A new {noun} appeared {P.at(s[1], s[2])}', x=s[1], z=s[2])
            continue
        left.remove(o)
        d = math.hypot(o[1] - s[1], o[2] - s[2])
        if s[3] and not o[3]:
            add('ship', f'The {noun} was lost off the edge of the world', x=s[1], z=s[2])
        elif d >= 200:
            verb = 'was hauled' if s[0] == 'Cart' else 'sailed'
            add('ship', f'The {noun} {verb} {km(d)}: {P.name(o[1], o[2])} → {P.name(s[1], s[2])}',
                x=s[1], z=s[2], line=[[o[1], o[2]], [s[1], s[2]]])
    for o in left:
        noun = 'cart' if o[0] == 'Cart' else o[0].lower()
        add('ship', f'The {noun} {P.at(o[1], o[2])} is gone', x=o[1], z=o[2])

    order = ['boss', 'newbase', 'death', 'portal', 'build', 'pins', 'charted', 'ship', 'recovered', 'crossed', 'demolish']
    ev.sort(key=lambda e: order.index(e['kind']))
    for i, e in enumerate(ev):
        e.update(t=cur['ts'], day=cur['day'], id=f"{cur['ts']}-{i}")
        for k in [k for k, v in e.items() if v is None]:
            del e[k]
    return ev


def update(state_dir, cur):
    """Diff against the stored snapshot, append events, store the new snapshot. Returns the feed."""
    prev, feed = load_state(state_dir)
    feed = feed or dict(since=cur['ts'], events=[])
    if prev is not None and prev['ts'] != cur['ts']:
        feed['events'] = diff(prev, cur) + feed['events']
        feed['events'] = sorted(feed['events'], key=lambda e: -e['t'])[:KEEP]
    save_state(state_dir, cur, feed)
    return feed


def for_page(feed, now=None, keep_geometry_days=7):
    """Trim what goes into the page: geometry only for recent events."""
    now = now or time.time()
    out = []
    for e in feed['events'][:250]:
        e = dict(e)
        if now - e['t'] > keep_geometry_days * 86400:
            for k in ('cells', 'pts', 'marks', 'line'):
                e.pop(k, None)
        out.append(e)
    return dict(since=feed['since'], events=out)
