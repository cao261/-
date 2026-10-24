# -*- coding: utf-8 -*-
"""把 classes.dex 加进 aapt2 生成的 base.apk。

用 Python 的 zipfile 而不是 `aapt add`：aapt v1 对 aapt2 产出的包有时会
报 "unable to add"；而且我们只加一个文件，zipfile 更可控。

dex 在 APK 里通常是不压缩存储的（对齐要求 4 字节），但用 DEFLATE 也完全
合法 —— zipalign 会在签名之前重新排布。
"""
import sys
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

    tmp.replace(apk)
    print(f"  classes.dex 已加入 ({dex.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
