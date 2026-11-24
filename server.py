"""Single-user local learning ledger; all orders are simulated, no exchange credentials."""
import os,json,time,math,re,sqlite3,uuid,hashlib,random,threading,tempfile,datetime as dt
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from providers import get,history,review,array
ROOT=Path(__file__).resolve().parent
DB=Path(os.getenv('STUDY_DB',str(ROOT/'study.sqlite3')))
PORT=int(os.getenv('STUDY_PORT','8765'))
GAMMA='https://gamma-api.polymarket.com'; CLOB='https://clob.polymarket.com'

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def day(): return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
def uid(): return uuid.uuid4().hex
def rounded(n): return round(n,6)
def require(ok,msg):
    if not ok: raise ValueError(msg)
def initial(): return {'balance':0,'xp':0,'tasks':[],'positions':[],'ledger':[],'replays':{},'lastOpenDay':''}
def connect():
    c=sqlite3.connect(DB,timeout=20); c.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)'); c.execute('INSERT OR IGNORE INTO state VALUES (1,?)',(json.dumps(initial()),)); c.commit(); return c
@contextmanager
def transaction():
    c=connect()
    try:
        c.execute('BEGIN IMMEDIATE'); s=json.loads(c.execute('SELECT data FROM state WHERE id=1').fetchone()[0]); yield s
        c.execute('UPDATE state SET data=? WHERE id=1',(json.dumps(s,ensure_ascii=False,allow_nan=False),)); c.commit()
    except BaseException: c.rollback(); raise
    finally: c.close()
def state():
    c=connect()
    try: return json.loads(c.execute('SELECT data FROM state WHERE id=1').fetchone()[0])
    finally: c.close()
def public(s):
    return {**{k:v for k,v in s.items() if k not in ('replays','positions')},'positions':[{k:v for k,v in p.items() if k!='future'} for p in s['positions']], 'today':day(),'modelReady':bool(os.getenv('LLM_MODEL') and os.getenv('LLM_API_KEY')),'nvdaReady':bool(os.getenv('TWELVE_DATA_API_KEY'))}
def ledger(s,kind,delta,description,ref,xp=0):
    s['balance']=rounded(s['balance']+delta); s['xp']+=xp
    require(s['balance']>=0,'余额不足')
    s['ledger'].insert(0,{'id':uid(),'kind':kind,'sc':rounded(delta),'xp':xp,'description':description,'ref':ref,'time':now(),'balance':s['balance']})
def integer(v,lo,hi):
    require(type(v)==int and lo<=v<=hi,f'请输入 {lo}–{hi} 的整数'); return v
def txt(v,lo=1,hi=8000):
    require(isinstance(v,str) and lo<=len(v.strip())<=hi,'请填写完整任务内容（长度不符合要求）'); return v.strip()
def task_by(s,id):
    t=next((x for x in s['tasks'] if x['id']==id),None); require(t is not None,'任务不存在'); return t

def submit(d):
    t={k:txt(d.get(k),n,h) for k,n,h in [('title',3,180),('topic',1,100),('work',12,8000),('evidence',5,8000)]}
    t.update(minutes=integer(d.get('minutes'),1,1440),requested=integer(d.get('requested'),1,100))
    fp=hashlib.sha256(re.sub(r'\s+','',t['work']+t['evidence']).casefold().encode()).hexdigest()
    with transaction() as s:
        require(not any(x['fingerprint']==fp for x in s['tasks']),'相同成果已申报，请补充新的独立成果')
        t.update(id=uid(),fingerprint=fp,created=now(),credited=None,review=None); s['tasks'].insert(0,t)
    return t

def delete_task(id):
    with transaction() as s:
        t=task_by(s,id); require(t['credited'] is None,'已入账任务不可删除，账本只追加不改写'); s['tasks'].remove(t)
    return public(state())

def backup():
    # Online, consistent copy via the SQLite backup API; safe while the server keeps running.
    src=connect()
    try:
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'backup.sqlite3'; dst=sqlite3.connect(f)
            try: src.backup(dst)
            finally: dst.close()
            return f.read_bytes()
    finally: src.close()

