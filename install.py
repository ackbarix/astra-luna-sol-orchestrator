#!/usr/bin/env python3
import argparse, hashlib, json, os, pathlib, stat, sys, tempfile
import config_overlay
ROOT=pathlib.Path(__file__).resolve().parent
A="<!-- ALS:BEGIN -->"; B="<!-- ALS:END -->"
def hashit(x):return hashlib.sha256(x.encode()).hexdigest()
def write(p,s,mode=0o644):
 p.parent.mkdir(parents=True,exist_ok=True)
 fd,t=tempfile.mkstemp(dir=p.parent)
 try:
  with os.fdopen(fd,"w") as f:f.write(s)
  os.chmod(t,stat.S_IMODE(p.stat().st_mode) if p.exists() else mode);os.replace(t,p)
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
 # Migrate first-release manifests: a newly created config was tracked as a file.
 if ".codex/config.toml" in old["files"]:
  old.setdefault("config",{"mode":"created","sha256":old["files"].pop(".codex/config.toml")})
 cfg=target/".codex/config.toml"
 config_state=old.get("config")
 if a.check and not mf.exists():print("Not installed");return 1
 if a.uninstall and not mf.exists():print("Nothing installed");return 0
 try:
  if cfg.is_symlink() or any(d.is_symlink() for d in cfg.parents if d!=target and target in d.parents):
   raise ValueError("Symlinked Codex config is unsafe")
  if config_state:
   if not cfg.is_file():raise ValueError("Previously installed Codex config is missing")
   before=cfg.read_text(encoding="utf-8")
   config_overlay.check(before,config_state)
   if config_state["mode"]=="merged":
    restored_config=config_overlay.restore(before,config_state)
    desired_config,next_config_state=config_overlay.merge(restored_config)
   else:
    restored_config=None
    desired_config=(ROOT/"profiles/config.toml").read_text(encoding="utf-8")
    next_config_state={"mode":"created","sha256":hashit(desired_config)}
  elif cfg.exists():
   if not cfg.is_file():raise ValueError("Existing Codex config is not a file")
   restored_config=None
   desired_config,next_config_state=config_overlay.merge(cfg.read_text(encoding="utf-8"))
  else:
   restored_config=None
   desired_config=(ROOT/"profiles/config.toml").read_text(encoding="utf-8")
   next_config_state={"mode":"created","sha256":hashit(desired_config)}
 except (OSError,KeyError,ValueError) as ex:
  print("REFUSED: unsafe Codex config merge: "+str(ex),file=sys.stderr);return 2
 sources={".codex/agents/"+p.name:p for p in (ROOT/"profiles/agents").glob("als_*.toml")}
 sources[".agents/skills/astra-luna-sol/SKILL.md"]=ROOT/"profiles/skills/astra-luna-sol/SKILL.md"
 sources[".astra-luna-sol/run.sh"]=ROOT/"scripts/run.sh"
 sources[".astra-luna-sol/token_usage.py"]=ROOT/"scripts/token_usage.py"
 sources[".astra-luna-sol/usage.sh"]=ROOT/"scripts/usage.sh"
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
  if not config_state:print("Existing Codex config needs new managed settings; run installer");return 2
  print("INSTALL VERIFIED (live Codex routing not tested)");return 0
 if a.uninstall:
  if not mf.exists():print("Nothing installed");return 0
  print("Remove tracked files, restore Codex model settings and managed AGENTS.md block")
  if a.dry_run:return 0
  for rel in old["files"]:(target/rel).unlink()
  if config_state:
   if config_state["mode"]=="created":cfg.unlink()
   else:write(cfg,restored_config)
  if ag.exists() and oldblock:
   if old["created"] and orig==oldblock+"\n":ag.unlink()
   else:write(ag,orig.replace("\n\n"+oldblock+"\n","",1).replace(oldblock+"\n","",1))
  mf.unlink();print("UNINSTALL OK");return 0
 print("Installing "+str(len(desired))+" managed files to "+str(target))
 if a.dry_run:print("DRY RUN: no files changed");return 0
 for rel,s in desired.items():write(target/rel,s,0o755 if rel.endswith(("/run.sh","/usage.sh","/token_usage.py")) else 0o644)
 if not cfg.is_file() or cfg.read_text(encoding="utf-8")!=desired_config:write(cfg,desired_config)
 block=A+"\n"+(ROOT/"profiles/AGENTS.snippet.md").read_text().strip()+"\n"+B
 if oldblock:new=orig.replace(oldblock,block,1)
 elif orig:new=orig.rstrip("\n")+"\n\n"+block+"\n"
 else:new=block+"\n"
 created=old.get("created",False) if ag.exists() else True
 write(ag,new)
 write(mf,json.dumps({"files":{k:hashit(v) for k,v in desired.items()},"config":next_config_state,"agents":hashit(block),"created":created},indent=2))
 print("INSTALL OK: start Codex with \"codex\" after one-time ocx setup")
 print("Token usage: bash .astra-luna-sol/usage.sh --latest");return 0
if __name__=="__main__":
 try:sys.exit(main())
 except (OSError,ValueError,KeyError,json.JSONDecodeError) as e:print(str(e),file=sys.stderr);sys.exit(2)
