"""Read cartography-table data out of a Valheim 1.0 world folder.

World folder layout (1.0):  _main.N.fwl2  _main.N.db2  _main.N.chunks  _main.N.ok  <x>_<y>__<g>_<s>.chunk
Each cartography table is a ZDO in a .chunk file whose byte-array field holds a gzip'd
Minimap.GetSharedMapData blob:
    int32 version (3) | int32 n (= 2048*2048) | n x bool explored
    | int32 pinCount | per pin: int64 owner, string name, vec3 pos, int32 type, bool checked, string author
Explored grid: 12 m per pixel, index = (z/12 + 1024) * 2048 + (x/12 + 1024).
"""
import glob, gzip, hashlib, os, re, struct, sys
import numpy as np

GRID, PX_M = 2048, 12
LAST_REPORT = None                                   # per-table summary from the last load()


def stable_hash(s: str) -> int:
    """Valheim's String.GetStableHashCode, as uint32."""
    n1 = n2 = 5381
    b = s.encode()
    for i in range(0, len(b), 2):
        n1 = ((n1 << 5) + n1 ^ b[i]) & 0xFFFFFFFF
        if i == len(b) - 1:
            break
        n2 = ((n2 << 5) + n2 ^ b[i + 1]) & 0xFFFFFFFF
    return (n1 + n2 * 1566083941) & 0xFFFFFFFF


TABLE_KEY = struct.pack('<I', stable_hash('piece_cartographytable'))


def read_str(d, p):
    ln = sh = 0
    while True:
        c = d[p]; p += 1
        ln |= (c & 0x7F) << sh; sh += 7
        if c < 128:
            break
    return d[p:p + ln].decode('utf-8', 'replace'), p + ln


def latest_generation(world_dir):
    """Highest save generation N that finished writing (has a _main.N.ok)."""
    gens = [int(re.search(r'_main\.(\d+)\.ok$', f).group(1)) for f in glob.glob(os.path.join(world_dir, '_main.*.ok'))]
    if not gens:
        raise SystemExit(f'no completed save (_main.N.ok) in {world_dir}')
    return max(gens)


def read_fwl2(world_dir, gen):
    d = open(os.path.join(world_dir, f'_main.{gen}.fwl2'), 'rb').read()
    p = 8
    name, p = read_str(d, p)
    seed_name, p = read_str(d, p)
    seed, uid, wgv = struct.unpack_from('<iqi', d, p)
    return dict(name=name, seed_name=seed_name, seed=seed, gen=wgv)


def chunk_files(world_dir, gen):
    """Chunk files listed by the save's .chunks index (ignores stale files); falls back to *.chunk."""
    idx = os.path.join(world_dir, f'_main.{gen}.chunks')
    if os.path.exists(idx):
        d = open(idx, 'rb').read()
        n, = struct.unpack_from('<i', d, 6)
        out = []
        for k in range(n):
            off = 10 + k * 11
            y, x, g = d[off], d[off + 1], d[off + 2]          # file name is <byte1>_<byte0>
            sv, = struct.unpack_from('<i', d, off + 3)
            f = os.path.join(world_dir, f'{x:02x}_{y:02x}__{g}_{sv}.chunk')
            if os.path.exists(f):
                out.append(f)
        if out:
            return out
    return sorted(glob.glob(os.path.join(world_dir, '*.chunk')))


def find_tables(files):
    """Yield (file, (x, y, z), raw_blob) for every cartography table with shared map data.
    Reads each table's own "data" byte array through the record parser, so a table that has
    never been written to is simply skipped and nothing from a neighbouring record is picked up."""
    import zdo
    key_data = stable_hash('data')
    table = struct.unpack('<I', TABLE_KEY)[0]
    for f in files:
        b = open(f, 'rb').read()
        if TABLE_KEY not in b:
            continue
        for fl, pos, prefab, fields in zdo.records(b):
            if prefab != table or pos is None:
                continue
            arr = fields.get('b', {}).get(key_data)
            if not arr:
                print(f'  table at ({pos[0]:.0f}, {pos[2]:.0f}) has nothing recorded yet', file=sys.stderr)
                continue
            try:
                yield f, pos, gzip.decompress(b[arr[0]:arr[0] + arr[1]])
            except Exception as e:
                print(f'  ! could not read the table at ({pos[0]:.0f}, {pos[2]:.0f}): {e}', file=sys.stderr)


