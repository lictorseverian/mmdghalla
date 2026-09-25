"""World objects for the map: portals, player buildings (bases), beds, ships, boss progress, day."""
import collections, gzip, math, os, re, struct, zlib

import vkw, zdo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = vkw.stable_hash
K_CREATOR, K_TAG, K_OWNER_NAME = H('creator'), H('tag'), H('ownerName')

_names = None


def names():
    """prefab hash -> name, from the vendored name list (MIT, Erhuangjing/valheim-save-research)."""
    global _names
    if _names is None:
        raw = gzip.decompress(open(os.path.join(ROOT, 'data', 'prefab-names.txt.gz'), 'rb').read()).decode()
        _names = {H(n): n for n in raw.split('\n') if n}
    return _names


SHIPS = {'Raft': 'Raft', 'Karve': 'Karve', 'VikingShip': 'Longship', 'VikingShip_Ashlands': 'Drakkar',
         'Trailership': 'Trader ship', 'Cart': 'Cart'}
BOSS_KEYS = [('eikthyr', 'Eikthyr'), ('gdking', 'The Elder'), ('bonemass', 'Bonemass'), ('dragon', 'Moder'),
             ('goblinking', 'Yagluth'), ('queen', 'The Queen'), ('fader', 'Fader')]

# material buckets for drawing building footprints
MATERIALS = [('stone', ('stone', 'grausten', 'blackmarble', 'marble', 'crystal')),
             ('iron', ('iron', 'metal', 'copper', 'dvergr')),
             ('darkwood', ('darkwood', 'ashwood', 'yggdrasil', 'root', 'stake')),
             ('wood', ('wood', 'log', 'roof', 'beam', 'pole', 'stair', 'ladder', 'floor', 'wall', 'door', 'gate')),
             ('craft', ('workbench', 'forge', 'chest', 'bed', 'fire', 'hearth', 'torch', 'brazier', 'sconce',
                        'cauldron', 'kiln', 'smelter', 'table', 'sign', 'cart', 'rack', 'portal', 'banner',
                        'stool', 'chair', 'throne', 'bench', 'windmill', 'spinning', 'oven', 'beehive', 'fermenter',
                        'smoke', 'blast', 'refinery', 'stonecutter', 'artisan', 'galdr', 'mage', 'lantern'))]
MAT_IDS = {m: i for i, (m, _) in enumerate(MATERIALS)}


def material(name):
    n = name.lower()
    for m, keys in MATERIALS:
        if any(k in n for k in keys):
            return m
    return 'other'


