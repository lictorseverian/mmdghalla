#!/usr/bin/env python3
"""Post the activity feed to a Discord thread through a webhook, and keep a status card up to date.

  discord.py STATE_DIR SITE_DIR [--test]

Env: DISCORD_WEBHOOK (secret), DISCORD_THREAD_ID, SITE_URL.
State: STATE_DIR/discord.json  {"posted_upto": <epoch>, "status_id": "<message id>"}

- Only events newer than the last one posted go out, one message per update.
- The first run just records where the feed is, so no backlog gets dumped into the thread.
- The status card is one message that gets edited in place on every update (edits don't ping anyone).
- Nothing here can fail the build: errors are printed and the next run tries again.
"""
import json, os, sys, time, urllib.error, urllib.request, uuid

BIG = ['boss', 'newbase', 'death', 'saplost', 'sapfull', 'low', 'portal', 'ship', 'charted']
EMOJI = {'boss': '🏆', 'newbase': '🏠', 'death': '💀', 'portal': '🌀', 'ship': '⛵', 'charted': '🧭',
         'low': '⚠️', 'restocked': '📦', 'build': '🔨', 'pins': '📍', 'demolish': '🪓', 'recovered': '⚰️', 'crossed': '✅',
         'sapfull': '🍯', 'saplost': '💔', 'sapnew': '🌱', 'needle': '🔩'}
COLOR = {'boss': 0xE9BB5E, 'death': 0xB8322B, 'low': 0xE07A2E, 'newbase': 0xC08A50, 'portal': 0x7F9CF5,
         'sapfull': 0xD9A23A, 'saplost': 0x8E5BD6}
UA = 'DiscordBot (https://github.com/lictorseverian/mmdghalla, 1.0)'


def call(method, url, payload, files=None):
    """Webhook request. files: [(field, filename, bytes, content_type)]. Returns parsed JSON or None."""
    if files:
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                 f'Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n'.encode()]
        for field, name, data, ctype in files:
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                         f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b'\r\n')
        body = b''.join(parts) + f'--{b}--\r\n'.encode()
        headers = {'Content-Type': f'multipart/form-data; boundary={b}'}
    else:
        body = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
    headers['User-Agent'] = UA
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read().decode() or 'null'
                return json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code == 429:                                   # rate limited: wait as told, then retry
                wait = float(json.loads(e.read() or b'{}').get('retry_after', 2))
                time.sleep(min(wait, 30)); continue
            raise RuntimeError(f'{method} failed: HTTP {e.code} {e.read()[:300]!r}') from None
    raise RuntimeError(f'{method} failed after retries')


def slug(name):
    import re
    return re.sub(r'[^a-z0-9]+', '-', ' '.join(name.lower().split())).strip('-')


def summary(events, site, site_dir):
    """One embed for one update's events: the big ones as lines, the small ones rolled up."""
    big = [e for e in events if e['kind'] in BIG]
    small = [e for e in events if e['kind'] not in BIG]
    if not big:
        return None
    lines = [f"{EMOJI.get(e['kind'], '•')} {e['text']}" for e in big]
    built = {}
    for e in small:
        if e['kind'] == 'build':
            built[e.get('who', 'Someone')] = built.get(e.get('who', 'Someone'), 0) + e.get('n', 0)
    pins = sum(len(e.get('marks', [])) or 1 for e in small if e['kind'] == 'pins')
    extra = [f"{w} placed {n} pieces" for w, n in sorted(built.items(), key=lambda kv: -kv[1])]
    if pins:
        extra.append(f"{pins} new pin{'s' if pins != 1 else ''}")
    extra += [f"📦 {e['text']}" for e in small if e['kind'] == 'restocked']
    extra += [f"{EMOJI[e['kind']]} {e['text']}" for e in small if e['kind'] in ('sapnew', 'needle')]
    desc = '\n'.join(lines)
    if extra:
        desc += '\n\n*Also: ' + ', '.join(extra) + '*'
    top = min(big, key=lambda e: BIG.index(e['kind']))
    embed = dict(title=f"Day {events[0]['day']}", url=site, description=desc[:4000],
                 color=COLOR.get(top['kind'], 0x5EC7D6), timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(events[0]['t'])))
    who = next((e['who'] for e in big if e.get('who')), None)
    if who and os.path.exists(os.path.join(site_dir, 'portraits', slug(who) + '.webp')):
        embed['thumbnail'] = dict(url=f"{site}portraits/{slug(who)}.webp")
    return embed


