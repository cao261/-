# -*- coding: utf-8 -*-
"""
Cross-language contract test for the tablet sync layer.

The tablet (JS) and the PC (Python) have to agree on the hash of a note's
content — that hash is what tells the merger whether the file was touched
since the tablet started editing it. If the two implementations disagree,
every single note looks like a conflict and nothing merges cleanly.

The tricky part: JS reads characters with charCodeAt() (UTF-16 code
units), Python reads them with ord() (code points). A note containing an
emoji therefore hashes differently unless Python walks UTF-16 code units
too.

Run:  python microbench/_test_tablet_sync.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from microbench.merge_changes import djb2_base36, merge_pack, safe_target  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except (AttributeError, ValueError):
    pass

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


# ---------------------------------------------------------------------------
print("\n[1] djb2 hash: JS 和 Python 必须完全一致")
# ---------------------------------------------------------------------------
SAMPLES = [
    "",
    "hello",
    "# 标题\n\n正文内容。",
    "含中文与标点的正文：微电子与半导体器件仿真。",
    "emoji 🚀 与代理对 😀",
    "$E = mc^2$ 公式行",
    "换行\n\r\n制表\t与反斜杠 \\",
    "a" * 500,
]

js_script = """
const samples = JSON.parse(process.argv[1]);
function hash(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36);
}
process.stdout.write(JSON.stringify(samples.map(hash)));
"""
proc = subprocess.run(
    ["node", "-e", js_script, json.dumps(SAMPLES)],
    capture_output=True, text=True, encoding="utf-8", timeout=60,
)
if proc.returncode != 0:
    print("  node 执行失败:", proc.stderr[:400])
    js_hashes = None
else:
    js_hashes = json.loads(proc.stdout)

if js_hashes is not None:
    for sample, js_hash in zip(SAMPLES, js_hashes):
        py_hash = djb2_base36(sample)
        label = f"hash 一致 ({js_hash}) <- {sample[:26]!r}"
        check(js_hash == py_hash, label)

    # 特别盯住 emoji：这是 UTF-16 码元 vs 码点最容易翻车的地方
    emoji = "emoji 🚀 与代理对 😀"
    check(djb2_base36(emoji) == js_hashes[SAMPLES.index(emoji)],
          "emoji 样本两端一致 (UTF-16 码元)")
else:
    check(False, "node 没能跑起来，无法对比 hash")

# ---------------------------------------------------------------------------
print("\n[2] 路径安全：越界的相对路径必须被拒绝")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    for bad in [
        "../outside.md",
        "01_Literature/../../evil.md",
        "/etc/passwd",
        "C:/Windows/x.md",
        "logs/x.md",
        "microbench/app.py",
        "00_Dashboard/../../evil.md",
        "",
        "00_Dashboard/evil.txt",
        "00_Dashboard/./ok.md",
    ]:
        check(safe_target(vault, bad) is None, f"拒绝 {bad!r}")
    ok = safe_target(vault, "01_Literature/sub/note.md")
    check(ok is not None and ok.name == "note.md", "接受 01_Literature/sub/note.md")

# ---------------------------------------------------------------------------
print("\n[3] 合并：无冲突时平板版本直接生效")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    rel = "03_Knowledge/note.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "# 笔记\n\n原始内容\n"
    target.write_text(original, encoding="utf-8")

    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{
            "id": rel, "title": "笔记", "group": "03_Knowledge",
            "body": "# 笔记\n\n平板改过的内容\n",
            "baseHash": djb2_base36(original),
        }],
    }
    s = merge_pack(pack, vault)
    check(len(s["written"]) == 1, "写入 1 篇")
    check(target.read_text(encoding="utf-8").endswith("平板改过的内容\n"),
          "文件内容已更新为平板版本")
    check(s["conflicts"] == [], "没有冲突")

# ---------------------------------------------------------------------------
print("\n[4] 合并：两边都改了 -> 另存合并稿，原文件不动")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    rel = "03_Knowledge/note.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    base = "# 笔记\n\n共同起点\n"
    target.write_text(base, encoding="utf-8")

    # 电脑端后来改了
    target.write_text("# 笔记\n\n电脑端的新内容\n", encoding="utf-8")

    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{
            "id": rel, "title": "笔记", "group": "03_Knowledge",
            "body": "# 笔记\n\n平板端的新内容\n",
            "baseHash": djb2_base36(base),   # 平板改之前看到的版本
        }],
    }
    s = merge_pack(pack, vault)
    check(len(s["conflicts"]) == 1, "识别出 1 处冲突")
    check(target.read_text(encoding="utf-8").endswith("电脑端的新内容\n"),
          "原文件没有被平板版本覆盖")
    merged = list((vault / "03_Knowledge").glob("*_冲突合并_*.md"))
    check(len(merged) == 1, "生成了 1 个冲突合并稿")
    if merged:
        text = merged[0].read_text(encoding="utf-8")
        check("平板端的新内容" in text, "合并稿里有平板版本")
        check("电脑端的新内容" in text, "合并稿里有电脑版本")

# ---------------------------------------------------------------------------
print("\n[5] 合并：新增笔记 / 恶意路径")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [
            {"id": "00_Dashboard/new.md", "title": "新笔记", "group": "00_Dashboard",
             "body": "# 新笔记\n", "baseHash": ""},
            {"id": "../../evil.md", "title": "越界", "group": "x",
             "body": "evil", "baseHash": ""},
            {"id": "logs/evil.md", "title": "越界2", "group": "logs",
             "body": "evil", "baseHash": ""},
        ],
    }
    s = merge_pack(pack, vault)
    check(len(s["created"]) == 1, "只创建了 1 个合法文件")
    check(len(s["rejected"]) == 2, "拒绝了 2 个越界路径")
    check(not (vault.parent / "evil.md").exists(), "vault 外面没有落文件")

# ---------------------------------------------------------------------------
print("\n[6] 幂等：同一份包合并两次，第二次应该是 no-op")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    vault = Path(td)
    rel = "03_Knowledge/idem.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("原始\n", encoding="utf-8")
    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{"id": rel, "title": "x", "group": "03_Knowledge",
                   "body": "新的\n", "baseHash": djb2_base36("原始\n")}],
    }
    merge_pack(pack, vault)
    s2 = merge_pack(pack, vault)
    check(len(s2["written"]) == 0 and len(s2["conflicts"]) == 0,
          "第二次合并没有再写、没有再报冲突")

print(f"\n{'=' * 60}")
print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
