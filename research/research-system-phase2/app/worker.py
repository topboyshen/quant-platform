from __future__ import annotations
import argparse,json
from phase2 import init,register_protocol,run_producer,run_settler,db,iso

def state(path,name,value):
 with db(path) as c:c.execute('INSERT INTO run_state VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(name,json.dumps(value,ensure_ascii=False,sort_keys=True),iso()))
def main():
 p=argparse.ArgumentParser();p.add_argument('job',choices=('producer','settler'));p.add_argument('--db',required=True);p.add_argument('--protocol',required=True);a=p.parse_args();init(a.db);register_protocol(a.db,a.protocol)
 try:r=run_producer(a.db,a.protocol) if a.job=='producer' else run_settler(a.db,a.protocol);state(a.db,a.job,r);print(json.dumps(r,ensure_ascii=False,sort_keys=True))
 except Exception as e:r={'status':'BLOCKED','reason':type(e).__name__};state(a.db,a.job,r);print(json.dumps(r,ensure_ascii=False,sort_keys=True));raise
if __name__=='__main__':main()
