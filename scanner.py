import concurrent.futures
import json, math, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

TZ = timezone(timedelta(hours=8))
OUT = Path('data/market.json')
BASES = [
    'https://push2.eastmoney.com/api/qt/clist/get',
    'https://82.push2.eastmoney.com/api/qt/clist/get',
]
HIST_BASES = [
    'https://push2his.eastmoney.com/api/qt/stock/kline/get',
    'https://82.push2.eastmoney.com/api/qt/stock/kline/get',
]
FIELDS = 'f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18'
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36', 'Referer': 'https://quote.eastmoney.com/'}


def get_json(urls, params, retries=2, timeout=10):
    last = None
    for url in urls:
        for i in range(retries):
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
                r.raise_for_status()
                j = r.json()
                if isinstance(j, dict):
                    return j
            except Exception as e:
                last = e
                time.sleep(0.8 * (i + 1))
    raise last or RuntimeError('data source unavailable')


def stocks():
    """Paginate the universe. Smaller requests are much more reliable than one 6000-row request."""
    out = []
    page_size = 1000
    for page in range(1, 8):
        p = {'pn': page, 'pz': page_size, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
             'fid': 'f3', 'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23', 'fields': FIELDS}
        try:
            j = get_json(BASES, p, retries=2, timeout=8)
            rows = (j.get('data') or {}).get('diff') or []
            if not rows:
                break
            out.extend(rows)
            total = int((j.get('data') or {}).get('total') or 0)
            if len(out) >= total or len(rows) < page_size:
                break
        except Exception as e:
            print('universe page', page, 'failed:', repr(e))
            # If at least one page was obtained, keep the partial universe rather than killing the run.
            if not out:
                raise
            break
    # Deduplicate by code.
    return list({str(x.get('f12')): x for x in out if x.get('f12')}.values())


def history(code, n=90):
    if code.startswith(('6', '68')): sec = '1.' + code
    elif code.startswith(('0', '3')): sec = '0.' + code
    else: return []
    p = {'secid': sec, 'klt': 101, 'fqt': 1, 'beg': 0, 'end': 20500101, 'lmt': n,
         'fields1': 'f1,f2,f3,f4,f5,f6',
         'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'}
    try:
        d = get_json(HIST_BASES, p, retries=2, timeout=8).get('data') or {}
        out = []
        for x in d.get('klines') or []:
            z = x.split(',')
            if len(z) >= 7:
                out.append({'date': z[0], 'open': float(z[1]), 'close': float(z[2]),
                            'high': float(z[3]), 'low': float(z[4]), 'volume': float(z[5]), 'amount': float(z[6])})
        return out[-n:]
    except Exception:
        return []


def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def score_and_tags(s, k):
    closes = [x['close'] for x in k]
    vols = [x['volume'] for x in k]
    pct = float(s.get('f3') or 0)
    turnover = float(s.get('f8') or 0)
    if not closes:
        return 0, [], {}
    m5, m10, m20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    v20 = ma(vols, 20) or 0
    trend = 0
    if m5 and m10 and m20:
        trend += 10 if closes[-1] > m5 > m10 > m20 else 5 if closes[-1] > m20 else 0
        if len(closes) >= 25 and m20 > ma(closes[:-5], 20): trend += 5
    vr = vols[-1] / v20 if v20 else 0
    vol = min(10, max(0, vr * 4)) + (5 if ma(vols, 5) > v20 else 0) + (5 if turnover >= 5 else 0)
    high60 = max(closes[-60:]) if len(closes) >= 20 else max(closes)
    near = closes[-1] / high60 if high60 else 0
    shape = 10 if near >= 0.98 else 7 if near >= 0.94 else 3
    if len(closes) >= 21 and closes[-1] > max(closes[-20:-1]): shape += 8
    strength = min(12, max(0, pct * 0.9)) + (5 if turnover >= 8 else 0)
    score = int(min(99, max(1, trend + vol + shape + strength)))

    tags = []
    streak = 0
    for i in range(len(k) - 1, 0, -1):
        prev, cur = k[i - 1]['close'], k[i]['close']
        r = (cur / prev - 1) * 100 if prev else 0
        if r >= 9.5: streak += 1
        else: break
    if streak >= 2: tags.append(f'连板{streak}')

    if len(k) >= 30:
        rs = [(k[i]['close'] / k[i - 1]['close'] - 1) * 100 for i in range(1, len(k))]
        prior = max(rs[-30:-8]) if len(rs) >= 30 else 0
        peak = max(x['high'] for x in k[-30:-8])
        trough = min(x['low'] for x in k[-8:])
        rebound = closes[-1] / trough - 1 if trough else 0
        if prior >= 8 and peak and trough / peak < 0.92 and rebound >= 0.08 and m5 and m10 and closes[-1] > m5 > m10:
            tags.append('二波启动')
    if m5 and m10 and m20 and closes[-1] > m5 > m10 > m20 and near >= 0.97 and vr >= 1.15:
        tags.append('主升趋势')
    if near >= 0.98 and vr >= 1.5 and pct >= 3:
        tags.append('放量突破')
    return score, tags, {'volume_ratio': round(vr, 2), 'turnover': round(turnover, 2), 'near_high': round(near, 3)}