def review_task(id):
    s=state(); t=task_by(s,id); require(t['credited'] is None,'已入账任务不可重审')
    payload={k:t[k] for k in ('title','topic','work','evidence','minutes','requested')}; payload['recentTasks']=[{k:x[k] for k in ('title','topic','credited')} for x in s['tasks'] if x['id']!=id and x['credited'] is not None][:15]
    r=review(payload); r['time']=now()
    with transaction() as s:
        t=task_by(s,id); require(t['credited'] is None,'任务已入账'); t['review']=r
    return r

def credit(d):
    with transaction() as s:
        t=task_by(s,d.get('id')); require(t['credited'] is None,'任务已入账，不可重复发币')
        n=integer(d.get('amount'),0,100); reason=txt(d.get('reason',''),0,1000)
        require(bool(reason) or (t['review'] is not None and n==t['review']['recommended']),'自评或修改模型建议必须填写理由')
        # XP measures cumulative confirmed learning credit, not externally verified mastery.
        t.update(credited=n,xp=n,creditReason=reason,creditedAt=now(),creditType='模型建议后确认' if t['review'] else '自评（未模型审核）')
        ledger(s,'mint',n,t['title'],t['id'],xp=n)
    return public(state())

def market_id(x): require(isinstance(x,str) and re.fullmatch(r'\d{1,24}',x),'无效市场 ID'); return x
def token_id(x): require(isinstance(x,str) and re.fullmatch(r'(?:\d{1,100}|0x[a-fA-F0-9]{1,128})',x),'无效 outcome ID'); return x

def normalize_market(m):
    outcomes=[str(x).upper() for x in array(m['outcomes'])]; require(len(outcomes)==2 and set(outcomes)=={'YES','NO'},'仅支持二元事件')
    ids=array(m.get('positionIds') or m.get('clobTokenIds')); require(len(ids)==2,'缺少 outcome ID')
    tokens={k:token_id(str(v)) for k,v in zip(outcomes,ids)}
    prices=array(m.get('outcomePrices') or '[]'); prices={k:float(v) for k,v in zip(outcomes,prices) if math.isfinite(float(v)) and 0<=float(v)<=1}
    return {'id':market_id(str(m['id'])),'question':str(m['question']),'description':str(m.get('description') or ''),'condition':str(m.get('conditionId') or ''),'tokens':tokens,'prices':prices,'end':m.get('endDate'),'closed':m.get('closed') is True,'open':m.get('active') is True and m.get('closed') is False and m.get('acceptingOrders') is True,'resolution':str(m.get('umaResolutionStatus') or '').lower()}

def markets():
    raw=get(GAMMA+'/markets',{'active':'true','closed':'false','limit':60,'order':'volume24hr','ascending':'false'}); out=[]
    for m in raw:
        try:
            x=normalize_market(m)
            if x['open']: out.append(x)
        except (ValueError,TypeError,KeyError): continue
    return {'markets':out,'received':now()}
def market(id): return normalize_market(get(GAMMA+'/markets/'+market_id(id),{}))
def levels(rows,reverse=False):
    out=[]
    for r in rows:
        p,q=float(r['price']),float(r['size'])
        if math.isfinite(p) and math.isfinite(q) and 0<p<=1 and q>0: out.append((p,q))
    return sorted(out,key=lambda x:x[0],reverse=reverse)
def book(token):
    token=token_id(token); raw=get(CLOB+'/book',{'token_id':token}); require(str(raw.get('asset_id'))==token,'订单簿标的与请求不符')
    bids=levels(raw['bids'],True); asks=levels(raw['asks']); require(not bids or not asks or bids[0][0]<=asks[0][0],'订单簿交叉，暂不模拟成交')
    return {'token':token,'bids':bids,'asks':asks,'received':now(),'timestamp':raw.get('timestamp'),'hash':raw.get('hash')}
def quote(token):
    b=book(token); return {**{k:b[k] for k in ('token','received','timestamp')},'bid':b['bids'][0][0] if b['bids'] else None,'ask':b['asks'][0][0] if b['asks'] else None}
def fill_buy(b,budget):
    rem=budget; qty=0
    for p,q in b['asks']:
        n=min(q,rem/p); qty+=n; rem-=n*p
        if rem<1e-8: break
    require(rem<1e-8 and qty>0,'卖盘深度不足，未模拟成交'); return qty,budget/qty