def db2_state(world_dir, gen):
    """(defeated boss keys, in-game day) from _main.N.db2 (16-byte header, then zlib/gzip data)."""
    raw = open(os.path.join(world_dir, f'_main.{gen}.db2'), 'rb').read()
    net_time, = struct.unpack_from('<d', raw, 4)
    try:
        d = zlib.decompress(raw[16:], 47)
    except zlib.error:
        d = raw[16:]
    keys = set(m.group(1).decode() for m in re.finditer(rb'defeated_([a-z]+)', d))
    return keys, int(net_time // 1800)


def state(world_dir):
    """Everything the page shows: (info, explored, pins, objects, bosses, day, digest, as_of_mtime)."""
    import hashlib, json
    info, ex, pins, table_digest, mtime = vkw.load(world_dir)
    gen = vkw.latest_generation(world_dir)
    objs = extract(world_dir, gen, vkw.chunk_files(world_dir, gen), pins)
    keys, day = db2_state(world_dir, gen)
    bosses = [dict(key=k, name=n, done=k in keys) for k, n in BOSS_KEYS]
    # what counts as "the map changed": tables, portals, base sizes, ships moved >25 m, bosses
    summary = dict(p=sorted((p['tag'], round(p['x']), round(p['z'])) for p in objs['portals']),
                   b=sorted((round(b['x'] / 25), round(b['z'] / 25), b['pieces'] // 10) for b in objs['bases']),
                   s=sorted((s['kind'], round(s['x'] / 25), round(s['z'] / 25)) for s in objs['ships']),
                   k=sorted(keys))
    digest = hashlib.sha256((table_digest + json.dumps(summary)).encode()).hexdigest()
    return info, ex, pins, objs, bosses, day, digest, mtime


def extract(world_dir, gen, files, pins):
    N = names()
    portals, ships, beds, pieces = [], [], [], []
    for f in files:
        b = open(f, 'rb').read()
        for fl, pos, prefab, fields in zdo.records(b):
            if pos is None:
                continue
            name = N.get(prefab)
            if not name:
                continue
            x, y, z = pos
            s = fields.get('s', {})
            if name.startswith('portal'):
                portals.append(dict(tag=s.get(K_TAG, '').strip(), x=round(x, 1), z=round(z, 1)))
            elif name in SHIPS:
                lost = y < -50 or math.hypot(x, z) > 10500
                ships.append(dict(kind=SHIPS[name], x=round(x, 1), y=round(y, 1), z=round(z, 1), lost=lost))
            if name == 'bed' and s.get(K_OWNER_NAME):
                beds.append(dict(owner=s[K_OWNER_NAME], x=round(x, 1), z=round(z, 1)))
            if K_CREATOR in fields.get('l', {}) and fields['l'][K_CREATOR] != 0:
                pieces.append((x, z, material(name), name))

    bases = cluster(pieces, beds, portals, pins)
    mat_count = collections.Counter(p[2] for p in pieces)
    return dict(portals=portals, ships=ships, bases=bases,
                pieces=[[round(x, 1), round(z, 1), MAT_IDS.get(m, len(MATERIALS))] for x, z, m, _ in pieces],
                materials=[m for m, _ in MATERIALS] + ['other'], mat_count=dict(mat_count))


def cluster(pieces, beds, portals, pins, cell=24):
    """Group building pieces into bases: 8-connected 24 m grid cells that contain pieces."""
    cells = collections.defaultdict(list)
    for p in pieces:
        cells[(int(p[0] // cell), int(p[1] // cell))].append(p)
    seen, bases = set(), []
    for c0 in cells:
        if c0 in seen:
            continue
        stack, comp = [c0], []
        seen.add(c0)
        while stack:
            c = stack.pop(); comp.append(c)
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    nb = (c[0] + dx, c[1] + dz)
                    if nb in cells and nb not in seen:
                        seen.add(nb); stack.append(nb)
        ps = [p for c in comp for p in cells[c]]
        if len(ps) < 4:
            continue
        xs, zs = [p[0] for p in ps], [p[1] for p in ps]
        cx, cz = sum(xs) / len(xs), sum(zs) / len(zs)
        r = max(math.hypot(x - cx, z - cz) for x, z in zip(xs, zs)) + 10
        inside = lambda o, pad=0: math.hypot(o['x'] - cx, o['z'] - cz) <= r + pad
        owners = sorted({b['owner'] for b in beds if inside(b)})
        tags = sorted({p['tag'] for p in portals if p['tag'] and inside(p)})
        named = [p for p in pins if p['name'].strip() and not p['name'].startswith('$') and inside(p, 40)]
        named.sort(key=lambda p: (len(p['name'].strip()) < 3, p['type'] not in (0, 1), math.hypot(p['x'] - cx, p['z'] - cz)))
        if named and len(named[0]['name'].strip()) < 3 and tags:
            named = []
        kinds = collections.Counter(p[3] for p in ps)
        bases.append(dict(x=round(cx, 1), z=round(cz, 1), r=round(r, 1), pieces=len(ps),
                          name=vkw.pretty(named[0]['name']) if named else (tags[0] if tags else None),
                          beds=owners, portals=tags,
                          workbenches=sum(v for k, v in kinds.items() if 'workbench' in k and 'ext' not in k)))
    bases.sort(key=lambda b: -b['pieces'])
    return bases