def main():
    ss = stocks()
    clean = []
    for s in ss:
        code, name = str(s.get('f12', '')), str(s.get('f14', ''))
        if not code or any(x in name.upper() for x in ['ST', '退']): continue
        price = float(s.get('f2') or 0)
        if price <= 0: continue
        s['_pct'] = float(s.get('f3') or 0)
        s['_turnover'] = float(s.get('f8') or 0)
        s['_amount'] = float(s.get('f6') or 0)
        clean.append(s)

    limitups = [s for s in clean if s['_pct'] >= 9.5]
    limitdowns = [s for s in clean if s['_pct'] <= -9.5]
    avg_pct = sum(s['_pct'] for s in clean) / max(1, len(clean))

    # First-pass filter: only deep-scan active names. This avoids 5000 x 90-day requests.
    candidates = sorted(clean, key=lambda s: (s['_pct'] * 3 + s['_turnover'] + math.log10(max(1, s['_amount']))), reverse=True)[:450]
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(history, str(s['f12']), 90): s for s in candidates}
        for fut in concurrent.futures.as_completed(futs):
            s = futs[fut]
            k = fut.result()
            score, tags, meta = score_and_tags(s, k)
            if score >= 45 or tags:
                records.append({'code': str(s['f12']), 'name': str(s['f14']), 'price': float(s['f2']),
                                'pct': s['_pct'], 'amount': s['_amount'], 'turnover': s['_turnover'],
                                'score': score, 'tags': tags, **meta})
    records.sort(key=lambda x: x['score'], reverse=True)
    now = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    data = {
        'updated_at': now, 'timezone': 'Asia/Shanghai',
        'market': {'stock_count': len(clean), 'limit_up': len(limitups), 'limit_down': len(limitdowns), 'avg_pct': round(avg_pct, 2)},
        'radar': {
            'lianban': [x for x in records if any('连板' in t for t in x['tags'])][:30],
            'second_wave': [x for x in records if '二波启动' in x['tags']][:30],
            'main_rise': [x for x in records if '主升趋势' in x['tags']][:30],
            'trend': records[:60]
        },
        'source': 'Eastmoney public API · paginated snapshot + parallel history',
        'health': {'universe': len(clean), 'deep_scan': len(candidates), 'history_ok': sum(1 for x in records if x.get('near_high') is not None)},
        'note': '无Token版本；先全市场快照，再对活跃候选深扫历史K线。GitHub Actions仅准时调度，不等同交易所逐笔实时行情。'
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(now, 'stocks=', len(clean), 'limit_up=', len(limitups), 'deep_scan=', len(candidates), 'records=', len(records))


if __name__ == '__main__':
    main()
