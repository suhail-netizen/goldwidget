"""Daily gold history for the gold price widget.

Run once a day (GitHub Actions, just after midnight Riyadh time). Writes history.json at the repo root:
  - ranges: chart data for 7d (hourly), 30d (4-hourly) and 1y+ (daily, kept and grown over time),
            PAX Gold (PAXG) candles from Binance's public market-data mirror (reachable from US servers).
  - spot:   one real spot price per new quote from gold-api.com, accumulated from the first run onward.

Only finished candles are stored; the pages top up the hours since the last run live.
Standard library only.
"""
import calendar
import json
import os
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'history.json')
KLINES = 'https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT'
SPOT_URL = 'https://api.gold-api.com/price/XAU'
RIYADH_OFFSET_MS = 3 * 3600 * 1000

# id -> (Binance interval, how many candles to request, keep forever?)
RANGES = {
    '7d': ('1h', 168, False),
    '30d': ('4h', 180, False),
    '1y': ('1d', 1000, True),   # daily archive grows past a year; pages show the last 365
}


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'gold-widget-history/1.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load_existing():
    try:
        with open(OUT, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def fetch_range(interval, limit, now_ms):
    rows = get_json(f'{KLINES}&interval={interval}&limit={limit}')
    # [openTime, open, high, low, close, volume, closeTime, ...]; drop the candle still forming
    rows = [r for r in rows if r[6] < now_ms]
    return {
        't': [r[6] + 1 for r in rows],          # end of each candle (= next open time)
        'o': [round(float(r[1]), 2) for r in rows],
        'h': [round(float(r[2]), 2) for r in rows],
        'l': [round(float(r[3]), 2) for r in rows],
        'c': [round(float(r[4]), 2) for r in rows],
    }


def merge_archive(old, new):
    """Keep older daily candles that have rolled out of Binance's window, newest values win."""
    if not old or not old.get('t'):
        return new
    by_t = {t: i for i, t in enumerate(old['t'])}
    merged = {k: list(old[k]) for k in 'tohlc'}
    for i, t in enumerate(new['t']):
        if t in by_t:
            j = by_t[t]
            for k in 'ohlc':
                merged[k][j] = new[k][i]
        else:
            for k in 'tohlc':
                merged[k].append(new[k][i])
    order = sorted(range(len(merged['t'])), key=lambda i: merged['t'][i])
    return {k: [merged[k][i] for i in order] for k in 'tohlc'}


def main():
    now_ms = int(time.time() * 1000)
    old = load_existing()
    out = {'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'source': 'PAXGUSDT (Binance)', 'ranges': {}}

    for rid, (interval, limit, keep) in RANGES.items():
        try:
            data = fetch_range(interval, limit, now_ms)
            if keep:
                data = merge_archive(old.get('ranges', {}).get(rid), data)
            if len(data['t']) < 2:
                raise ValueError('too few candles')
            out['ranges'][rid] = data
            print(f'{rid}: {len(data["t"])} candles')
        except Exception as e:  # keep yesterday's data for this range rather than failing the run
            print(f'{rid}: fetch failed ({e}); keeping previous data')
            if old.get('ranges', {}).get(rid):
                out['ranges'][rid] = old['ranges'][rid]

    spot = list(old.get('spot', []))
    try:
        g = get_json(SPOT_URL)
        price = float(g['price'])
        updated = g.get('updatedAt') or out['generatedAt']
        ts = calendar.timegm(time.strptime(updated[:19], '%Y-%m-%dT%H:%M:%S')) * 1000   # UTC
        riyadh_date = time.strftime('%Y-%m-%d', time.gmtime((ts + RIYADH_OFFSET_MS) / 1000))
        usd = round(price, 2)
        entry = {'date': riyadh_date, 'usd': usd, 'updatedAt': updated}
        # One entry per Riyadh day. The source re-stamps its time on every request, and at weekends it
        # repeats Friday's close, so an unchanged price is not recorded again.
        if not price > 0:
            print('spot: bad price, nothing added')
        elif spot and spot[-1].get('date') == riyadh_date:
            spot[-1] = entry
            print(f'spot: updated today ({riyadh_date}) to {usd}')
        elif spot and spot[-1].get('usd') == usd:
            print(f'spot: unchanged since {spot[-1]["date"]} (market closed), nothing added')
        else:
            spot.append(entry)
            print(f'spot: recorded {usd} for {riyadh_date}')
    except Exception as e:
        print(f'spot: fetch failed ({e})')
    out['spot'] = spot

    if not out['ranges']:
        raise SystemExit('no chart data at all; not writing history.json')
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(out, f, separators=(',', ':'))
    print(f'wrote {OUT} ({os.path.getsize(OUT)} bytes)')


if __name__ == '__main__':
    main()
