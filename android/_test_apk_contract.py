# -*- coding: utf-8 -*-
"""
APK 侧的源码契约护栏。

这轮踩的坑里有几个是「改回去也不会立刻报错、但功能会静默坏掉」的，
所以用静态检查把它们钉死：

  1. MainActivity 必须用 startActivityForResult —— 用 startActivity 的话
     onActivityResult 永远不触发，设置页保存后页面不知道，状态一直显示
     「未连接坚果云」。
  2. 不能用 location.reload() 刷新 —— 会把页面里已有的状态整个丢掉。
  3. PROPFIND / MKCOL 必须走裸 socket —— HttpURLConnection 只放行标准方法。
  4. 不能出现 lambda（->）—— Android 上 javac 找不到 LambdaMetafactory。
  5. 不能依赖 androidx —— 手工打包没有 Gradle 拉 Maven 依赖。
  6. d8 必须是 build-tools 35+ —— 34 自带的 d8 8.2.2 读 javac 21 的 class 会 NPE。

跑:  python android/_test_apk_contract.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JAVA = HERE / "java" / "microbench" / "tablet"
BUILD = HERE / "build_apk.ps1"

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def code_only(src: str) -> str:
    """去掉注释行 —— 否则注释里提到某个 API 会被误判成「用到了它」。"""
    out = []
    in_block = False
    for line in src.splitlines():
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("//") or s.startswith("*"):
            continue
        out.append(line)
    return "\n".join(out)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    main_java = read(JAVA / "MainActivity.java")
    dav = read(JAVA / "DavClient.java")
    bridge = read(JAVA / "DavBridge.java")
    settings = read(JAVA / "SettingsActivity.java")
    manifest = read(HERE / "AndroidManifest.xml")
    build = read(BUILD)

    print("\n[1] 设置页保存后页面必须能知道")
    check("startActivityForResult" in main_java,
          "MainActivity 用 startActivityForResult (不是 startActivity)")
    check(not re.search(r"\bstartActivity\s*\(\s*new Intent", main_java),
          "没有退化成 startActivity")
    check("__mbRefreshSync" in main_java,
          "保存后通知页面刷新同步状态")
    check("location.reload()" not in code_only(main_java),
          "没有用 location.reload() (会丢页面状态)")
    # 网页侧必须有这个钩子
    app_js = read(HERE.parent / "microbench" / "static" / "tablet" / "app.js")
    check("window.__mbRefreshSync" in app_js,
          "网页侧暴露了 __mbRefreshSync 钩子")

    print("\n[2] 「测试连接」必须真的保存凭证")
    check("saveIfOk" in settings, "测试通过后调用 saveIfOk")
    check("prefs.setDav" in settings, "设置页会写 SharedPreferences")
    check("测试并保存" in settings, "按钮文案说明会保存")

    print("\n[3] WebDAV 的非标准方法必须走裸 socket")
    check("rawRequest" in dav, "DavClient 有 rawRequest")
    check('rawRequest("PROPFIND"' in dav, "PROPFIND 走 rawRequest")
    check('rawRequest("MKCOL"' in dav, "MKCOL 走 rawRequest")
    check("readChunked" in dav, "支持 chunked 响应解码")
    check('setRequestMethod("PROPFIND")' not in dav,
          "没有用 setRequestMethod 发 PROPFIND (会抛 ProtocolException)")

    print("\n[4] Android 上不能用 lambda")
    for name, src in (("MainActivity", main_java), ("DavClient", dav),
                      ("DavBridge", bridge), ("SettingsActivity", settings)):
        # 注释里的箭头不算; 字符串字面量里的 "-> HTTP" 也不算
        # （真正的 lambda 行是 `Type name = (args) -> {`, 几乎不会带双引号）
        bad = []
        for line in code_only(src).splitlines():
            if "->" in line and '"' not in line and "'" not in line:
                bad.append(line.strip())
        check(not bad, f"{name} 里没有 lambda" + (f" -> {bad[:1]}" if bad else ""))

    print("\n[5] 不能依赖 androidx (手工打包没有 Gradle)")
    for name, src in (("MainActivity", main_java), ("DavClient", dav),
                      ("DavBridge", bridge), ("SettingsActivity", settings)):
        # 只看 import —— 注释里写「不依赖 androidx」不算依赖
        imports = [l for l in code_only(src).splitlines()
                   if l.strip().startswith("import ")]
        check(not any("androidx" in l for l in imports),
              f"{name} 没有 import androidx")

    print("\n[6] 构建工具版本")
    check("35.0.1" in build or "BuildTools" in build,
          "build_apk.ps1 指定了 >=35 的 build-tools (d8 8.2.2 会 NPE)")
    check("com.android.tools.r8.D8" in build, "d8 用 java -cp 直接调, 绕开 bat 的中文 JAVA_HOME")
    check("com.android.apksigner.ApkSignerTool" in build,
          "apksigner 同样用 java -cp 直接调")
    check("-A " in build or '"-A"' in build or "-A$" in build,
          "aapt2 link 带了 -A assets (漏了会得到 50KB 空壳)")
    check("mbbuild" in build or "Stage" in build,
          "构建暂存到纯 ASCII 路径 (aapt2 吃不了中文路径)")

    print("\n[7] APK 必备组件")
    for needed in ("MainActivity", "SettingsActivity"):
        check(needed in manifest, f"AndroidManifest 声明了 {needed}")
    check("INTERNET" in manifest, "声明了 INTERNET 权限")
    check("exported" in manifest, "MainActivity exported (能当启动器)")

    print("\n[8] 页面侧的双通道后端")
    check("BACKENDS" in app_js and '"dav"' in app_js and '"fsa"' in app_js,
          "网页有 fsa / dav 两套后端")
    check("davUrl" in app_js, "有 /dav/ 的 URL 构造")
    check("getCachedPdf" in app_js, "原文 PDF 有本地缓存 (离线可读)")
    check("cacheAllPdfs" in app_js, "有「缓存全部原文」功能")
    check("pdfcache" in app_js, "IndexedDB 里有 pdfcache store")
    check("DB_VER = 2" in app_js, "IndexedDB 版本 >= 2 (新增 pdfcache)")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
