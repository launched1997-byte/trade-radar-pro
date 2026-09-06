import json, math, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

TZ = timezone(timedelta(hours=8))
OUT = Path('data/market.json')
BASE = 'https://push2.eastmoney.com/api/qt/clist/get'
HIST = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
FIELDS = 'f2,f3,f5,f6,f8,f12,f14,f15,f16,f17,f18'
HEADERS = {'User-Agent':'Mozilla/5.0'}


def get_json(url, params, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=12)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == retries - 1: raise
            time.sleep(1.2 * (i + 1))


def stocks():
    p = {'pn':1,'pz':6000,'po':1,'np':1,'fltt':2,'invt':2,'fid':'f3','fs':'m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23','fields':FIELDS}
    j = get_json(BASE,p)
    return j.get('data',{}).get('diff',[]) or []


def history(code, n=90):
    if code.startswith(('6','68')): sec='1.'+code
    elif code.startswith(('0','3')): sec='0.'+code
    else: return []
    p={'secid':sec,'klt':101,'fqt':1,'beg':0,'end':20500101,'lmt':n,'fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'}
    try:
        d=get_json(HIST,p).get('data') or {}
        out=[]
        for x in d.get('klines') or []:
            z=x.split(',')
            if len(z)>=7:
                out.append({'date':z[0],'open':float(z[1]),'close':float(z[2]),'high':float(z[3]),'low':float(z[4]),'volume':float(z[5]),'amount':float(z[6])})
        return out[-n:]
    except Exception:
        return []


def ma(vals,n): return sum(vals[-n:])/n if len(vals)>=n else None


def score_and_tags(s,k):
    closes=[x['close'] for x in k]; vols=[x['volume'] for x in k]
    pct=float(s.get('f3') or 0); turnover=float(s.get('f8') or 0); amount=float(s.get('f6') or 0)
    if not closes: return 0,[],{}
    m5,m10,m20=ma(closes,5),ma(closes,10),ma(closes,20)
    v5=ma(vols,5) or 0; v20=ma(vols,20) or 0
    trend=0
    if m5 and m10 and m20:
        trend += 10 if closes[-1]>m5>m10>m20 else 5 if closes[-1]>m20 else 0
        if len(closes)>=25 and m20>ma(closes[:-5],20): trend += 5
    vol=0
    vr=vols[-1]/v20 if v20 else 0
    vol=min(10, max(0, vr*4))
    if v5>v20: vol+=5
    if turnover>=5: vol+=5
    high60=max(closes[-60:]) if len(closes)>=20 else max(closes)
    near=closes[-1]/high60 if high60 else 0
    shape=10 if near>=0.98 else 7 if near>=0.94 else 3
    if closes[-1]>max(closes[-20:-1]) if len(closes)>=21 else False: shape+=8
    strength=min(12,max(0,pct*0.9)) + (5 if turnover>=8 else 0)
    score=int(min(99,max(1,trend+vol+shape+strength)))

    tags=[]
    # Consecutive limit-up approximation using daily returns.
    streak=0
    for i in range(len(k)-1,0,-1):
        prev=k[i-1]['close']; cur=k[i]['close']; r=(cur/prev-1)*100 if prev else 0
        if r>=9.5: streak+=1
        else: break
    if streak>=2: tags.append(f'连板{streak}')
    # Second-wave pattern: prior strong run, pullback, then reclaim short MAs/high.
    if len(k)>=30:
        rs=[(k[i]['close']/k[i-1]['close']-1)*100 for i in range(1,len(k))]
        prior=max(rs[-30:-8]) if len(rs)>=30 else 0
        peak=max(x['high'] for x in k[-30:-8])
        trough=min(x['low'] for x in k[-8:])
        rebound=closes[-1]/trough-1 if trough else 0
        if prior>=8 and peak and trough/peak<0.92 and rebound>=0.08 and m5 and m10 and closes[-1]>m5>m10:
            tags.append('二波启动')
    if m5 and m10 and m20 and closes[-1]>m5>m10>m20 and near>=0.97 and vr>=1.15:
        tags.append('主升趋势')
    return score,tags,{'volume_ratio':round(vr,2),'turnover':round(turnover,2),'near_high':round(near,3)}


def main():
    ss=stocks(); records=[]; limitups=[]; limitdowns=[]
    for s in ss:
        code=str(s.get('f12','')); name=str(s.get('f14',''))
        if not code or any(x in name for x in ['ST','退']): continue
        price=float(s.get('f2') or 0); pct=float(s.get('f3') or 0)
        if price<=0: continue
        if pct>=9.5: limitups.append(s)
        if pct<=-9.5: limitdowns.append(s)
        k=history(code)
        score,tags,meta=score_and_tags(s,k)
        if score>=55 or tags:
            records.append({'code':code,'name':name,'price':price,'pct':pct,'amount':float(s.get('f6') or 0),'turnover':float(s.get('f8') or 0),'score':score,'tags':tags,**meta})
    records.sort(key=lambda x:x['score'],reverse=True)
    now=datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    data={'updated_at':now,'timezone':'Asia/Shanghai','market':{'stock_count':len(ss),'limit_up':len(limitups),'limit_down':len(limitdowns),'avg_pct':round(sum(float(x.get('f3') or 0) for x in ss)/max(1,len(ss)),2)},'radar':{'lianban':[x for x in records if any('连板' in t for t in x['tags'])][:30],'second_wave':[x for x in records if '二波启动' in x['tags']][:30],'main_rise':[x for x in records if '主升趋势' in x['tags']][:30],'trend':records[:50]},'source':'Eastmoney public market endpoints','note':'约5分钟级自动扫描；GitHub Actions 调度存在延迟，不等同交易所逐笔实时行情。'}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(now, 'stocks=',len(ss),'limit_up=',len(limitups),'records=',len(records))

if __name__=='__main__': main()
