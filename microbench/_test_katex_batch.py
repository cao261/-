# -*- coding: utf-8 -*-
"""Smoke test for the Node KaTeX batch renderer."""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

items = [
    {"id": 0, "tex": r"\mu_0", "display": False},
    {"id": 1, "tex": r"V = I \cdot R", "display": True},
    {"id": 2, "tex": r"\frac{\partial f}{\partial x}", "display": False},
    {"id": 3, "tex": r"\begin{align} a &= b \\ c &= d \end{align}", "display": True},
]

proc = subprocess.run(
    ["node", str(HERE / "_katex_batch.js")],
    input=json.dumps(items),
    capture_output=True,
    text=True,
    encoding="utf-8",
    timeout=60,
)
if proc.returncode != 0:
    print("node failed:", proc.stderr[:2000])
    sys.exit(1)

out = json.loads(proc.stdout)
assert len(out) == len(items), f"expected {len(items)} results, got {len(out)}"
ok = 0
for r in out:
    if r["error"]:
        print(f"  id={r['id']} ERROR: {r['error']}")
    else:
        ok += 1
        head = r["html"][:90].replace("\n", " ")
        print(f"  id={r['id']} ok  {head}")
print(f"\n{ok}/{len(items)} rendered")
sys.exit(0 if ok == len(items) else 1)
