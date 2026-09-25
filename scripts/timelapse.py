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
