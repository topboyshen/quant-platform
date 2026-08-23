from __future__ import annotations
import hashlib,json,math,sqlite3,uuid
from datetime import datetime,date,time,timedelta,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai');PROTOCOL_SCHEMA='phase2-v1';MIN_COVERAGE=25;STABLE_BATCHES=10;STABLE_FORECASTS=300;STABLE_BIN=20
HOLIDAYS_2026={'2026-01-01','2026-01-02','2026-01-03','2026-02-15','2026-02-16','2026-02-17','2026-02-18','2026-02-19','2026-02-20','2026-02-21','2026-02-22','2026-02-23','2026-04-04','2026-04-05','2026-04-06','2026-05-01','2026-05-02','2026-05-03','2026-05-04','2026-05-05','2026-06-19','2026-06-20','2026-06-21','2026-09-25','2026-09-26','2026-09-27','2026-10-01','2026-10-02','2026-10-03','2026-10-04','2026-10-05','2026-10-06','2026-10-07'}
class Phase2Error(ValueError):pass
def iso(x=None):return (x or datetime.now(TZ)).astimezone(TZ).replace(microsecond=0).isoformat()
def parse(x):return datetime.fromisoformat(x.replace('Z','+00:00'))
def sha(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def uid(p):return p+'-'+uuid.uuid4().hex[:16]
def db(path):
 c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
def trading_day(d):return d.weekday()<5 and d.isoformat() not in HOLIDAYS_2026
def next_session(d):
 x=d+timedelta(days=1)
 while not trading_day(x):x+=timedelta(days=1)
 return x
def local_date(now=None):return (now or datetime.now(TZ)).astimezone(TZ).date()
def cutoff_ready(now=None):
 n=(now or datetime.now(TZ)).astimezone(TZ);return trading_day(n.date()) and n.time()>=time(15,20)
def source_symbol(symbol):return ('sh' if symbol.endswith('.SH') else 'sz')+symbol[:6]
def load_protocol(path):
 x=json.loads(Path(path).read_text());
 if x.get('protocol_version')!='1.0.0' or len(x.get('universe',{}).get('public_symbols',[]))!=30:raise Phase2Error('协议版本或公开 cohort 无效')
 if 'heuristic_baseline_not_trained' not in x.get('model',{}).get('model_status',''):raise Phase2Error('模型定位未冻结为透明 heuristic')
 return x,sha(x)
def init(path):
 Path(path).parent.mkdir(parents=True,exist_ok=True)
 with db(path) as c:c.executescript('''
 CREATE TABLE IF NOT EXISTS protocol(protocol_hash TEXT PRIMARY KEY,protocol_id TEXT NOT NULL,version TEXT NOT NULL,protocol_json TEXT NOT NULL,frozen_at TEXT NOT NULL,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS audit(event_id TEXT PRIMARY KEY,action TEXT NOT NULL,actor TEXT NOT NULL,occurred_at TEXT NOT NULL,payload_hash TEXT NOT NULL,payload_json TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS batch(batch_id TEXT PRIMARY KEY,cohort_id TEXT NOT NULL,protocol_hash TEXT NOT NULL,data_cutoff TEXT NOT NULL,target_session TEXT NOT NULL,status TEXT NOT NULL,expected_n INTEGER NOT NULL,nominal_n INTEGER NOT NULL DEFAULT 0,qualified_n INTEGER NOT NULL DEFAULT 0,independent_batch_count INTEGER NOT NULL DEFAULT 1,block_reason TEXT,created_at TEXT NOT NULL,sealed_at TEXT,UNIQUE(protocol_hash,target_session));
 CREATE TABLE IF NOT EXISTS exclusion(exclusion_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,symbol TEXT NOT NULL,reason TEXT NOT NULL,source_hash TEXT,created_at TEXT NOT NULL,UNIQUE(batch_id,symbol));
 CREATE TABLE IF NOT EXISTS source_snapshot(snapshot_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,symbol TEXT NOT NULL,source_url TEXT NOT NULL,fetched_at TEXT NOT NULL,content_hash TEXT NOT NULL,normalized_hash TEXT NOT NULL,latest_session TEXT NOT NULL,UNIQUE(batch_id,symbol,content_hash));
 CREATE TABLE IF NOT EXISTS forecast(forecast_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,cohort_id TEXT NOT NULL,symbol TEXT NOT NULL,target_session TEXT NOT NULL,data_cutoff TEXT NOT NULL,created_at TEXT NOT NULL,registered_at TEXT NOT NULL,sealed_at TEXT NOT NULL,probability REAL NOT NULL,model_version TEXT NOT NULL,feature_snapshot_hash TEXT NOT NULL,source_hashes_json TEXT NOT NULL,correlation_group TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'SEALED',UNIQUE(batch_id,symbol));
 CREATE TABLE IF NOT EXISTS outcome(outcome_id TEXT PRIMARY KEY,forecast_id TEXT NOT NULL UNIQUE,observed_close REAL NOT NULL,prior_close REAL NOT NULL,outcome INTEGER NOT NULL,source_url TEXT NOT NULL,source_as_of TEXT NOT NULL,content_hash TEXT NOT NULL,settled_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',settler TEXT NOT NULL,reviewed_at TEXT,reviewed_by TEXT,review_hash TEXT);
 CREATE TABLE IF NOT EXISTS run_state(name TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS manual_capability(role_name TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,revoked_at TEXT);
 CREATE TABLE IF NOT EXISTS risk_veto(veto_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,reason TEXT NOT NULL,evidence_hash TEXT NOT NULL,created_at TEXT NOT NULL,reviewer TEXT NOT NULL,overridden_at TEXT,overridden_by TEXT,override_reason TEXT);
 CREATE TABLE IF NOT EXISTS calibration_suggestion(suggestion_id TEXT PRIMARY KEY,protocol_hash TEXT NOT NULL,proposed_change TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL',created_at TEXT NOT NULL,created_by TEXT NOT NULL);
 ''')
def audit(c,action,actor,payload):c.execute('INSERT INTO audit VALUES (?,?,?,?,?,?)',(uid('audit'),action,actor,iso(),sha(payload),json.dumps(payload,ensure_ascii=False,sort_keys=True)))
def token_hash(x):return hashlib.sha256(x.encode()).hexdigest()
def provision_manual_roles(path):
 import secrets
 init(path);out={}
 with db(path) as c:
  for role in ('reviewer','risk_officer','calibration_approver'):
   t=secrets.token_urlsafe(32);out[role]=t;c.execute('INSERT OR REPLACE INTO manual_capability VALUES (?,?,?,NULL)',(role,token_hash(t),iso()))
 return out
def require_manual(c,role,token):
 r=c.execute('SELECT 1 FROM manual_capability WHERE role_name=? AND token_hash=? AND revoked_at IS NULL',(role,token_hash(token))).fetchone()
 if not r:raise Phase2Error('独立人工 capability 无效')
def register_protocol(path,protocol_path):
 init(path);p,h=load_protocol(protocol_path)
 with db(path) as c:
  c.execute('INSERT OR IGNORE INTO protocol VALUES (?,?,?,?,?,?)',(h,p['protocol_id'],p['protocol_version'],json.dumps(p,ensure_ascii=False,sort_keys=True),p['frozen_at'],iso()));audit(c,'PROTOCOL_REGISTER','system',{'protocol_hash':h});return h
def sina_history(symbol,fetcher=None):
 url='https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData?'+urlencode({'symbol':source_symbol(symbol),'scale':'240','ma':'no','datalen':'35'})
 try:raw=(fetcher(url) if fetcher else urlopen(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=20).read().decode())
 except Exception as e:raise Phase2Error('PUBLIC_SOURCE_UNAVAILABLE:'+type(e).__name__)
 try:items=json.loads(raw)['result']['data']
 except Exception:raise Phase2Error('PUBLIC_SOURCE_SCHEMA_INVALID')
 rows=[]
 for x in items:
  try:rows.append({'day':x['day'],'close':float(x['close']),'volume':float(x['volume'])})
  except Exception:continue
 return url,raw,rows
def valid_history(rows,cutoff):
 if len(rows)<20:return 'HISTORY_LT_20'
 if rows[-1]['day']!=cutoff:return 'CUTOFF_NOT_FINAL'
 if any(x['close']<=0 or x['volume']<=0 for x in rows[-20:]):return 'NONPOSITIVE_CLOSE_OR_VOLUME'
 if sum(x['close']*x['volume'] for x in rows[-20:])/20<10_000_000:return 'LIQUIDITY_BELOW_MIN'
 return None
def probability(rows):
 t=rows[-1];r1=t['close']/rows[-2]['close']-1;r5=t['close']/rows[-6]['close']-1;avg=sum(x['volume'] for x in rows[-21:-1])/20;vr=t['volume']/avg
 raw=.65*r5+.35*r1+.15*math.log(vr);p=1/(1+math.exp(-35*raw));p=max(.05,min(.95,p));return p,{'r1':r1,'r5':r5,'volume_ratio':vr,'raw':raw,'probability':p}
def run_producer(path,protocol_path,now=None,fetcher=None):
 init(path);p,ph=load_protocol(protocol_path);register_protocol(path,protocol_path);n=(now or datetime.now(TZ)).astimezone(TZ);cut=n.date().isoformat();target=next_session(n.date()).isoformat()
 with db(path) as c:
  if not trading_day(n.date()):audit(c,'PRODUCER_BLOCKED','forecast_producer',{'reason':'NON_TRADING_DAY','date':cut});return {'status':'BLOCKED','reason':'NON_TRADING_DAY'}
  if n.time()<time(15,20):audit(c,'PRODUCER_PENDING','forecast_producer',{'reason':'AFTER_CLOSE_NOT_REACHED','date':cut});return {'status':'PENDING','reason':'AFTER_CLOSE_NOT_REACHED','next_run':n.replace(hour=15,minute=20,second=0).isoformat()}
  existing=c.execute('SELECT batch_id,status,cohort_id FROM batch WHERE protocol_hash=? AND target_session=?',(ph,target)).fetchone()
  if existing and existing['status']=='SEALED':return {'status':existing['status'],'batch_id':existing['batch_id'],'idempotent':True}
  if existing:
   bid=existing['batch_id'];cohort=existing['cohort_id'];c.execute("UPDATE batch SET status='COLLECTING',block_reason=NULL WHERE batch_id=?",(bid,));audit(c,'BATCH_RETRY','forecast_producer',{'batch_id':bid,'prior_status':existing['status']})
  else:
   bid=uid('batch');cohort=p['protocol_id']+'-'+cut;c.execute('INSERT INTO batch(batch_id,cohort_id,protocol_hash,data_cutoff,target_session,status,expected_n,created_at) VALUES (?,?,?,?,?,?,?,?)',(bid,cohort,ph,cut,target,'COLLECTING',30,iso(n)));audit(c,'BATCH_CREATE','forecast_producer',{'batch_id':bid,'data_cutoff':cut,'target_session':target})
  qualified=[]
  for symbol in p['universe']['public_symbols']:
   try:
    url,raw,rows=sina_history(symbol,fetcher);reason=valid_history(rows,cut)
    norm=sha(rows)
    c.execute('INSERT OR IGNORE INTO source_snapshot VALUES (?,?,?,?,?,?,?,?)',(uid('snap'),bid,symbol,url,iso(n),hashlib.sha256(raw.encode()).hexdigest(),norm,rows[-1]['day'] if rows else ''))
    if reason:raise Phase2Error(reason)
    prob,features=probability(rows);fh=sha({'protocol_hash':ph,'model_version':p['model']['model_version'],'symbol':symbol,'rows':rows[-21:],'features':features});qualified.append((symbol,prob,fh,norm))
   except Phase2Error as e:c.execute('INSERT OR IGNORE INTO exclusion VALUES (?,?,?,?,?,?)',(uid('excl'),bid,symbol,str(e),None,iso(n)))
  if len(qualified)<MIN_COVERAGE:
   c.execute("UPDATE batch SET status='BLOCKED',qualified_n=?,block_reason=? WHERE batch_id=?",(len(qualified),'COVERAGE_LT_25',bid));audit(c,'BATCH_BLOCKED','forecast_producer',{'batch_id':bid,'qualified_n':len(qualified),'reason':'COVERAGE_LT_25'});return {'status':'BLOCKED','batch_id':bid,'qualified_n':len(qualified),'reason':'COVERAGE_LT_25'}
  probs=[x[1] for x in qualified];buckets={int(x/.1) for x in probs}
  mean=sum(probs)/len(probs);std=(sum((x-mean)**2 for x in probs)/len(probs))**.5
  if std<.01 or len(buckets)<3:
   c.execute("UPDATE batch SET status='BLOCKED',qualified_n=?,block_reason=? WHERE batch_id=?",(len(qualified),'PROBABILITY_DISPERSION_INSUFFICIENT',bid));audit(c,'BATCH_BLOCKED','forecast_producer',{'batch_id':bid,'reason':'PROBABILITY_DISPERSION_INSUFFICIENT'});return {'status':'BLOCKED','batch_id':bid,'reason':'PROBABILITY_DISPERSION_INSUFFICIENT'}
  for symbol,prob,fh,snap in qualified:
   fid=uid('forecast');c.execute('INSERT INTO forecast VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(fid,bid,cohort,symbol,target,cut,iso(n),iso(n),iso(n),prob,p['model']['model_version'],fh,json.dumps([snap]),cohort,'SEALED'))
  c.execute("UPDATE batch SET status='SEALED',qualified_n=?,nominal_n=?,sealed_at=? WHERE batch_id=?",(len(qualified),len(qualified),iso(n),bid));audit(c,'BATCH_SEAL','forecast_producer',{'batch_id':bid,'nominal_n':len(qualified),'probability_std':std});return {'status':'SEALED','batch_id':bid,'nominal_n':len(qualified),'target_session':target}
def run_settler(path,protocol_path,now=None,fetcher=None):
 init(path);p,ph=load_protocol(protocol_path);n=(now or datetime.now(TZ)).astimezone(TZ)
 if n.time()<time(15,40):return {'status':'PENDING','reason':'AFTER_CLOSE_NOT_REACHED'}
 made=0;blocked=0
 with db(path) as c:
  fs=c.execute("SELECT f.*,b.data_cutoff FROM forecast f JOIN batch b ON f.batch_id=b.batch_id WHERE f.status='SEALED' AND f.target_session<=? AND NOT EXISTS(SELECT 1 FROM outcome o WHERE o.forecast_id=f.forecast_id)",(n.date().isoformat(),)).fetchall()
  for f in fs:
   try:
    url,raw,rows=sina_history(f['symbol'],fetcher)
    if not rows or rows[-1]['day']!=f['target_session']:raise Phase2Error('TARGET_SESSION_NOT_FINAL')
    prior=[r for r in rows if r['day']==f['data_cutoff']]
    if not prior:raise Phase2Error('PRIOR_SESSION_MISSING')
    close=rows[-1]['close'];prev=prior[-1]['close'];out=int(close>prev);ih=hashlib.sha256(raw.encode()).hexdigest();c.execute('INSERT OR IGNORE INTO outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(uid('outcome'),f['forecast_id'],close,prev,out,url,f['target_session'],ih,iso(n),'PENDING_REVIEW','outcome_settler',None,None,None));audit(c,'OUTCOME_PENDING_REVIEW','outcome_settler',{'forecast_id':f['forecast_id'],'content_hash':ih});made+=1
   except Phase2Error as e:audit(c,'SETTLEMENT_BLOCKED','outcome_settler',{'forecast_id':f['forecast_id'],'reason':str(e)});blocked+=1
 return {'status':'OK','pending_review_created':made,'blocked':blocked}
def approve_outcome(path,forecast_id,reviewer_token,reason):
 if not reason:raise Phase2Error('独立审核理由必填')
 with db(path) as c:
  require_manual(c,'reviewer',reviewer_token);o=c.execute("SELECT * FROM outcome WHERE forecast_id=?",(forecast_id,)).fetchone()
  if not o or o['status']!='PENDING_REVIEW':raise Phase2Error('仅 PENDING_REVIEW 可审核')
  c.execute("UPDATE outcome SET status='APPROVED',reviewed_at=?,reviewed_by=?,review_hash=? WHERE forecast_id=?",(iso(),'reviewer',sha({'reason':reason,'forecast_id':forecast_id}),forecast_id));audit(c,'OUTCOME_APPROVED','reviewer',{'forecast_id':forecast_id});
def veto_batch(path,batch_id,risk_token,reason,evidence_hash):
 if not reason or len(evidence_hash)!=64:raise Phase2Error('风险否决理由或证据 hash 无效')
 with db(path) as c:
  require_manual(c,'risk_officer',risk_token);c.execute("UPDATE batch SET status='VETOED',block_reason='RISK_VETO' WHERE batch_id=?",(batch_id,));c.execute('INSERT INTO risk_veto VALUES (?,?,?,?,?,?,?,?,?)',(uid('veto'),batch_id,reason,evidence_hash,iso(),'risk_officer',None,None,None));audit(c,'RISK_VETO','risk_officer',{'batch_id':batch_id})
def propose_calibration_change(path,protocol_hash,approver_token,change):
 if not change:raise Phase2Error('建议内容不能为空')
 with db(path) as c:
  require_manual(c,'calibration_approver',approver_token);sid=uid('suggestion');c.execute('INSERT INTO calibration_suggestion VALUES (?,?,?,?,?,?)',(sid,protocol_hash,change,'PENDING_HUMAN_APPROVAL',iso(),'calibration_approver'));audit(c,'CALIBRATION_SUGGESTION','calibration_approver',{'suggestion_id':sid});return sid
def bins(rows,min_bin):
 out=[]
 for b in range(10):
  xs=[x for x in rows if min(9,int(x[0]*10))==b]
  if xs:
   mp=sum(x[0] for x in xs)/len(xs);orate=sum(x[1] for x in xs)/len(xs);out.append({'bucket':b,'n':len(xs),'mean_probability':mp,'outcome_rate':orate,'gap':mp-orate,'stable_eligible':len(xs)>=min_bin})
 return out
def stats(path):
 init(path)
 with db(path) as c:
  pend=c.execute("SELECT f.probability,o.outcome FROM forecast f JOIN outcome o ON f.forecast_id=o.forecast_id WHERE o.status='PENDING_REVIEW'").fetchall();formal=c.execute("SELECT f.probability,o.outcome,f.batch_id FROM forecast f JOIN outcome o ON f.forecast_id=o.forecast_id WHERE o.status='APPROVED'").fetchall();batches=c.execute("SELECT count(*) FROM batch WHERE status='SEALED'").fetchone()[0];nominal=c.execute("SELECT COALESCE(sum(nominal_n),0) FROM batch WHERE status='SEALED'").fetchone()[0]
  def calc(rs):
   pairs=[(r['probability'],r['outcome']) for r in rs];return {'n':len(pairs),'brier':sum((p-o)**2 for p,o in pairs)/len(pairs) if pairs else None,'bins':bins(pairs,STABLE_BIN)}
  formal_stat=calc(formal);provisional=calc(pend);ind=len(set(r['batch_id'] for r in formal));stable=ind>=STABLE_BATCHES and len(formal)>=STABLE_FORECASTS and formal_stat['n'] and all(x['stable_eligible'] for x in formal_stat['bins'])
  return {'sealed_batches':batches,'nominal_forecast_count':nominal,'independent_sealed_batch_count':batches,'pending_review_n':len(pend),'formal_n':len(formal),'independent_batch_count':ind,'operational_provisional':provisional,'formal_calibration':formal_stat if stable else {'status':'INSUFFICIENT_SAMPLE','n':len(formal),'reason':'requires_10_batches_300_approved_and_20_per_displayed_bin'},'calibration_state':'STABLE' if stable else 'INSUFFICIENT_SAMPLE'}
def dashboard(path,protocol_path):
 p,h=load_protocol(protocol_path);s=stats(path);n=datetime.now(TZ);today=n.date();next_run=n.replace(hour=15,minute=20,second=0,microsecond=0);settle_run=n.replace(hour=15,minute=40,second=0,microsecond=0)
 if n>=next_run:next_run=(datetime.combine(next_session(today),time(15,20),TZ))
 if n>=settle_run:settle_run=(datetime.combine(next_session(today),time(15,40),TZ))
 with db(path) as c:
  batches=[dict(x) for x in c.execute('SELECT batch_id,target_session,status,nominal_n,qualified_n,block_reason,created_at,sealed_at FROM batch ORDER BY created_at DESC')]
 return {'protocol_hash':h,'protocol_id':p['protocol_id'],'model_status':p['model']['model_status'],'next_producer_run':iso(next_run),'next_settler_run':iso(settle_run),'batches':batches,'stats':s}
