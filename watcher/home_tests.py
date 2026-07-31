#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
TRADE=Path.home()/"All here"/"trade"
VENV=TRADE/".venv"/"bin"/"python"
tests=["tests/test_dashboard_e2e.py::test_initial_view_is_home",
       "tests/test_dashboard_e2e.py::test_home_has_three_entry_buttons",
       "tests/test_dashboard_e2e.py::test_quick_link_buttons_on_home"]
p=subprocess.run([str(VENV),"-m","pytest",*tests,"-q","--no-header","-p","no:warnings"],
                 cwd=str(TRADE),capture_output=True,text=True,timeout=600)
o=((p.stdout or "")+(p.stderr or "")).strip().splitlines()
print(json.dumps({"kind":"home_tests","rc":p.returncode,"passed":p.returncode==0,
                  "tail":o[-10:]},ensure_ascii=False,indent=2))