def fill_sell(b,qty):
    rem=qty; total=0
    for p,q in b['bids']:
        n=min(q,rem); total+=n*p; rem-=n
        if rem<1e-8: break
    require(rem<1e-8,'买盘深度不足，保留仓位，未模拟成交'); return total,total/qty

def replay(asset,mode):
    require(asset in ('BTC','NVDA') and mode in ('demo','history'),'无效回放参数'); k=day()+asset+mode
    old=state()['replays'].get(k)
    if old: return {**{a:b for a,b in old.items() if a!='points'},'points':old['points'][:15]}
    if mode=='history': r=history(asset)
    else:
        rng=random.Random(k); p=100.; pts=[]
        for i in range(30):
            old=p; p*=1+rng.uniform(-.035,.035); pts.append({'t':i,'p':p,'low':min(old,p)*.995,'high':max(old,p)*1.005})
        r={'source':'虚构价格路径 · 非真实行情','points':pts}
    r.update(id=k,asset=asset,mode=mode)
    with transaction() as s:
        s['replays']={a:b for a,b in s['replays'].items() if a.startswith(day())}; r=s['replays'].setdefault(k,r)
    return {**{a:b for a,b in r.items() if a!='points'},'points':r['points'][:15]}

def opening_checks(s,stake):
    require(s['lastOpenDay']!=day(),'每日最多一次模拟交易（北京时间）'); require(not any(p['status']=='open' for p in s['positions']),'已有未结算仓位'); require(s['balance']>=stake,'SC 余额不足，请完成独立学习任务')
def open_position(d):
    stake=integer(d.get('stake'),1,20); opening_checks(state(),stake); kind=d.get('kind'); p={'id':uid(),'kind':kind,'stake':stake,'status':'open','opened':now()}
    if kind=='POLY':
        m=market(d.get('market')); require(m['open'],'事件已停止接受订单'); outcome=d.get('outcome'); require(outcome in ('YES','NO'),'请选择 YES / NO'); token=m['tokens'][outcome]; b=book(token); qty,entry=fill_buy(b,stake)
        p.update(market=m['id'],condition=m['condition'],label=m['question'],outcome=outcome,token=token,qty=qty,entry=entry,entryQuote=b['received'],leverage=1)
    elif kind=='REPLAY':
        r=state()['replays'].get(d.get('replay')); require(r is not None and r['id'].startswith(day()),'请先加载今日回放'); lev=integer(d.get('leverage'),1,10); require(lev in (1,2,5,10),'无效杠杆'); direction=d.get('direction'); require(direction in ('long','short'),'请选择方向'); sign=1 if direction=='long' else -1; entry=r['points'][14]['p']; liq=entry*(1+sign*(-1/lev+.006))
        p.update(label=r['asset']+' · '+r['mode'],entry=entry,leverage=lev,direction=direction,liq=liq,future=r['points'][15:],source=r['source'],replay=r['id'])
    else: raise ValueError('未知模拟交易类型')
    with transaction() as s:
        opening_checks(s,stake); s['lastOpenDay']=day(); s['positions'].insert(0,p); ledger(s,'open',-stake,p['label'],p['id'])
    return public(state())
def open_by(s,id):
    p=next((p for p in s['positions'] if p['id']==id and p['status']=='open'),None); require(p is not None,'仓位不存在或已结算'); return p

def resolved_price(p,m):
    # Never infer settlement from market odds or deadline alone. Require explicit final UMA state + exact binary prices.
    if m['closed'] and m['resolution']=='resolved' and m['tokens'].get(p['outcome'])==p['token']:
        a=m['prices'].get('YES'); b=m['prices'].get('NO')
        if (a,b) in ((1,0),(0,1),(.5,.5)): return m['prices'][p['outcome']]
    return None

