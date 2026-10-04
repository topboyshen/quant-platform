from __future__ import annotations
import argparse,html,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from phase2 import dashboard,init
class H(BaseHTTPRequestHandler):
 db_path='';protocol_path=''
 def log_message(self,*a):pass
 def sendj(self,x):
  b=json.dumps(x,ensure_ascii=False,sort_keys=True).encode();self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
  if self.path=='/healthz':return self.sendj({'status':'ok','scope':'loopback_phase2'})
  if self.path=='/api/dashboard':return self.sendj(dashboard(self.db_path,self.protocol_path))
  if self.path=='/':
   x=dashboard(self.db_path,self.protocol_path);s=x['stats'];rows=''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'%(html.escape(str(b.get('target_session'))),html.escape(str(b.get('status'))),b.get('nominal_n'),b.get('qualified_n'),html.escape(str(b.get('block_reason') or ''))) for b in x['batches']) or '<tr><td colspan=5>尚无 batch；任务会在盘后数据终态可验证后运行。</td></tr>'
   page=f'''<!doctype html><meta charset=utf-8><title>Phase 2 前瞻校准</title><style>body{{font-family:system-ui;max-width:1100px;margin:32px auto;background:#f7f9fc;color:#14213d}}.card{{background:#fff;border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 1px 5px #dce3ee}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #e6edf5;text-align:left}}code{{word-break:break-all}}.warn{{color:#9d4f00}}</style><h1>真实前瞻校准 Phase 2</h1><div class=card><b>协议：</b><code>{html.escape(x['protocol_hash'])}</code><br><b>模型：</b>{html.escape(x['model_status'])}<br><b>下次预测：</b>{html.escape(x['next_producer_run'] or ('已冻结，不会自动运行' if x['schedule_state']=='FROZEN_NO_AUTOMATIC_RUN' else '运行模式未知，不显示计划'))}<br><b>下次结算：</b>{html.escape(x['next_settler_run'] or ('已冻结，不会自动运行' if x['schedule_state']=='FROZEN_NO_AUTOMATIC_RUN' else '运行模式未知，不显示计划'))}</div><div class=card><h2>样本充分性</h2><p>已冻结批次：{s['sealed_batches']}；待独立审核 outcome：{s['pending_review_n']}；正式审核样本：{s['formal_n']}；独立 batch：{s['independent_batch_count']}；状态：<b class=warn>{s['calibration_state']}</b></p><p>正式 Brier：{s['formal_calibration'].get('brier')}。PENDING_REVIEW 仅显示 operational provisional，不进入正式校准。</p></div><div class=card><h2>批次</h2><table><tr><th>目标日</th><th>状态</th><th>nominal n</th><th>合格数</th><th>阻断原因</th></tr>{rows}</table></div><div class=card>此页面为本机 loopback 服务；不提供审核、风险否决、校准激活或写入接口。</div>'''
   b=page.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  self.send_error(404)
 def do_POST(self):self.send_error(403)
def main():
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--protocol',required=True);p.add_argument('--port',type=int,default=18768);a=p.parse_args();init(a.db);H.db_path=a.db;H.protocol_path=a.protocol;ThreadingHTTPServer(('127.0.0.1',a.port),H).serve_forever()
if __name__=='__main__':main()
