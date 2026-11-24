import tempfile, unittest, concurrent.futures, sqlite3, json, threading, urllib.request
from pathlib import Path
from unittest.mock import patch
import server as s

MARKET={'id':'123','question':'Test event?','description':'Test rules','condition':'0xabc','tokens':{'YES':'111','NO':'222'},'prices':{'YES':.4,'NO':.6},'open':True,'closed':False,'resolution':''}
BOOK={'token':'111','bids':[(.39,100)],'asks':[(.4,10),(.5,100)],'received':'2026-11-24T00:00:00Z','timestamp':'123','hash':'test'}
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old=s.DB; s.DB=Path(self.tmp.name)/'db.sqlite3'
    def tearDown(self): s.DB=self.old; self.tmp.cleanup()
    def task(self):return s.submit({'title':'学习 KV Cache','topic':'KV Cache','work':'独立推导了 GQA 下的缓存显存公式','evidence':'计算结果为 2LHSDB','minutes':40,'requested':20})
    def funded(self):
        t=self.task(); s.credit({'id':t['id'],'amount':20,'reason':'自评确认'}); return t
    def test_mint_and_duplicate(self):
        t=self.funded(); self.assertEqual(s.state()['xp'],20); self.assertEqual(s.state()['balance'],20)
        with self.assertRaises(ValueError): self.task()
        with self.assertRaises(ValueError): s.credit({'id':t['id'],'amount':20,'reason':'duplicate'})
    def test_model_review_and_credit(self):
        t=self.task()
        with patch.object(s,'review',return_value={'recommended':8,'verdict':'adjust','reason':'证据有限','improvements':'补充实验','model':'mock'}):s.review_task(t['id'])
        with self.assertRaises(ValueError):s.credit({'id':t['id'],'amount':12,'reason':''})
        s.credit({'id':t['id'],'amount':8,'reason':''}); self.assertEqual(s.state()['xp'],8)
    def test_concurrent_credit(self):
        t=self.task()
        def f(_):
            try:s.credit({'id':t['id'],'amount':20,'reason':'确认'});return True
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex: self.assertEqual(sum(ex.map(f,range(4))),1)
        self.assertEqual(s.state()['balance'],20)
    def test_poly_fill_and_close(self):
        self.funded()
        with patch.object(s,'market',return_value=MARKET),patch.object(s,'book',return_value=BOOK):
            p=s.open_position({'kind':'POLY','market':'123','outcome':'YES','stake':10})['positions'][0]; self.assertAlmostEqual(p['qty'],22); self.assertEqual(s.state()['balance'],10)
            result=s.settle(p['id']); self.assertAlmostEqual(result['balance'],18.58); self.assertEqual(result['xp'],20)
            with self.assertRaises(ValueError):s.settle(p['id'])
            with self.assertRaises(ValueError):s.open_position({'kind':'POLY','market':'123','outcome':'YES','stake':1})
    def test_no_fill_and_no_fake_data(self):
        self.funded()
        with patch.object(s,'market',return_value=MARKET),patch.object(s,'book',side_effect=OSError('offline')):
            with self.assertRaises(OSError):s.open_position({'kind':'POLY','market':'123','outcome':'YES','stake':10})
        self.assertEqual(s.state()['balance'],20); self.assertFalse(s.state()['positions'])
        with self.assertRaises(ValueError):s.fill_buy({'asks':[(.4,1)]},10)
        with self.assertRaises(ValueError):s.fill_sell({'bids':[]},1)
    def test_resolution(self):
        self.funded()
        with patch.object(s,'market',return_value=MARKET),patch.object(s,'book',return_value=BOOK):p=s.open_position({'kind':'POLY','market':'123','outcome':'YES','stake':4})['positions'][0]
        with patch.object(s,'market',return_value={**MARKET,'open':False,'closed':True,'prices':{'YES':1,'NO':0}}):
            self.assertIsNone(s.settle(p['id'],True)); self.assertEqual(s.state()['balance'],16)
        with patch.object(s,'market',return_value={**MARKET,'open':False,'closed':True,'resolution':'resolved','prices':{'YES':1,'NO':0}}):
            self.assertEqual(s.settle(p['id'],True)['balance'],26)
    def test_replay_hides_future(self):
        self.funded(); r=s.replay('BTC','demo');self.assertEqual(len(r['points']),15)
        p=s.open_position({'kind':'REPLAY','replay':r['id'],'leverage':10,'direction':'short','stake':10})['positions'][0];self.assertNotIn('future',p)
        v=s.settle(p['id']);self.assertGreaterEqual(v['balance'],0);self.assertEqual(v['xp'],20)
    def test_delete_pending_only(self):
        t=self.task(); self.assertEqual(s.delete_task(t['id'])['tasks'],[])
        with self.assertRaises(ValueError):s.delete_task(t['id'])
        t=self.task(); s.credit({'id':t['id'],'amount':5,'reason':'自评确认'})
        with self.assertRaises(ValueError):s.delete_task(t['id'])
        self.assertEqual(len(s.state()['tasks']),1); self.assertEqual(s.state()['balance'],5)
    def test_backup_is_restorable_sqlite(self):
        self.funded(); f=Path(self.tmp.name)/'restore.sqlite3'; f.write_bytes(s.backup())
        c=sqlite3.connect(f)
        try: d=json.loads(c.execute('SELECT data FROM state WHERE id=1').fetchone()[0])
        finally: c.close()
        self.assertEqual(d['balance'],20); self.assertEqual(len(d['tasks']),1)
    def test_http_backup_and_delete(self):
        t=self.task(); srv=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler); port=srv.server_address[1]; old=s.PORT; s.PORT=port
        threading.Thread(target=srv.serve_forever,daemon=True).start()
        try:
            base=f'http://127.0.0.1:{port}'; op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            r=op.open(base+'/api/backup'); self.assertEqual(r.headers['Content-Type'],'application/vnd.sqlite3'); self.assertIn('attachment',r.headers['Content-Disposition']); self.assertTrue(r.read().startswith(b'SQLite format 3'))
            self.assertEqual(json.loads(op.open(base+'/api/state').read())['tasks'][0]['id'],t['id'])
            req=urllib.request.Request(base+'/api/tasks/delete',json.dumps({'id':t['id']}).encode(),{'Content-Type':'application/json'})
            self.assertEqual(json.loads(op.open(req).read())['tasks'],[])
        finally: srv.shutdown(); srv.server_close(); s.PORT=old
    def test_http_manifest_installable(self):
        srv=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler); port=srv.server_address[1]; old=s.PORT; s.PORT=port
        threading.Thread(target=srv.serve_forever,daemon=True).start()
        try:
            base=f'http://127.0.0.1:{port}'; op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            r=op.open(base+'/manifest.webmanifest'); self.assertEqual(r.headers['Content-Type'],'application/manifest+json'); m=json.loads(r.read())
            self.assertEqual((m['display'],m['start_url'],m['scope']),('standalone','/','/')); self.assertTrue(m['name'] and m['short_name'])
            sizes={i['sizes'] for i in m['icons']}; self.assertTrue({'192x192','512x512'}<=sizes)
            for i in m['icons']: self.assertTrue(op.open(base+i['src']).read().startswith(b'\x89PNG'))
            html=op.open(base+'/').read().decode(); self.assertIn('rel="manifest"',html); self.assertIn('id="install"',html)
        finally: srv.shutdown(); srv.server_close(); s.PORT=old
    def test_normalize(self):
        m=s.normalize_market({'id':'123','question':'Test?','outcomes':'["No","Yes"]','clobTokenIds':'["222","111"]','outcomePrices':'["0.6","0.4"]','active':True,'closed':False,'acceptingOrders':True});self.assertEqual(m['tokens']['YES'],'111');self.assertTrue(m['open'])
    def test_source_error_mapping(self):
        import providers as p, socket
        from urllib.error import URLError, HTTPError
        cases=[(URLError(socket.timeout('timed out')),'https://gamma-api.polymarket.com/markets','Polymarket 连接超时','timeout'),
               (TimeoutError('timed out'),'https://api.exchange.coinbase.com/products','Coinbase 连接超时','timeout'),
               (URLError(socket.gaierror(8,'nodename')),'https://clob.polymarket.com/book','Polymarket 域名解析失败','dns'),
               (URLError(ConnectionRefusedError(61,'refused')),'https://api.exchange.coinbase.com/x','Coinbase 无法访问','unreachable'),
               (HTTPError('https://api.twelvedata.com/time_series?apikey=SECRET',429,'Too Many',{},None),'https://api.twelvedata.com/time_series?apikey=SECRET','Twelve Data 请求过于频繁','rate_limited')]
        for exc,url,msg,kind in cases:
            with patch.object(p,'urlopen',side_effect=exc), self.assertRaises(p.SourceError) as cm: p.request(url)
            self.assertTrue(str(cm.exception).startswith(msg),str(cm.exception)); self.assertEqual(cm.exception.kind,kind)
            self.assertNotIn('SECRET',str(cm.exception)); self.assertNotIn('http',str(cm.exception))
        with patch.dict('os.environ',{'TWELVE_DATA_API_KEY':''}), self.assertRaisesRegex(ValueError,'Twelve Data 未配置密钥'): p.history('NVDA')
    def test_source_error_http_502(self):
        import providers as p, socket
        from urllib.error import URLError
        srv=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler); port=srv.server_address[1]; old=s.PORT; s.PORT=port
        threading.Thread(target=srv.serve_forever,daemon=True).start()
        try:
            op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.object(p,'urlopen',side_effect=URLError(socket.timeout('timed out'))):
                with self.assertRaises(urllib.error.HTTPError) as cm: op.open(f'http://127.0.0.1:{port}/api/markets')
            self.assertEqual(cm.exception.code,502); j=json.loads(cm.exception.read())
            self.assertEqual(j['error'],'Polymarket 连接超时，未执行模拟交易。'); self.assertEqual((j['source'],j['reason']),('Polymarket','timeout'))
            self.assertIsInstance(j['proxy'],bool); self.assertEqual(s.state()['positions'],[])
        finally: srv.shutdown(); srv.server_close(); s.PORT=old
if __name__=='__main__':unittest.main(verbosity=2)
