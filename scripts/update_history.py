"""Gold history for the gold price widget (spot prices).

Two modes, both run by GitHub Actions (.github/workflows/history.yml):

  hourly  - record the current spot price (gold-api.com /price, free, no key) into `spotHourly`,
            then rebuild the 7-day and 30-day charts.
  daily   - everything `hourly` does, plus fetch daily spot history (gold-api.com /history, needs the
            free API key in the GOLD_API_KEY secret) for the 30-day and 1-year charts.

Charts written to history.json -> ranges (each {t, o, h, l, c, src}):
  7d   hourly spot from our own record; hours before the record began are filled with PAX Gold (PAXG)
       candles from Binance until the record covers a full week (src 'spot' or 'mixed').
  30d  every 4 hours from our own record once it covers 30 days; until then daily spot history.
  1y   daily spot history (average of the day; h/l = the day's high/low), kept and grown over time.
If spot data is not available at all, a range falls back to PAXG candles (src 'paxg'), as before.

Times are the END of each period in ms (UTC). Only finished periods are stored; the pages add the live price.
Standard library only.
"""
import calendar
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'history.json')
KLINES = 'https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT'
SPOT_URL = 'https://api.gold-api.com/price/XAU'
HISTORY_API = 'https://api.gold-api.com/history'
API_KEY = os.environ.get('GOLD_API_KEY', '').strip()
HOUR = 3600 * 1000
DAY = 24 * HOUR
RIYADH_OFFSET_MS = 3 * HOUR
HOURLY_KEEP = 35 * DAY          # own hourly record kept for 35 days
FIRST_DAILY_DAYS = 400          # first daily-history fetch reaches this far back


