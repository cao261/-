# -*- coding: utf-8 -*-
"""把 classes.dex 加进 aapt2 生成的 base.apk。

用 Python 的 zipfile 而不是 `aapt add`：aapt v1 对 aapt2 产出的包有时会
报 "unable to add"；而且我们只加一个文件，zipfile 更可控。

dex 在 APK 里通常是不压缩存储的（对齐要求 4 字节），但用 DEFLATE 也完全
合法 —— zipalign 会在签名之前重新排布。
"""
import sys
import time
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: _add_dex.py <apk> <classes.dex>")
        return 2
    apk = Path(sys.argv[1])
    dex = Path(sys.argv[2])
    if not apk.exists():
        print(f"APK 不存在: {apk}")
        return 1
    if not dex.exists():
        print(f"dex 不存在: {dex}")
        return 1

    # 读出原有内容（跳过已有同名项）
    with zipfile.ZipFile(apk, "r") as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()
                 if i.filename != "classes.dex"]

    tmp = apk.with_suffix(".tmp.apk")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            z.writestr(info, data)
        z.write(dex, "classes.dex")

    # ⚠⚠ Windows 上 `os.replace` 覆盖**已存在**的目标可能报
    #    PermissionError(WinError 5)：杀毒软件 / 索引服务会在文件刚写完的
    #    瞬间短暂持有句柄（实测 2026-10-28 构建 APK 时撞到）。
    #    → 先把目标删掉再 replace（删除对已打开的读句柄也是允许的），
    #      并带几次重试兜住扫描器。
    for attempt in range(6):
        try:
            if apk.exists():
                apk.unlink()
            tmp.replace(apk)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.4 * (attempt + 1))
    print(f"  classes.dex 已加入 ({dex.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
