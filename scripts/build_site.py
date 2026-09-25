#!/usr/bin/env python3
"""Build the known-world web map.

  build_site.py digest WORLD_DIR                 print a hash of the cartography-table data
  build_site.py build  WORLD_DIR DUMP_BIN OUT    render tiles + index.html into OUT

Terrain comes from vegvisr's Rust port of Valheim's world generator (the `dump` binary),
only for the explored bounding box. Unexplored ground is fogged.
"""
import datetime, json, math, os, shutil, subprocess, sys, tempfile
from zoneinfo import ZoneInfo
import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vkw, objects

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
    return col


def fogged(col, ex, west, north):
    ny, nx = col.shape[:2]
    wx = west + (np.arange(nx) + 0.5) * MPP
    wz = north - (np.arange(ny) + 0.5) * MPP
    gxi = np.clip(np.floor(wx / PX_M + GRID // 2).astype(int), 0, GRID - 1)
    gyi = np.clip(np.floor(wz / PX_M + GRID // 2).astype(int), 0, GRID - 1)
    m = ex[gyi[:, None], gxi[None, :]].astype(np.float32)
    m = np.clip(ndimage.gaussian_filter(m, 6) * 1.6, 0, 1)
    rng = np.random.default_rng(7)
    n = sum(ndimage.zoom(rng.random((ny // s + 3, nx // s + 3)).astype(np.float32), s, order=3)[:ny, :nx] / (i + 1)
            for i, s in enumerate([320, 120, 40, 12]))
    n = (n - n.min()) / (n.max() - n.min())
    fog = FOG + FOGV * n[..., None]; del n
    img = col * m[..., None] + fog * (1 - m[..., None]); del fog
    edge = (m > 0.35) & (m < 0.55); img[edge] *= 0.75
    return Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))


def write_tiles(im, out):
    nx, ny = im.size
    fogrgb = tuple(int(v * 255 + 0.07 * 127) for v in FOG)
    count = 0
    for z in range(NATIVE_Z, -1, -1):
        s = 2 ** (NATIVE_Z - z)
        lvl = im if s == 1 else im.resize((math.ceil(nx / s), math.ceil(ny / s)), Image.LANCZOS)
        w, h = lvl.size
        for tx in range(math.ceil(w / TILE)):
            for ty in range(math.ceil(h / TILE)):
                t = Image.new('RGB', (TILE, TILE), fogrgb)
                t.paste(lvl.crop((tx * TILE, ty * TILE, min(w, (tx + 1) * TILE), min(h, (ty + 1) * TILE))), (0, 0))
                d = os.path.join(out, 'tiles', str(z), str(tx)); os.makedirs(d, exist_ok=True)
                t.save(os.path.join(d, f'{ty}.webp'), 'WEBP', quality=82, method=6)
                count += 1
    return count


def build(world_dir, dump_bin, out):
    info, ex, pins, objs, bosses, day, digest, mtime = objects.state(world_dir)
    ys, xs = np.nonzero(ex)
    gx0, gx1 = int(xs.min()) - MARGIN, int(xs.max()) + MARGIN + 1
    gy0, gy1 = int(ys.min()) - MARGIN, int(ys.max()) + MARGIN + 1
    west, east = (gx0 - GRID // 2) * PX_M, (gx1 - GRID // 2) * PX_M
    south, north = (gy0 - GRID // 2) * PX_M, (gy1 - GRID // 2) * PX_M
    nx, ny = int((east - west) / MPP), int((north - south) / MPP)
    print(f'rendering {nx}x{ny} px at {MPP} m/px', file=sys.stderr)

    im = fogged(terrain(info, west, north, nx, ny, dump_bin), ex, west, north)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    n = write_tiles(im, out)

    as_of = datetime.datetime.fromtimestamp(mtime, ZoneInfo(TZ)).strftime('%d %b %Y, %H:%M')
    km2 = round(float(ex.sum() * PX_M * PX_M / 1e6), 1)
    data = dict(world=info['name'], west=west, north=north, mpp=MPP, nx=nx, ny=ny, tile=TILE, nativeZ=NATIVE_Z,
                km2=km2, asOf=as_of, day=day, bosses=bosses,
                objects=dict(portals=objs['portals'], ships=objs['ships'], bases=objs['bases'],
                             pieces=objs['pieces'], materials=objs['materials'], graves=objs['graves']),
                clan=objs['clan'],
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
    elif cmd == 'build' and len(sys.argv) == 5:
        build(*sys.argv[2:])
    else:
        sys.exit(__doc__)
