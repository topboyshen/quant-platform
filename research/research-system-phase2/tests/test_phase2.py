import json,sys,tempfile,unittest
from datetime import datetime,timedelta,time
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from phase2 import *
TZ=ZoneInfo('Asia/Shanghai')
class Phase2Tests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.db=str(Path(self.t.name)/'phase2.db');root=Path(__file__).resolve().parents[1];proto=root/'research'/'calibration_protocol_v1.json';self.proto=str(proto if proto.exists() else root/'protocol'/'calibration_protocol_v1.json');self.p=json.loads(Path(self.proto).read_text());init(self.db);self.ph=register_protocol(self.db,self.proto)
 def tearDown(self):self.t.cleanup()
 def payload(self,day='2026-08-13',shift=0):
  d=datetime.fromisoformat(day+'T00:00:00');out=[]
  for i in range(25):
   x=(d-timedelta(days=34-i)).date().isoformat();out.append({'day':x,'close':10+i*.01+shift,'volume':10_000_000+i*100_000})
  out[-1]['day']=day
  if day=='2026-08-14':out[-2]['day']='2026-08-13'
  return json.dumps({'result':{'data':out}})
 def fetch(self,good=30,day='2026-08-13'):
  def f(url):
   code=url.split('symbol=')[1].split('&')[0];idx=self.p['universe']['public_symbols'].index(code[2:]+('.SH' if code.startswith('sh') else '.SZ'))
   if idx>=good:raise OSError('source unavailable')
   raw=json.loads(self.payload(day));raw['result']['data'][-1]['close']=str(float(raw['result']['data'][-1]['close'])+(idx-15)*0.45);return json.dumps(raw)
  return f
 def test_calendar_and_before_close_time_travel_gate(self):
  self.assertTrue(trading_day(datetime(2026,8,13).date()));self.assertEqual(next_session(datetime(2026,8,13).date()).isoformat(),'2026-08-14')
  r=run_producer(self.db,self.proto,datetime(2026,8,13,15,19,tzinfo=TZ),self.fetch());self.assertEqual(r['status'],'PENDING')
 def test_coverage_and_no_private_substitution(self):
  r=run_producer(self.db,self.proto,datetime(2026,8,13,15,21,tzinfo=TZ),self.fetch(24));self.assertEqual(r['status'],'BLOCKED');self.assertEqual(r['reason'],'COVERAGE_LT_25')
  with db(self.db) as c:self.assertEqual(c.execute('SELECT count(*) FROM forecast').fetchone()[0],0);self.assertEqual(c.execute('SELECT count(*) FROM exclusion').fetchone()[0],6)
 def test_seal_idempotent_target_and_forward_dates(self):
  r=run_producer(self.db,self.proto,datetime(2026,8,13,15,21,tzinfo=TZ),self.fetch());self.assertEqual(r['status'],'SEALED');self.assertEqual(r['nominal_n'],30);again=run_producer(self.db,self.proto,datetime(2026,8,13,15,30,tzinfo=TZ),self.fetch());self.assertTrue(again['idempotent'])
  with db(self.db) as c:
   x=c.execute('SELECT * FROM forecast').fetchall();self.assertEqual(len(x),30);self.assertTrue(all(parse(y['sealed_at'])<parse(y['target_session']+'T00:00:00+08:00') for y in x));self.assertEqual(len(set(y['correlation_group'] for y in x)),1)
 def test_blocked_batch_retries_after_source_recovery(self):
  first=run_producer(self.db,self.proto,datetime(2026,8,13,15,21,tzinfo=TZ),self.fetch(24));self.assertEqual(first['status'],'BLOCKED')
  second=run_producer(self.db,self.proto,datetime(2026,8,13,15,31,tzinfo=TZ),self.fetch(30));self.assertEqual(second['status'],'SEALED');self.assertEqual(second['batch_id'],first['batch_id'])
 def test_manual_review_capability_is_separate(self):
  r=run_producer(self.db,self.proto,datetime(2026,8,13,15,21,tzinfo=TZ),self.fetch());run_settler(self.db,self.proto,datetime(2026,8,14,15,41,tzinfo=TZ),self.fetch(day='2026-08-14'))
  with db(self.db) as c:fid=c.execute('SELECT forecast_id FROM forecast WHERE batch_id=? LIMIT 1',(r['batch_id'],)).fetchone()[0]
  with self.assertRaises(Phase2Error):approve_outcome(self.db,fid,'forecast_producer','independent review')
  caps=provision_manual_roles(self.db);approve_outcome(self.db,fid,caps['reviewer'],'independent review')
  with db(self.db) as c:self.assertEqual(c.execute('SELECT status FROM outcome WHERE forecast_id=?',(fid,)).fetchone()[0],'APPROVED')
 def test_pending_review_is_not_formal_and_settler_idempotent(self):
  r=run_producer(self.db,self.proto,datetime(2026,8,13,15,21,tzinfo=TZ),self.fetch());self.assertEqual(r['status'],'SEALED')
  s=run_settler(self.db,self.proto,datetime(2026,8,14,15,41,tzinfo=TZ),self.fetch(day='2026-08-14'));self.assertEqual(s['pending_review_created'],30);again=run_settler(self.db,self.proto,datetime(2026,8,14,15,45,tzinfo=TZ),self.fetch(day='2026-08-14'));self.assertEqual(again['pending_review_created'],0)
  st=stats(self.db);self.assertEqual(st['pending_review_n'],30);self.assertEqual(st['formal_n'],0);self.assertEqual(st['formal_calibration']['status'],'INSUFFICIENT_SAMPLE')
 def test_stable_requires_approved_batches_forecasts_and_20_per_bin(self):
  with db(self.db) as c:
   for i in range(300):
    bid='b'+str(i//30);c.execute('INSERT OR IGNORE INTO batch(batch_id,cohort_id,protocol_hash,data_cutoff,target_session,status,expected_n,nominal_n,qualified_n,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(bid,bid,self.ph,'2026-01-01','2026-01-02','SEALED',30,30,30,iso()))
    fid='f'+str(i);c.execute('INSERT INTO forecast VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(fid,bid,bid,f'{i%30:06d}.SH','2026-01-02','2026-01-01',iso(),iso(),iso(),.15+(i%10)*.07,'heuristic','h'+str(i),json.dumps(['s']),bid,'SEALED'));c.execute('INSERT INTO outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('o'+str(i),fid,11,10,i%2,'https://example.com','2026-01-02','a'*64,iso(),'APPROVED','reviewer',iso(),'reviewer','h'))
  st=stats(self.db);self.assertEqual(st['formal_n'],300);self.assertEqual(st['independent_batch_count'],10);self.assertIn(st['calibration_state'],('STABLE','INSUFFICIENT_SAMPLE'))
if __name__=='__main__':unittest.main()
