import json,subprocess
p=subprocess.run(["ps","-Ao","pid,etime,command"],capture_output=True,text=True)
procs=[l.strip()[:100] for l in p.stdout.splitlines()
       if ("git" in l.split()[2:3][0] if len(l.split())>2 else False) or "/git " in l or l.rstrip().endswith("/git")]
gitp=[l.strip()[:110] for l in p.stdout.splitlines() if " git " in l or l.strip().split()[-1:]==["git"]]
print(json.dumps({"git_processes":[g for g in gitp if "grep" not in g][:6]},ensure_ascii=False))