def get_json(url, key=None):
    headers = {'User-Agent': 'gold-widget-history/2.0'}
    if key:
        headers['x-api-key'] = key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load_existing():
    try:
        with open(OUT, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def series(rows):
    """rows: list of (t, o, h, l, c) -> column dict."""
    rows = sorted(rows)
    return {k: [r[i] for r in rows] for i, k in enumerate('tohlc')}


def rows_of(s):
    return list(zip(*(s[k] for k in 'tohlc'))) if s and s.get('t') else []


def paxg(interval, limit, now_ms):
    k = get_json(f'{KLINES}&interval={interval}&limit={limit}')
    # [openTime, open, high, low, close, volume, closeTime, ...]; drop the candle still forming
    return [(r[6] + 1, round(float(r[1]), 2), round(float(r[2]), 2), round(float(r[3]), 2), round(float(r[4]), 2))
            for r in k if r[6] < now_ms]


def record_hourly(old, now_ms):
    """Append the current spot price to our own hourly record (skip if the market is closed / unchanged)."""
    rec = old.get('spotHourly') or {'t': [], 'p': []}
    t, p = list(rec['t']), list(rec['p'])
    try:
        g = get_json(SPOT_URL)
        price = round(float(g['price']), 2)
        if not price > 0:
            raise ValueError('bad price')
        slot = now_ms // HOUR * HOUR                        # the hour this run belongs to (never in the future)
        if t and t[-1] == slot:
            p[-1] = price
            print(f'hourly: updated {slot} to {price}')
        elif p and p[-1] == price:
            print(f'hourly: unchanged at {price} (market closed), nothing added')
        else:
            t.append(slot)
            p.append(price)
            print(f'hourly: recorded {price}')
    except Exception as e:
        print(f'hourly: spot fetch failed ({e})')
    keep = [i for i, x in enumerate(t) if x > now_ms - HOURLY_KEEP]
    return {'t': [t[i] for i in keep], 'p': [p[i] for i in keep]}


def fetch_daily(old_daily, now_ms):
    """Daily spot history from gold-api (needs the key): average, high and low of each UTC day."""
    last = old_daily['t'][-1] if old_daily and old_daily.get('t') else 0
    start = (last - 5 * DAY) if last else now_ms - FIRST_DAILY_DAYS * DAY
    base = f'{HISTORY_API}?symbol=XAU&startTimestamp={start // 1000}&endTimestamp={now_ms // 1000}&groupBy=day&orderBy=asc'
    days = {}
    for agg in ('avg', 'max', 'min'):                    # 3 of the 10 requests per hour the free plan allows
        for row in get_json(f'{base}&aggregation={agg}', API_KEY):
            d = row.get('day')
            v = row.get(f'{agg}_price')
            if d and v:
                days.setdefault(str(d)[:10], {})[agg] = float(v)
        time.sleep(2)
    today = time.strftime('%Y-%m-%d', time.gmtime(now_ms / 1000))
    new = []
    for d, v in days.items():
        if d >= today or 'avg' not in v:                  # today's UTC day is not finished yet
            continue
        end = calendar.timegm(time.strptime(d, '%Y-%m-%d')) * 1000 + DAY
        a = round(v['avg'], 2)
        new.append((end, a, round(v.get('max', a), 2), round(v.get('min', a), 2), a))
    if not new:
        raise ValueError('no daily rows returned')
    merged = {r[0]: r for r in rows_of(old_daily)}
    merged.update({r[0]: r for r in new})              # newest values win
    print(f'daily: {len(new)} days fetched, {len(merged)} kept')
    return series(merged.values())


def bucket(rec, size_ms, since):
    """Group the hourly record into periods of size_ms (o/h/l/c), periods ending after `since`."""
    groups = {}
    for t, p in zip(rec['t'], rec['p']):
        if t <= since:
            continue
        end = -(-t // size_ms) * size_ms                 # end of the period this hour belongs to
        groups.setdefault(end, []).append(p)
    return [(e, v[0], max(v), min(v), v[-1]) for e, v in sorted(groups.items())]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'daily'
    now_ms = int(time.time() * 1000)
    old = load_existing()
    old_ranges = old.get('ranges', {})
    out = {'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'source': 'gold-api.com spot (XAU); PAXGUSDT (Binance) only where spot is missing', 'ranges': {}}

    rec = record_hourly(old, now_ms)
    out['spotHourly'] = rec
    covered = now_ms - rec['t'][0] if rec['t'] else 0

    # 1y: daily spot history (daily mode refreshes it; hourly mode keeps the stored one).
    daily = old_ranges.get('1y') if old_ranges.get('1y', {}).get('src') == 'spot' else None
    if mode == 'daily':
        if API_KEY:
            try:
                daily = fetch_daily(daily, now_ms)
                daily['src'] = 'spot'
            except Exception as e:
                print(f'daily: spot history failed ({e}); keeping previous data')
        else:
            print('daily: no GOLD_API_KEY, spot history skipped')
    if daily and len(daily['t']) > 1:
        out['ranges']['1y'] = daily
    else:
        try:
            out['ranges']['1y'] = dict(series(paxg('1d', 1000, now_ms)), src='paxg')
        except Exception as e:
            print(f'1y: PAXG failed ({e})')
            if old_ranges.get('1y'):
                out['ranges']['1y'] = old_ranges['1y']

    # 30d: 4-hourly from our record once it covers 30 days, otherwise the last 30 days of daily spot.
    if covered >= 30 * DAY:
        out['ranges']['30d'] = dict(series(bucket(rec, 4 * HOUR, now_ms - 30 * DAY)), src='spot')
    elif daily and len(daily['t']) > 1:
        rows = [r for r in rows_of(daily) if r[0] > now_ms - 31 * DAY]
        out['ranges']['30d'] = dict(series(rows), src='spot')
    else:
        try:
            out['ranges']['30d'] = dict(series(paxg('4h', 180, now_ms)), src='paxg')
        except Exception as e:
            print(f'30d: PAXG failed ({e})')
            if old_ranges.get('30d'):
                out['ranges']['30d'] = old_ranges['30d']

    # 7d: hourly spot from our record; PAXG hours before the record began, until it covers a week.
    spot7 = [(t, p, p, p, p) for t, p in zip(rec['t'], rec['p']) if t > now_ms - 7 * DAY]
    if covered >= 7 * DAY:
        out['ranges']['7d'] = dict(series(spot7), src='spot')
    else:
        try:
            first = spot7[0][0] if spot7 else now_ms
            fill = [r for r in paxg('1h', 168, now_ms) if r[0] < first]
            out['ranges']['7d'] = dict(series(fill + spot7), src='mixed' if spot7 else 'paxg')
        except Exception as e:
            print(f'7d: PAXG failed ({e})')
            if len(spot7) > 1:
                out['ranges']['7d'] = dict(series(spot7), src='spot')
            elif old_ranges.get('7d'):
                out['ranges']['7d'] = old_ranges['7d']

    for rid, s in out['ranges'].items():
        print(f'{rid}: {len(s["t"])} points ({s.get("src")})')
    if not out['ranges']:
        raise SystemExit('no chart data at all; not writing history.json')
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        # one value per line keeps the hourly commits small (git stores line changes)
        json.dump(out, f, indent=0, separators=(',', ':'))
        f.write('\n')
    print(f'wrote {OUT} ({os.path.getsize(OUT)} bytes)')


if __name__ == '__main__':
    main()