def settle(id,auto=False):
    p=open_by(state(),id); mode=''; exit=0
    if p['kind']=='POLY':
        m=market(p['market']); require(m['tokens'].get(p['outcome'])==p['token'],'市场标的变化，需人工核查'); price=resolved_price(p,m)
        if price is not None: exit=price; value=p['qty']*price; mode='官方已确认结算'
        elif auto: return None
        else:
            require(m['open'],'市场已关闭但未确认最终结果，等待结算'); value,exit=fill_sell(book(p['token']),p['qty']); mode='公开买盘模拟平仓'
    else:
        if auto: return None
        sign=1 if p['direction']=='long' else -1; hit=any(x['low']<=p['liq'] if sign==1 else x['high']>=p['liq'] for x in p['future']); exit=p['liq'] if hit else p['future'][-1]['p']; value=0 if hit else max(0,p['stake']*(1+p['leverage']*sign*(exit/p['entry']-1)-p['leverage']*.001)); mode='参考清算' if hit else '回放结算'
    value=rounded(value)
    with transaction() as s:
        x=open_by(s,id); x.update(status='closed',closed=now(),exit=exit,value=value,pnl=rounded(value-x['stake']),settlement=mode); x.pop('future',None); ledger(s,'close',value,x['label']+' · '+mode,id)
    return public(state())
def worker():
    while True:
        try:
            for p in state()['positions']:
                if p['kind']=='POLY' and p['status']=='open':
                    try: settle(p['id'],True)
                    except Exception: pass # fail closed, retry next tick; manual refresh also available
        except Exception: pass
        time.sleep(60)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def response(self,data,status=200,typ='application/json; charset=utf-8',headers=None):
        b=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode(); self.send_response(status); self.send_header('Content-Type',typ); self.send_header('Cache-Control','no-store')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def allowed(self):
        allowed={f'127.0.0.1:{PORT}',f'localhost:{PORT}'}; extra=os.getenv('STUDY_ALLOWED_HOST','');
        if extra: allowed.add(extra)
        host=self.headers.get('Host',''); origin=self.headers.get('Origin'); return host in allowed and (not origin or origin in ('http://'+host,'https://'+host)) and self.headers.get('Sec-Fetch-Site') not in ('cross-site',)
    def do_GET(self):
        if not self.allowed(): return self.response({'error':'Host / Origin not allowed'},403)
        u=urlparse(self.path); q=parse_qs(u.query); param=lambda k,default='':q.get(k,[default])[0]
        try:
            static={'/':('index.html','text/html; charset=utf-8'),'/manifest.webmanifest':('manifest.webmanifest','application/manifest+json'),'/icon.svg':('icon.svg','image/svg+xml'),'/icon-192.png':('icon-192.png','image/png'),'/icon-512.png':('icon-512.png','image/png'),'/sw.js':('sw.js','application/javascript'),'/app.js':('app.js','application/javascript'),'/style.css':('style.css','text/css')}
            if u.path in static:
                fn,typ=static[u.path]; return self.response((ROOT/fn).read_bytes(),typ=typ)
            if u.path=='/api/backup': return self.response(backup(),typ='application/vnd.sqlite3',headers={'Content-Disposition':f'attachment; filename="study-exchange-{day()}.sqlite3"'})
            if u.path in ('/api/state','/api/export'): r=public(state())
            elif u.path=='/api/markets': r=markets()
            elif u.path=='/api/quote': r=quote(param('token'))
            elif u.path=='/api/replay': r=replay(param('asset'),param('mode','demo'))
            else: return self.response({'error':'Not found'},404)
            self.response(r)
        except ValueError as e: self.response({'error':str(e)[:300]},400)
        except Exception: self.response({'error':'数据源或服务不可用，未执行模拟交易。'},502)
    def do_POST(self):
        if not self.allowed() or self.headers.get('Content-Type','').split(';')[0]!='application/json': return self.response({'error':'Origin / Content-Type not allowed'},403)
        try:
            n=int(self.headers.get('Content-Length',0)); require(0<n<=60000,'请求长度无效'); d=json.loads(self.rfile.read(n)); require(isinstance(d,dict),'无效请求')
            funcs={'/api/tasks':submit,'/api/tasks/delete':lambda d:delete_task(d.get('id')),'/api/review':lambda d:review_task(d.get('id')),'/api/credit':credit,'/api/open':open_position,'/api/settle':lambda d:settle(d.get('id'))}
            if self.path not in funcs: return self.response({'error':'Not found'},404)
            self.response(funcs[self.path](d))
        except ValueError as e: self.response({'error':str(e)[:300]},400)
        except Exception: self.response({'error':'服务或上游接口异常；未确认操作，请刷新账本核对。'},502)
if __name__=='__main__':
    connect().close(); threading.Thread(target=worker,daemon=True).start(); print(f'Study Exchange 3.0: http://127.0.0.1:{PORT}',flush=True); ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
