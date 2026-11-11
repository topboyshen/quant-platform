from __future__ import annotations
import argparse,json,os
from pathlib import Path
from phase2 import provision_manual_roles

def main():
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--output',required=True);a=p.parse_args();
 if os.geteuid()!=0:raise SystemExit('must run as root')
 caps=provision_manual_roles(a.db);out=Path(a.output);out.write_text(json.dumps(caps,sort_keys=True));os.chmod(out,0o600);print('manual_capabilities_provisioned=true')
if __name__=='__main__':main()
