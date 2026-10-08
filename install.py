#!/usr/bin/env python3
import argparse, hashlib, json, os, pathlib, sys, tempfile
ROOT=pathlib.Path(__file__).resolve().parent
A="<!-- ALS:BEGIN -->"; B="<!-- ALS:END -->"
def hashit(x):return hashlib.sha256(x.encode()).hexdigest()
def write(p,s,mode=0o644):
 p.parent.mkdir(parents=True,exist_ok=True)
 fd,t=tempfile.mkstemp(dir=p.parent)
 try:
  with os.fdopen(fd,"w") as f:f.write(s)
  os.chmod(t,mode);os.replace(t,p)
 finally:
  if os.path.exists(t):os.unlink(t)
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--target",required=True)
 ap.add_argument("--dry-run",action="store_true")
 q=ap.add_mutually_exclusive_group();q.add_argument("--check",action="store_true");q.add_argument("--uninstall",action="store_true")
 a=ap.parse_args();target=pathlib.Path(a.target).expanduser().resolve()
 if not target.is_dir() or target==ROOT:raise ValueError("Use an existing target project")
 mf=target/".astra-luna-sol/manifest.json"
 old=json.loads(mf.read_text()) if mf.exists() else {"files":{},"agents":None,"created":False}
 sources={".codex/agents/"+p.name:p for p in (ROOT/"profiles/agents").glob("als_*.toml")}
 sources[".agents/skills/astra-luna-sol/SKILL.md"]=ROOT/"profiles/skills/astra-luna-sol/SKILL.md"
 sources[".astra-luna-sol/run.sh"]=ROOT/"scripts/run.sh"
 if ".codex/config.toml" in old["files"] or not (target/".codex/config.toml").exists():sources[".codex/config.toml"]=ROOT/"profiles/config.toml"
 desired={k:v.read_text() for k,v in sources.items()}
 ag=target/"AGENTS.md";orig=ag.read_text() if ag.is_file() else ""
 if A in orig or B in orig:
  if orig.count(A)!=1 or orig.count(B)!=1 or orig.index(A)>orig.index(B):raise ValueError("Broken AGENTS.md markers")
  oldblock=orig[orig.index(A):orig.index(B)+len(B)]
 else:oldblock=None
 errs=[]
 for rel,expected in old["files"].items():
  p=target/rel
  if not p.is_file() or hashit(p.read_text())!=expected:errs.append("Modified or missing: "+rel)
 for rel in desired:
  p=target/rel
  if rel not in old["files"] and p.exists():errs.append("User-owned: "+rel)
 if oldblock and hashit(oldblock)!=old.get("agents"):errs.append("Unowned/modified AGENTS.md block")
 if old.get("agents") and not oldblock:errs.append("Missing AGENTS.md block")
 for rel in desired:
  p=target/rel
  if p.is_symlink() or any(d.is_symlink() for d in p.parents if target in d.parents):errs.append("Unsafe symlink: "+rel)
 if errs:
  print("\n".join(errs),file=sys.stderr);return 2
 if a.check:
  if not mf.exists():print("Not installed");return 1
  print("INSTALL VERIFIED (live Codex routing not tested)");return 0
 if a.uninstall:
  if not mf.exists():print("Nothing installed");return 0
  print("Remove tracked files and managed AGENTS.md block")
  if a.dry_run:return 0
  for rel in old["files"]:(target/rel).unlink()
  if ag.exists() and oldblock:
   if old["created"] and orig==oldblock+"\n":ag.unlink()
   else:write(ag,orig.replace("\n\n"+oldblock+"\n","",1).replace(oldblock+"\n","",1))
  mf.unlink();print("UNINSTALL OK");return 0
 print("Installing "+str(len(desired))+" managed files to "+str(target))
 if a.dry_run:print("DRY RUN: no files changed");return 0
 for rel,s in desired.items():write(target/rel,s,0o755 if rel.endswith("/run.sh") else 0o644)
 block=A+"\n"+(ROOT/"profiles/AGENTS.snippet.md").read_text().strip()+"\n"+B
 if oldblock:new=orig.replace(oldblock,block,1)
 elif orig:new=orig.rstrip("\n")+"\n\n"+block+"\n"
 else:new=block+"\n"
 created=old.get("created",False) if ag.exists() else True
 write(ag,new)
 write(mf,json.dumps({"files":{k:hashit(v) for k,v in desired.items()},"agents":hashit(block),"created":created},indent=2))
 print("INSTALL OK: run ./.astra-luna-sol/run.sh --configure-ocx");return 0
if __name__=="__main__":
 try:sys.exit(main())
 except (OSError,ValueError,KeyError,json.JSONDecodeError) as e:print(str(e),file=sys.stderr);sys.exit(2)
