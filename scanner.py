import concurrent.futures
import json, math, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

TZ = timezone(timedelta(hours=8))
OUT = Path('data/market.json')
EM_BASES = ['https://push2.eastmoney.com/api/qt/clist/get','https://82.push2.eastmoney.com/api/qt/clist/get']
EM_HIST = ['https://push2his.eastmoney.com/api/qt/stock/kline/get','https://82.push2his.eastmoney.com/api/qt/stock/kline/get']
HEADERS = {'User-Agent':'Mozilla/5.0','Referer':'https://quote.eastmoney.com/'}


def get_json(urls, params, timeout=10):
    last = None
    for url in urls:
        for n in range(2):
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e; time.sleep(0.8*(n+1))
    raise last or RuntimeError('source unavailable')


def eastmoney_snapshot():
    fields='f2,f3,f5,f6,f8,f12,f14'
    out=[]
    for page in range(1,8):
        p={'pn':page,'pz':1000,'po':1,'np':1,'fltt':2,'invt':2,'fid':'f3','fs':'m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23','fields':fields}
        j=get_json(EM_BASES,p,8); d=j.get('data') or {}; rows=d.get('diff') or []
        out.extend(rows)
        if not rows or len(out)>=int(d.get('total') or 0): break
    return [normalize_east(x) for x in out if x.get('f12')]


def normalize_east(x):
    return {'code':str(x.get('f12','')),'name':str(x.get('f14','')),'price':float(x.get('f2') or 0),'pct':float(x.get('f3') or 0),'amount':float(x.get('f6') or 0),'turnover':float(x.get('f8') or 0)}


def tencent_snapshot():
    # Tencent endpoint is used as a genuine independent fallback. Chunking avoids oversized URLs.
    codes=[]
    for prefix, start, end in [('sh',600000,604000),('sz',1,4000)]:
        # We cannot discover the complete universe without another source, so query the common A-share ranges.
        if prefix=='sh': codes += [f'sh{i:06d}' for i in range(start,end) if str(i).startswith('60')]
        else: codes += [f'sz{i:06d}' for i in range(start,end)]
    out=[]
    for i in range(0,len(codes),500):
        q=','.join(codes[i:i+500])
        try:
            text=requests.get('https://qt.gtimg.cn/q='+q,headers={'User-Agent':'Mozilla/5.0'},timeout=12).text
            for line in text.split(';'):
                if '~' not in line: continue
                parts=line.split('=')
                if len(parts)<2: continue
                sym=parts[0].split('v_')[-1].strip(); z=parts[1].strip().strip('"').split('~')
                if len(z)<38: continue
                code=sym[-6:]; name=z[1] if len(z)>1 else code
                try:
                    price=float(z[3] or 0); pct=float(z[32] or 0); amount=float(z[37] or 0)
                except Exception: continue
                if price>0 and code:
                    out.append({'code':code,'name':name,'price':price,'pct':pct,'amount':amount,'turnover':0.0})
        except Exception as e:
            print('tencent chunk failed',i,repr(e))
    return list({x['code']:x for x in out}.values())


def history(code,n=90):
    sec=('1.' if code.startswith(('6','68')) else '0.')+code
    p={'secid':sec,'klt':101,'fqt':1,'beg':0,'end':20500101,'lmt':n,'fields1':'f1,f2,f3,f4,f5,f6','fields2':'f51,f52,f53,f54,f55,f56,f57'}
    try:
        d=get_json(EM_HIST,p,8).get('data') or {}; out=[]
        for x in d.get('klines') or []:
            z=x.split(',')
            if len(z)>=6: out.append({'close':float(z[2]),'high':float(z[3]),'low':float(z[4]),'volume':float(z[5])})
        return out[-n:]
    except Exception: return []


def ma(v,n): return sum(v[-n:])/n if len(v)>=n else None

def score(s,k):
    if not k: return 0,[],{}
    c=[x['close'] for x in k]; v=[x['volume'] for x in k]; m5,m10,m20=ma(c,5),ma(c,10),ma(c,20); v20=ma(v,20) or 0; vr=v[-1]/v20 if v20 else 0
    trend=(15 if m5 and m10 and m20 and c[-1]>m5>m10>m20 else 5 if m20 and c[-1]>m20 else 0)
    near=c[-1]/max(c[-60:]); shape=10 if near>=.98 else 7 if near>=.94 else 3
    if len(c)>=21 and c[-1]>max(c[-20:-1]): shape+=8
    strength=min(15,max(0,s['pct']))+(5 if s['turnover']>=5 else 0)
    vol=min(12,vr*5)+(5 if vr>=1.2 else 0)
    total=int(min(99,max(1,trend+shape+strength+vol)))
    tags=[]; streak=0
    for i in range(len(c)-1,0,-1):
        if c[i]/c[i-1]>=1.095: streak+=1
        else: break
    if streak>=2: tags.append(f'连板{streak}')
    if len(c)>=30:
        peak=max(x['high'] for x in k[-30:-8]); trough=min(x['low'] for x in k[-8:])
        if peak and trough/peak<.92 and c[-1]/trough>=1.08 and m5 and m10 and c[-1]>m5>m10: tags.append('二波启动')
    if m5 and m10 and m20 and c[-1]>m5>m10>m20 and near>=.97 and vr>=1.15: tags.append('主升趋势')
    if near>=.98 and vr>=1.5 and s['pct']>=3: tags.append('放量突破')
    return total,tags,{'volume_ratio':round(vr,2),'near_high':round(near,3)}


def write_data(data):
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    source='Eastmoney'; errors=[]
    try: ss=eastmoney_snapshot()
    except Exception as e:
        errors.append('Eastmoney: '+repr(e)); source='Tencent'
        try: ss=tencent_snapshot()
        except Exception as e: errors.append('Tencent: '+repr(e)); ss=[]
    clean=[x for x in ss if x['price']>0 and x['code'] and 'ST' not in x['name'].upper() and '退' not in x['name']]
    if not clean:
        # Never erase the previous good dataset on a source outage.
        raise RuntimeError('all snapshot sources failed: '+ ' | '.join(errors))
    limitups=[x for x in clean if x['pct']>=9.5]; limitdowns=[x for x in clean if x['pct']<=-9.5]
    candidates=sorted(clean,key=lambda x:x['pct']*3+x['turnover']+math.log10(max(1,x['amount'])),reverse=True)[:250]
    records=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        fs={ex.submit(history,x['code']):x for x in candidates}
        for f in concurrent.futures.as_completed(fs):
            x=fs[f]; k=f.result(); sc,tags,meta=score(x,k)
            if sc>=45 or tags: records.append({**x,'score':sc,'tags':tags,**meta})
    records.sort(key=lambda x:x['score'],reverse=True); now=datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    data={'updated_at':now,'timezone':'Asia/Shanghai','market':{'stock_count':len(clean),'limit_up':len(limitups),'limit_down':len(limitdowns),'avg_pct':round(sum(x['pct'] for x in clean)/len(clean),2)},'radar':{'lianban':[x for x in records if any('连板' in t for t in x['tags'])][:30],'second_wave':[x for x in records if '二波启动' in x['tags']][:30],'main_rise':[x for x in records if '主升趋势' in x['tags']][:30],'trend':records[:60]},'source':source,'health':{'universe':len(clean),'deep_scan':len(candidates),'records':len(records),'errors':errors},'note':'无Token多源版本。快照优先东方财富，失败切换腾讯；失败时保留上一份成功数据。'}
    write_data(data); print(now,source,len(clean),len(records),errors)

if __name__=='__main__': main()
