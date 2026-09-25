"""World objects for the map: portals, player buildings (bases), beds, ships, tombstones, chests,
boss progress, day, and the clan stats built from them (builders, cartographers, graveyard, treasury)."""
import collections, gzip, math, os, re, struct, zlib

import vkw, zdo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
H = vkw.stable_hash
K_CREATOR, K_TAG, K_OWNER_NAME = H('creator'), H('tag'), H('ownerName')
K_OWNER, K_ITEMS, K_TIME_OF_DEATH = H('owner'), H('items'), H('timeOfDeath')
TICKS_PER_DAY = 1800 * 10_000_000                           # a Valheim day is 30 min; times are .NET ticks

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


def items(blob):
    """Decode a 1.0 inventory (version 109):
    [version i32][count u16] then per item:
    [i32 durability-ish][u8 x][u8 y][u8 ?][u8 flags]
    [u16 quality if bit2][u16 stack if bit3][i32 variant if bit4][i64 crafter id + str name if bit5]
    [u32 item prefab hash][u8 ?]"""
    ver, = struct.unpack_from('<i', blob, 0)
    if ver != 109:
        raise ValueError(f'unknown inventory version {ver}')
    cnt, = struct.unpack_from('<H', blob, 4)
    p, out = 6, []
    for _ in range(cnt):
        p += 7
        fl = blob[p]; p += 1
        it = dict(quality=1, stack=1)
        if fl & 0b10:
            p += 4                                          # not seen in the wild; best guess
        if fl & (1 << 2):
            it['quality'], = struct.unpack_from('<H', blob, p); p += 2
        if fl & (1 << 3):
            it['stack'], = struct.unpack_from('<H', blob, p); p += 2
        if fl & (1 << 4):
            p += 4
        if fl & (1 << 5):
            it['crafter_id'], = struct.unpack_from('<q', blob, p); p += 8
            ln = blob[p]; p += 1
            it['crafter'] = blob[p:p + ln].decode('utf-8', 'replace'); p += ln
        h, = struct.unpack_from('<I', blob, p); p += 5
        it['prefab'] = names().get(h, '')
        out.append(it)
    if p != len(blob):
        raise ValueError('inventory length mismatch')
    return out


ITEM_NAMES = {'RoundLog': 'Core wood', 'ElderBark': 'Ancient bark', 'IronScrap': 'Scrap iron',
              'BlackMetalScrap': 'Black metal scrap', 'FineWood': 'Fine wood', 'YggdrasilWood': 'Yggdrasil wood',
              'LeatherScraps': 'Leather scraps', 'BoneFragments': 'Bone fragments', 'SurtlingCore': 'Surtling core',
              'WitheredBone': 'Withered bone', 'Wishbone': 'Wishbone', 'Ooze': 'Ooze', 'GreydwarfEye': 'Greydwarf eye',
              'BlackMetal': 'Black metal', 'FlametalNew': 'Flametal', 'Coins': 'Coins', 'AmberPearl': 'Amber pearl'}