def status_card(stats, feed, site):
    bosses = ' · '.join(('✅ ' if b['done'] else '⬜ ') + b['name'] for b in stats.get('boss_track', []))
    latest = [f"{EMOJI.get(e['kind'], '•')} {e['text']}" for e in feed['events'] if e['kind'] in BIG][:4]
    fields = [dict(name='Bosses', value=bosses or '—', inline=False),
              dict(name='Charted', value=f"{stats['km2']} km²", inline=True),
              dict(name='Bases', value=str(stats.get('bases', 0)), inline=True),
              dict(name='Portals', value=str(stats.get('portals', 0)), inline=True),
              dict(name='Pins', value=str(stats.get('pins', 0)), inline=True),
              dict(name='Unrecovered graves', value=str(stats.get('graves', 0)), inline=True)]
    sap = stats.get('sap') or {}
    if sap.get('extractors') or sap.get('loose') or sap.get('spare'):
        bits = [f"{sap['extractors']} extractor{'s' if sap['extractors'] != 1 else ''}, **{sap['full']} full**"]
        if sap['full_at']:
            bits[0] += f" ({', '.join(sap['full_at'][:4])})"
        if sap.get('loose'):
            bits.append(f"🔩 **{sap['loose']} loose Dvergr extractor{'s' if sap['loose'] != 1 else ''}** on the ground")
        if sap.get('spare'):
            bits.append(f"{sap['spare']} spare in chests")
        fields.append(dict(name='🍯 Sap', value='\n'.join(bits)[:1000], inline=False))
    for b in stats.get('low', []):
        rows = ' · '.join(f'{n} **{c}**/{t}' for n, c, t in b['items'])
        fields.append(dict(name=f"⚠️ Running low at {b['base']}", value=rows[:1000], inline=False))
    if latest:
        fields.append(dict(name='Latest', value='\n'.join(latest)[:1000], inline=False))
    return dict(title=f"🗺️ {stats['world']} · the known world", url=site,
                description=f"Day **{stats['day']}** · updated <t:{int(time.time())}:R> · [open the map]({site})",
                color=0xE9BB5E, fields=fields, image=dict(url='attachment://map.png'),
                footer=dict(text='Updates hourly from the world save'))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    test = '--test' in sys.argv
    state_dir, site_dir = args
    hook, thread = os.environ.get('DISCORD_WEBHOOK', '').strip(), os.environ.get('DISCORD_THREAD_ID', '').strip()
    site = os.environ.get('SITE_URL', '').strip()
    # DISCORD_SAP_PING: a role id (pinged as <@&id>) or 'user:<id>'; empty = no ping, just the embed
    sp_raw = os.environ.get('DISCORD_SAP_PING', '').strip()
    ping_user = sp_raw[5:] if sp_raw.startswith('user:') else ''
    ping_role = '' if ping_user else sp_raw
    ping = f'<@{ping_user}>' if ping_user else f'<@&{ping_role}>' if ping_role else ''
    if not hook:
        print('discord: no DISCORD_WEBHOOK secret set, skipping'); return
    q = f'?wait=true&thread_id={thread}' if thread else '?wait=true'
    qe = f'?thread_id={thread}' if thread else ''
    sp = os.path.join(state_dir, 'discord.json')
    st = json.load(open(sp)) if os.path.exists(sp) else {}
    feed = json.load(open(os.path.join(state_dir, 'feed.json')))
    stats = json.load(open(os.path.join(site_dir, 'stats.json')))
    events = feed['events']

    if 'posted_upto' not in st:                          # first run: start from here, no backlog
        st['posted_upto'] = max((e['t'] for e in events), default=0)
        print('discord: first run, starting from the current feed')
    new = sorted([e for e in events if e['t'] > st['posted_upto']], key=lambda e: e['t'])
    by_t = {}
    for e in new:
        by_t.setdefault(e['t'], []).append(e)
    try:
        if test:
            call('POST', hook + q, dict(content=f"🗺️ The {stats['world']} map is connected. "
                                                 f"Big moments from the world will show up here. {site}"))
        for t in sorted(by_t):
            evs = sorted(by_t[t], key=lambda e: [k for k in BIG + list(EMOJI)].index(e['kind']) if e['kind'] in EMOJI else 99)
            emb = summary(evs, site, site_dir)
            if emb:
                body = dict(embeds=[emb])
                if ping and any(e['kind'] in ('sapfull', 'saplost') for e in evs):
                    body.update(content=ping, allowed_mentions=dict(parse=[], roles=[ping_role] if ping_role else [],
                                                                   users=[ping_user] if ping_user else []))
                call('POST', hook + q, body)
            st['posted_upto'] = t
        card = status_card(stats, feed, site)
        png = open(os.path.join(site_dir, 'preview.png'), 'rb').read()
        files = [('files[0]', 'map.png', png, 'image/png')]
        payload = dict(embeds=[card], attachments=[dict(id=0, filename='map.png')])
        done = False
        if st.get('status_id'):
            try:
                call('PATCH', f"{hook}/messages/{st['status_id']}{qe}", payload, files)
                done = True
            except RuntimeError as e:
                print(f'discord: could not edit the status card ({e}); posting a new one')
        if not done:
            m = call('POST', hook + q, payload, files)
            st['status_id'] = m['id']
            print('discord: posted a new status card; pin it in the thread if you like')
        print(f'discord: {len(by_t)} update(s) posted, status card refreshed')
    except Exception as e:
        print(f'discord: {e}', file=sys.stderr)
    finally:
        json.dump(st, open(sp, 'w'))


if __name__ == '__main__':
    main()