def parse_table(d):
    ver, n = struct.unpack_from('<ii', d, 0)
    if ver != 3 or n != GRID * GRID:
        raise ValueError(f'unexpected map data version={ver} n={n}')
    ex = np.frombuffer(d, np.uint8, n, 8).reshape(GRID, GRID).astype(bool)
    p = 8 + n
    cnt, = struct.unpack_from('<i', d, p); p += 4
    pins = []
    for _ in range(cnt):
        owner, = struct.unpack_from('<q', d, p); p += 8   # player id of whoever placed it
        name, p = read_str(d, p)
        x, _, z = struct.unpack_from('<fff', d, p); p += 12
        typ, = struct.unpack_from('<i', d, p); p += 4
        checked = bool(d[p]); p += 1
        _author, p = read_str(d, p)                       # platform user id; never published
        pins.append(dict(name=name, x=x, z=z, type=typ, checked=checked, owner=owner))
    return ex, pins


def load(world_dir):
    """Union of all tables. Returns (info, explored, pins, digest, table_chunk_mtime)."""
    gen = latest_generation(world_dir)
    info = read_fwl2(world_dir, gen)
    files = chunk_files(world_dir, gen)
    ex = np.zeros((GRID, GRID), bool); pins = []; h = hashlib.sha256(); mtimes = []
    tables = list(find_tables(files))
    if not tables:
        raise SystemExit('no cartography tables with shared map data found')
    report = []
    for f, pos, blob in sorted(tables, key=lambda t: t[1]):
        e, p = parse_table(blob)
        km2 = e.sum() * PX_M * PX_M / 1e6
        print(f'  table at ({pos[0]:.0f}, {pos[2]:.0f}): {km2:.1f} km², {len(p)} pins', file=sys.stderr)
        report.append(dict(x=round(pos[0]), z=round(pos[2]), km2=round(float(km2), 2), pins=len(p),
                           crossed=sum(q['checked'] for q in p), only_here_km2=0.0, only_here_pins=0,
                           _ex=e, _keys={(q['name'], round(q['x']), round(q['z']), q['type']) for q in p}))
        ex |= e; pins += p; h.update(blob); mtimes.append(os.path.getmtime(f))
    # the union: every pin from every table once; crossed off if it is crossed off on any table
    merged = {}
    for p in pins:
        k = (p['name'], round(p['x']), round(p['z']), p['type'])
        if k in merged:
            merged[k]['checked'] = merged[k]['checked'] or p['checked']
        else:
            merged[k] = dict(p)
    uniq = list(merged.values())
    for r in report:                                      # what each table knows that no other table does
        others = np.zeros_like(ex)
        other_keys = set()
        for o in report:
            if o is not r:
                others |= o['_ex']; other_keys |= o['_keys']
        r['only_here_km2'] = round(float((r['_ex'] & ~others).sum() * PX_M * PX_M / 1e6), 2)
        r['only_here_pins'] = len(r['_keys'] - other_keys)
    for r in report:
        r.pop('_ex'); r.pop('_keys')
    global LAST_REPORT
    LAST_REPORT = dict(tables=report, union_km2=round(float(ex.sum() * PX_M * PX_M / 1e6), 2), union_pins=len(uniq))
    return info, ex, uniq, h.hexdigest(), max(mtimes)


BOSS = {'$enemy_eikthyr': 'Eikthyr', '$enemy_gdking': 'The Elder', '$enemy_bonemass': 'Bonemass',
        '$enemy_dragon': 'Moder', '$enemy_goblinking': 'Yagluth', '$enemy_seekerqueen': 'The Queen',
        '$enemy_fader': 'Fader', '$hud_pin_hildir1': 'Smouldering Tomb',
        '$hud_pin_hildir2': 'Howling Cavern', '$hud_pin_hildir3': 'Sealed Tower'}


def pretty(name):
    n = name.strip()
    if n.lower() in BOSS:
        return BOSS[n.lower()]
    if n.startswith('$'):
        return n.split('_')[-1].title()
    if len(n) > 1 and n[0].islower() and n[1:].isupper():   # caps-lock typo: "tROLL CAVE"
        n = n.swapcase()
    return n