_WORD = re.compile(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+')


def item_name(prefab):
    if prefab in ITEM_NAMES:
        return ITEM_NAMES[prefab]
    base = prefab.split('_')[0]
    words = _WORD.findall(base) or [base]
    if words[0] in ('Arrow', 'Bolt') and len(words) > 1:           # ArrowFlint -> Flint arrow
        words = words[1:] + [words[0]]
    elif words[0] == 'Trophy' and len(words) > 1:                  # TrophyDeer -> Deer trophy
        words = words[1:] + ['trophy']
    elif words[0] in ('Sword', 'Axe', 'Mace', 'Knife', 'Spear', 'Bow', 'Shield', 'Helmet', 'Armor', 'Cape',
                      'Atgeir', 'Pickaxe', 'Sledge', 'Battleaxe', 'Crossbow', 'Staff', 'Club', 'Torch') and len(words) > 1:
        words = words[1:] + [words[0]]                             # SwordIron -> Iron sword
    txt = ' '.join(words).lower()
    return txt[:1].upper() + txt[1:]


TREASURY_GROUPS = [
    ('Metals', ['Copper', 'Tin', 'Bronze', 'Iron', 'Silver', 'BlackMetal', 'Flametal', 'FlametalNew',
                'CopperOre', 'TinOre', 'IronScrap', 'IronOre', 'SilverOre', 'BlackMetalScrap', 'CopperScrap']),
    ('Building', ['Wood', 'FineWood', 'RoundLog', 'ElderBark', 'YggdrasilWood', 'Blackwood', 'Stone', 'Flint',
                  'Resin', 'Obsidian', 'Crystal', 'BlackMarble', 'Grausten', 'Tar', 'Chitin']),
    ('Hides & mob drops', ['LeatherScraps', 'DeerHide', 'TrollHide', 'WolfPelt', 'LoxPelt', 'ScaleHide',
                           'BjornHide', 'Feathers', 'BoneFragments', 'GreydwarfEye', 'Guck', 'Ooze', 'Entrails',
                           'WitheredBone', 'SurtlingCore', 'Chain', 'Needle', 'FreezeGland', 'WolfFang',
                           'Bloodbag', 'Root', 'AncientSeed']),
    ('Valuables', ['Coins', 'Amber', 'AmberPearl', 'Ruby', 'SilverNecklace']),
]


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
    # what counts as "the map changed": tables, portals, base sizes, ships moved >25 m, graves, bosses
    summary = dict(p=sorted((p['tag'], round(p['x']), round(p['z'])) for p in objs['portals']),
                   b=sorted((round(b['x'] / 25), round(b['z'] / 25), b['pieces'] // 10) for b in objs['bases']),
                   s=sorted((s['kind'], round(s['x'] / 25), round(s['z'] / 25)) for s in objs['ships']),
                   g=sorted((t['owner'], round(t['x']), round(t['z'])) for t in objs['graves']),
                   k=sorted(keys))
    digest = hashlib.sha256((table_digest + json.dumps(summary)).encode()).hexdigest()
    return info, ex, pins, objs, bosses, day, digest, mtime


def extract(world_dir, gen, files, pins):
    N = names()
    portals, ships, beds, pieces, tombs, chests, hives = [], [], [], [], [], [], []
    ids = {}                                                     # player id -> character name
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
                ships.append(dict(kind=SHIPS[name], x=round(x, 1), y=round(y, 1), z=round(z, 1), lost=lost,
                                  _by=fields.get('l', {}).get(K_CREATOR, 0)))
            l = fields.get('l', {})
            if name == 'bed' and s.get(K_OWNER_NAME):
                beds.append(dict(owner=s[K_OWNER_NAME], x=round(x, 1), z=round(z, 1)))
                if l.get(K_OWNER):
                    ids[l[K_OWNER]] = s[K_OWNER_NAME]
            creator = l.get(K_CREATOR, 0)
            if name == 'piece_beehive' and creator:
                hives.append(creator)
            if creator:
                pieces.append((x, z, material(name), name, creator, y))
            blob = fields.get('b', {}).get(K_ITEMS)
            if blob and (name == 'Player_tombstone' or creator):
                try:
                    inv = items(b[blob[0]:blob[0] + blob[1]])
                except Exception:
                    continue
                for it in inv:
                    if it.get('crafter_id') and it.get('crafter'):
                        ids.setdefault(it['crafter_id'], it['crafter'])
                if name == 'Player_tombstone':
                    if l.get(K_OWNER) and s.get(K_OWNER_NAME):
                        ids[l[K_OWNER]] = s[K_OWNER_NAME]
                    tombs.append(dict(owner=s.get(K_OWNER_NAME, 'Someone'), x=round(x, 1), z=round(z, 1),
                                      day=int(l.get(K_TIME_OF_DEATH, 0) // TICKS_PER_DAY), items=inv))
                else:
                    chests.append(dict(x=x, z=z, items=inv, creator=creator))

    bases = cluster(pieces, beds, portals, pins)
    base_details(bases, ids, chests, pins, tombs)
    who = lambda pid: ids.get(pid, 'Unknown viking')
    raw = dict(pieces=[(round(x, 1), round(y, 1), round(z, 1), name, who(pid)) for x, z, _m, name, pid, y in pieces])
    people = players(ids, pieces, pins, tombs, chests, bases, beds, ships, hives)
    for b in bases:
        b.pop('_ps', None)
    for sh in ships:
        sh.pop('_by', None)
    mat_count = collections.Counter(p[2] for p in pieces)
    return dict(portals=portals, ships=ships, bases=bases, clan=clan(ids, pieces, pins, tombs, chests, bases), raw=raw,
                _idnames=dict(ids), players=people,
                graves=[dict(owner=t['owner'], x=t['x'], z=t['z'], day=t['day'], items=summarise(t['items']))
                        for t in tombs],
                pieces=[[round(x, 1), round(z, 1), MAT_IDS.get(m, len(MATERIALS))] for x, z, m, *_ in pieces],
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
        bases.append(dict(_ps=ps, x=round(cx, 1), z=round(cz, 1), r=round(r, 1), pieces=len(ps),
                          name=vkw.pretty(named[0]['name']) if named else (tags[0] if tags else None),
                          beds=owners, portals=tags,
                          workbenches=sum(v for k, v in kinds.items() if 'workbench' in k and 'ext' not in k)))
    bases.sort(key=lambda b: -b['pieces'])
    return bases


def base_details(bases, ids, chests, pins, tombs):
    """Per base: who built it, what its chests hold, which pins and graves are in or near it."""
    who = lambda pid: ids.get(pid, 'Unknown viking')
    near = lambda b, o, pad: math.hypot(o['x'] - b['x'], o['z'] - b['z']) <= b['r'] + pad
    for b in bases:
        builders = collections.Counter(who(p[4]) for p in b['_ps'])
        b['builders'] = [[n, c] for n, c in builders.most_common()]
        stone = sum(1 for p in b['_ps'] if p[2] == 'stone')
        b['stone'] = round(100 * stone / max(1, len(b['_ps'])))
        store, nchests = collections.Counter(), 0
        for ch in chests:
            if near(b, ch, 5):
                nchests += 1
                for it in ch['items']:
                    if it['prefab']:
                        store[it['prefab']] += it['stack']
        b['chests'] = nchests
        b['store'] = [[item_name(k), n] for k, n in store.most_common(40)]
        b['store_total'] = sum(store.values())
        b['pins'] = sorted({vkw.pretty(p['name']) for p in pins
                            if p['name'].strip() and not p['name'].startswith('$') and near(b, p, 40)})
        b['graves'] = sum(1 for t in tombs if near(b, t, 60))


def summarise(inv):
    """Inventory -> [[display name, count, quality, crafter]] with equal stacks merged, biggest first."""
    agg = collections.OrderedDict()
    for it in inv:
        if not it['prefab']:
            continue
        k = (it['prefab'], it['quality'] if it['quality'] > 1 else 1, it.get('crafter', ''))
        agg[k] = agg.get(k, 0) + it['stack']
    rows = [[item_name(p), n, q, c] for (p, q, c), n in agg.items()]
    rows.sort(key=lambda r: (-(r[2] > 1 or bool(r[3])), -r[1]))    # crafted gear first, then big stacks
    return rows


def clan(ids, pieces, pins, tombs, chests, bases):
    """Per-player and group stats. Only character names leave this function, never ids."""
    who = lambda pid: ids.get(pid, 'Unknown viking')

    # builders
    b = collections.defaultdict(lambda: dict(pieces=0, stone=0, wood=0, other=0, bases=set()))
    base_of = {}
    for i, base in enumerate(bases):
        base_of[i] = base
    for x, z, mat, _name, pid, *_ in pieces:
        r = b[who(pid)]
        r['pieces'] += 1
        r['stone' if mat == 'stone' else 'wood' if mat in ('wood', 'darkwood') else 'other'] += 1
        for i, base in base_of.items():
            if math.hypot(x - base['x'], z - base['z']) <= base['r']:
                r['bases'].add(i); break
    builders = sorted(({'name': n, **{k: (len(v) if k == 'bases' else v) for k, v in r.items()}}
                       for n, r in b.items()), key=lambda r: -r['pieces'])

    # cartographers (pins recorded at the tables)
    c = collections.defaultdict(lambda: dict(pins=0, crossed=0, bosses=0))
    for p in pins:
        r = c[who(p.get('owner'))]
        r['pins'] += 1
        r['crossed'] += int(p['checked'])
        r['bosses'] += int(p['type'] == 9)
    cartographers = sorted(({'name': n, **r} for n, r in c.items()), key=lambda r: -r['pins'])

    # graveyard
    d = collections.Counter(t['owner'] for t in tombs)
    deaths = [dict(name=n, graves=k) for n, k in d.most_common()]

    # treasury: player-built containers only (dungeon loot chests are the world's, not the clan's)
    total = collections.Counter()
    for ch in chests:
        for it in ch['items']:
            if it['prefab']:
                total[it['prefab']] += it['stack']
    groups, used = [], set()
    for title, keys in TREASURY_GROUPS:
        rows = [[item_name(k), total[k]] for k in keys if total.get(k)]
        rows.sort(key=lambda r: -r[1])
        used.update(keys)
        if rows:
            groups.append(dict(title=title, rows=rows))
    rest = [[item_name(k), n] for k, n in total.most_common() if k not in used][:18]
    if rest:
        groups.append(dict(title='Everything else', rows=rest))
    return dict(builders=builders, cartographers=cartographers, deaths=deaths,
                treasury=dict(chests=len(chests), stacks=sum(len(ch['items']) for ch in chests),
                              items=sum(total.values()), groups=groups))


def players(ids, pieces, pins, tombs, chests, bases, beds, ships, hives):
    """One profile per character name. Built only from what the world file stores; ids never leave here."""
    who = lambda pid: ids.get(pid)
    P = collections.defaultdict(lambda: dict(pieces=0, stone=0, wood=0, bases=[], pins=0, crossed=0, bosses_found=0,
                                             graves=[], ships=collections.Counter(), hives=0, homes=[],
                                             crafted=collections.Counter(), crafted_total=0))
    for x, z, mat, _n, pid, *_ in pieces:
        n = who(pid)
        if not n:
            continue
        P[n]['pieces'] += 1
        if mat == 'stone':
            P[n]['stone'] += 1
        elif mat in ('wood', 'darkwood'):
            P[n]['wood'] += 1
    for bi, b in enumerate(bases):
        for n, c in b.get('builders', []):
            if n in P or n in ids.values():
                P[n]['bases'].append([bi, c])
    for p in pins:
        n = who(p.get('owner'))
        if n:
            P[n]['pins'] += 1; P[n]['crossed'] += int(p['checked']); P[n]['bosses_found'] += int(p['type'] == 9)
    for t in tombs:
        P[t['owner']]['graves'].append([t['x'], t['z'], t['day']])
    for sh in ships:
        n = who(sh.get('_by'))
        if n:
            P[n]['ships'][sh['kind']] += 1
    for h in hives:
        n = who(h)
        if n:
            P[n]['hives'] += 1
    for b in beds:
        home = next((i for i, base in enumerate(bases)
                     if math.hypot(b['x'] - base['x'], b['z'] - base['z']) <= base['r'] + 5), None)
        P[b['owner']]['homes'].append([b['x'], b['z'], home])
    for inv in [c['items'] for c in chests] + [t['items'] for t in tombs]:
        for it in inv:
            if it.get('crafter') and it['prefab']:
                P[it['crafter']]['crafted'][item_name(it['prefab'])] += 1
                P[it['crafter']]['crafted_total'] += 1
    out = []
    for n, r in P.items():
        r['bases'].sort(key=lambda bc: -bc[1])
        r['ships'] = [[k, v] for k, v in r['ships'].most_common()]
        r['crafted'] = [[k, v] for k, v in r['crafted'].most_common(12)]
        r['graves'].sort(key=lambda g: -g[2])
        out.append(dict(name=n, **r))
    out.sort(key=lambda r: -(r['pieces'] + 5 * r['pins'] + r['crafted_total']))
    return out
