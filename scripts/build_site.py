#!/usr/bin/env python3
"""Build the known-world web map.

  build_site.py digest WORLD_DIR                 print a hash of the cartography-table data
  build_site.py build  WORLD_DIR DUMP_BIN OUT [STATE_DIR]
                                                 render tiles + index.html into OUT; with STATE_DIR,
                                                 also update the activity feed kept there

Terrain comes from vegvisr's Rust port of Valheim's world generator (the `dump` binary),
only for the explored bounding box. Unexplored ground is fogged.
"""
import datetime, json, math, os, re, shutil, subprocess, sys, tempfile
from zoneinfo import ZoneInfo
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vkw, objects, feed, timelapse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MPP, TILE, NATIVE_Z, MARGIN = 2.0, 512, 5, 24
GRID, PX_M = vkw.GRID, vkw.PX_M
TZ = os.environ.get('MAP_TZ', 'America/Sao_Paulo')

# in-game minimap palette
PAL = {1: (0.573, 0.655, 0.361), 2: (0.639, 0.447, 0.345), 4: (1, 1, 1), 8: (0.420, 0.455, 0.247),
       16: (0.906, 0.671, 0.470), 32: (0.690, 0.192, 0.192), 64: (0.85, 0.85, 1.0),
       256: (0.36, 0.45, 0.60), 512: (0.30, 0.28, 0.32)}
FOG = np.array([0.17, 0.16, 0.14], np.float32)
FOGV = np.array([0.07, 0.06, 0.05], np.float32)


def terrain(info, west, north, nx, ny, dump_bin):
    with tempfile.TemporaryDirectory() as td:
        pre = os.path.join(td, 't')
        subprocess.run([dump_bin, info['seed_name'], str(info['gen']), str(west), str(north),
                        str(nx), str(ny), str(MPP), pre], check=True, stderr=subprocess.DEVNULL)
        b = np.fromfile(pre + '.biome', np.uint16).reshape(ny, nx)
        h = np.fromfile(pre + '.height', np.float32).reshape(ny, nx)
        fo = np.fromfile(pre + '.forest', np.float32).reshape(ny, nx)
    col = np.zeros((ny, nx, 3), np.float32)
    for k, c in PAL.items():
        col[b == k] = c
    col[(fo < 1.15) & np.isin(b, [1, 16])] *= 0.82
    del fo
    gy, gx = np.gradient(h, MPP)
    slope = np.arctan(np.hypot(gx, gy) * 1.2); aspect = np.arctan2(-gx, gy); del gx, gy
    az, alt = np.radians(315), np.radians(45)
    shade = np.clip(np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)
    del slope, aspect
    col *= (0.55 + 0.6 * shade)[..., None]; del shade
    swamp = b == 2
    water = (h < 30) & ~swamp
    pud = (h < 30) & swamp                                   # swamp puddles: tint, don't paint as sea
    col[pud] = col[pud] * 0.55 + np.array([0.30, 0.36, 0.33]) * 0.45
    depth = np.clip((30 - h) / 60, 0, 1)[..., None] ** 0.6
    wc = np.array([0.45, 0.62, 0.70], np.float32) * (1 - depth) + np.array([0.10, 0.20, 0.33], np.float32) * depth
    col[water] = wc[water]; del wc, depth
    shore = water & ~ndimage.binary_erosion(water)
    col[shore] *= 0.65
    return col, b, h


def fogged(col, ex, west, north):
    """Terrain where charted, fading to transparent at the edge of the known world.
    The fog itself is drawn once, for the whole disk, by world_disk()."""
    ny, nx = col.shape[:2]
    wx = west + (np.arange(nx) + 0.5) * MPP
    wz = north - (np.arange(ny) + 0.5) * MPP
    gxi = np.clip(np.floor(wx / PX_M + GRID // 2).astype(int), 0, GRID - 1)
    gyi = np.clip(np.floor(wz / PX_M + GRID // 2).astype(int), 0, GRID - 1)
    m = ex[gyi[:, None], gxi[None, :]].astype(np.float32)
    m = np.clip(ndimage.gaussian_filter(m, 6) * 1.6, 0, 1)
    edge = (m > 0.35) & (m < 0.55)
    col[edge] *= 0.75
    # dark rim: blend towards the fog colour where the fog is thickest, so the fade matches the disk
    rgb = col * np.clip(m * 1.4, 0, 1)[..., None] + FOG * (1 - np.clip(m * 1.4, 0, 1))[..., None]
    a = np.clip(m, 0, 1)
    out = np.dstack([np.clip(rgb, 0, 1), a[..., None]]).reshape(ny, nx, 4)
    return Image.fromarray((out * 255).astype(np.uint8), 'RGBA')


WORLD_R, EDGE_R = 10000, 10500                     # playable radius, and where the world ends
DISK_MPP = 20


def world_disk(out):
    """The whole world as a disk: fog, the edge of the world, and where the Ashlands and the
    Deep North begin. Region rules are Valheim's own (WorldGenerator.IsAshlands / IsDeepNorth):
    distance from (0, +4000) or (0, -4000) beyond 12 000 m, give or take 100 m of wobble."""
    n = 2 * EDGE_R // DISK_MPP
    c = (np.arange(n) + 0.5) * DISK_MPP - EDGE_R
    x = c[None, :].astype(np.float32)
    z = -c[:, None].astype(np.float32)
    r = np.hypot(x, z)
    wobble = np.sin(np.arctan2(x, z) * 20.0) * 100.0
    ash = (np.hypot(x, z - 4000) > 12000 + wobble) & (r <= EDGE_R)
    north = (np.hypot(x, z + 4000) > 12000 + wobble) & (r <= EDGE_R)
    rng = np.random.default_rng(11)
    noise = sum(ndimage.zoom(rng.random((n // s + 3, n // s + 3)).astype(np.float32), s, order=3)[:n, :n] / (i + 1)
                for i, s in enumerate([64, 24, 8, 3]))
    noise = (noise - noise.min()) / (noise.max() - noise.min())
    img = FOG + FOGV * noise[..., None]
    for mask, tint, amt, line in ((ash, (0.42, 0.13, 0.08), 0.38, (0.86, 0.38, 0.22)),
                                  (north, (0.62, 0.70, 0.80), 0.30, (0.78, 0.88, 0.98))):
        img[mask] = img[mask] * (1 - amt) + np.array(tint, np.float32) * amt
        rim = mask & ~ndimage.binary_erosion(mask, iterations=2)
        rim &= r < EDGE_R - 60
        img[rim] = img[rim] * 0.3 + np.array(line, np.float32) * 0.7
    ring = (r > EDGE_R - 50) & (r <= EDGE_R)
    img[ring] = img[ring] * 0.4 + np.array([0.62, 0.57, 0.48], np.float32) * 0.6
    alpha = np.clip((EDGE_R - r) / DISK_MPP + 0.5, 0, 1)
    rgba = np.dstack([np.clip(img, 0, 1), alpha])
    Image.fromarray((rgba * 255).astype(np.uint8), 'RGBA').save(os.path.join(out, 'world.webp'), 'WEBP', quality=80, method=6)


BIOMES = {1: 'Meadows', 2: 'Swamp', 4: 'Mountains', 8: 'Black Forest', 16: 'Plains', 32: 'Ashlands',
          64: 'Deep North', 256: 'Ocean', 512: 'Mistlands'}


def death_places(graves, b, h, west, north):
    """Annotate graves with where they are: 'in the Swamp, in the water' / 'at sea'."""
    ny, nx = b.shape
    for g in graves:
        i, j = int((g['x'] - west) / MPP), int((north - g['z']) / MPP)
        if not (0 <= i < nx and 0 <= j < ny):
            continue
        biome = BIOMES.get(int(b[j, i]), '')
        g['biome'] = biome
        if biome == 'Ocean':
            g['where'] = 'at sea'
        elif biome:
            wet = g.get('water') or h[j, i] < 29.5
            g['where'] = f'in the {biome}' + (', in the water' if wet else '')


def write_tiles(im, out):
    nx, ny = im.size
    count = 0
    for z in range(NATIVE_Z, -1, -1):
        s = 2 ** (NATIVE_Z - z)
        lvl = im if s == 1 else im.convert('RGBa').resize((math.ceil(nx / s), math.ceil(ny / s)), Image.LANCZOS).convert('RGBA')
        w, h = lvl.size
        for tx in range(math.ceil(w / TILE)):
            for ty in range(math.ceil(h / TILE)):
                t = Image.new('RGBA', (TILE, TILE), (0, 0, 0, 0))
                t.paste(lvl.crop((tx * TILE, ty * TILE, min(w, (tx + 1) * TILE), min(h, (ty + 1) * TILE))), (0, 0))
                d = os.path.join(out, 'tiles', str(z), str(tx)); os.makedirs(d, exist_ok=True)
                t.save(os.path.join(d, f'{ty}.webp'), 'WEBP', quality=82, method=6)
                count += 1
    return count


def norm_name(n):
    return ' '.join(str(n).lower().split())


def load_portraits(out):
    """portraits/portraits.yaml -> {normalised name: 'portraits/<slug>.webp'}.

    Read as simple 'Name: file.jpg' lines rather than full YAML, so odd names (yes, no, 1984)
    stay names. Anything wrong with an entry skips just that entry; the page then draws the default.
    """
    folder = os.path.join(ROOT, 'portraits')
    cfg = os.path.join(folder, 'portraits.yaml')
    found = {}
    if not os.path.isfile(cfg):
        return found
    for ln, line in enumerate(open(cfg, encoding='utf-8', errors='replace'), 1):
        line = line.strip()
        if not line or line.startswith('#') or ':' not in line:
            continue
        name, fn = line.split(':', 1)
        name = name.strip().strip('"\'')
        fn = fn.split(' #')[0].strip().strip('"\'')
        if not name or not fn:
            continue
        src = os.path.join(folder, os.path.basename(fn))
        if not os.path.isfile(src):
            print(f'portraits: line {ln}: {os.path.basename(fn)} not found, using the default drawing for {name}', file=sys.stderr)
            continue
        try:
            im = ImageOps.exif_transpose(Image.open(src)).convert('RGB')
            im.thumbnail((512, 512))
            slug = re.sub(r'[^a-z0-9]+', '-', norm_name(name)).strip('-') or f'p{ln}'
            os.makedirs(os.path.join(out, 'portraits'), exist_ok=True)
            im.save(os.path.join(out, 'portraits', slug + '.webp'), 'WEBP', quality=85)
            found[norm_name(name)] = f'portraits/{slug}.webp'
        except Exception as e:
            print(f'portraits: line {ln}: could not read {os.path.basename(fn)} ({e}), using the default drawing', file=sys.stderr)
    return found


def build(world_dir, dump_bin, out, state_dir=None):
    info, ex, pins, objs, bosses, day, digest, mtime = objects.state(world_dir)
    saved_at = os.path.getmtime(os.path.join(world_dir, f'_main.{vkw.latest_generation(world_dir)}.ok'))
    ys, xs = np.nonzero(ex)
    gx0, gx1 = int(xs.min()) - MARGIN, int(xs.max()) + MARGIN + 1
    gy0, gy1 = int(ys.min()) - MARGIN, int(ys.max()) + MARGIN + 1
    west, east = (gx0 - GRID // 2) * PX_M, (gx1 - GRID // 2) * PX_M
    south, north = (gy0 - GRID // 2) * PX_M, (gy1 - GRID // 2) * PX_M
    nx, ny = int((east - west) / MPP), int((north - south) / MPP)
    print(f'rendering {nx}x{ny} px at {MPP} m/px', file=sys.stderr)

    col, biome, height = terrain(info, west, north, nx, ny, dump_bin)
    death_places(objs['graves'], biome, height, west, north)
    del biome, height
    if state_dir:
        fd = feed.update(state_dir, feed.snapshot(ex, pins, objs, bosses, day, int(saved_at)))
        print(f"feed: {len(fd['events'])} events since {fd['since']}", file=sys.stderr)
        page_feed = feed.for_page(fd)
    else:
        page_feed = dict(since=None, events=[])
    im = fogged(col, ex, west, north)
    del col
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    n = write_tiles(im, out)
    world_disk(out)
    portraits = load_portraits(out)
    tl = None
    if state_dir:
        fs, frames = timelapse.update(state_dir, ex, day, int(saved_at), TZ)
        tl = timelapse.for_page(fs, frames, gx0, gx1, gy0, gy1)
        print(f'timelapse: {len(frames)} frame(s)', file=sys.stderr)

    as_of = datetime.datetime.fromtimestamp(mtime, ZoneInfo(TZ)).strftime('%d %b %Y, %H:%M')
    km2 = round(float(ex.sum() * PX_M * PX_M / 1e6), 1)
    data = dict(world=info['name'], west=west, north=north, mpp=MPP, edgeR=EDGE_R, nx=nx, ny=ny, tile=TILE, nativeZ=NATIVE_Z,
                km2=km2, asOf=as_of, day=day, bosses=bosses,
                objects=dict(portals=objs['portals'], ships=objs['ships'], bases=objs['bases'],
                             pieces=objs['pieces'], materials=objs['materials'], graves=objs['graves']),
                clan=objs['clan'], feed=page_feed, tz=TZ,
                players=objs['players'], portraits=portraits, timelapse=tl,
                pins=[dict(n=vkw.pretty(p['name']), raw=p['name'], x=round(p['x'], 1), z=round(p['z'], 1),
                           t=p['type'], c=p['checked']) for p in pins])
    tpl = open(os.path.join(ROOT, 'web', 'template.html'), encoding='utf-8').read()
    css = ''.join(l for l in open(os.path.join(ROOT, 'web', 'leaflet.css'), encoding='utf-8') if 'url(' not in l)
    js_data = json.dumps(data, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')
    html = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + tpl.replace('{{WORLD}}', info['name']).replace('/*LEAFLET_CSS*/', css).replace('/*DATA*/null', js_data)
            + '</html>\n')
    open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(html)
    json.dump(dict(world=info['name'], km2=km2, pins=len(pins), day=day, asOf=as_of, digest=digest,
                   bosses=[b['name'] for b in bosses if b['done']], bases=len(objs['bases']),
                   portals=len(objs['portals'])),
              open(os.path.join(out, 'stats.json'), 'w'))
    open(os.path.join(out, '.nojekyll'), 'w').close()
    print(f'{n} tiles, {km2} km², {len(pins)} pins, as of {as_of}', file=sys.stderr)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'digest' and len(sys.argv) == 3:
        print(objects.state(sys.argv[2])[6])
    elif cmd == 'build' and len(sys.argv) in (5, 6):
        build(*sys.argv[2:])
    else:
        sys.exit(__doc__)
