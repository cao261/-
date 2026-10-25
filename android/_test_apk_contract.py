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

    print("\n[9] vault 前缀 (网页给的是 vault 相对路径, 少前缀就全 404)")
    check("vaultRoot" in dav, "DavClient 有 vaultRoot")
    check("detectVaultRoot" in dav, "能自动探测 vault 在云盘里的位置")
    check("rawUrlFor" in dav, "有不带前缀的 URL 构造 (探测时用)")
    check("davVaultRoot" in read(JAVA / "Prefs.java"), "Prefs 存了 vault 位置")
    check("detectVaultRoot" in settings, "设置页会调用自动探测")
    # 关键: urlFor 必须带前缀, 而 appendPath 不能把根自己的斜杠删掉
    check("urlFor" in dav and "vaultRoot" in dav.split("urlFor")[1][:600],
          "urlFor 会拼上 vault 前缀")
    check("rootLen" in dav, "去尾斜杠时保住了根的斜杠")

    print("\n[10] TLS 必须做主机名验证 (裸 socket 默认不验)")
    check("setEndpointIdentificationAlgorithm" in code_only(dav),
          "裸 socket 开了 endpointIdentificationAlgorithm")
    check("getDefaultHostnameVerifier" in code_only(dav),
          "握手后再验一次主机名")
    # 顺序必须是 createSocket() -> setSSLParameters -> connect
    code = code_only(dav)
    i_create = code.find(".createSocket()")
    i_params = code.find("setSSLParameters")
    i_connect = code.find("sock.connect(")
    check(i_create >= 0 and i_params > i_create and i_connect > i_params,
          "顺序是 createSocket() -> setSSLParameters -> connect")
    check(".createSocket(host" not in code and ".createSocket(u.getHost()" not in code,
          "没有用 createSocket(host,port) (内部就握手了, 主机名验证来不及生效)")

    print("\n[11] 不能依赖 API 26 才有的 java.util.Base64 (minSdk 是 24)")
    check("java.util.Base64" not in code_only(dav), "DavClient 没有 import java.util.Base64")
    check("base64(" in dav, "用了自实现的 Base64")
    check("isLoopback" in dav, "http 根只对回环地址豁免 (明文会把应用密码送出去)")

    print("\n[12] 设置页不能跨线程改 UI (CalledFromWrongThreadException)")
    probe = settings.split("class ProbeTask")[1] if "class ProbeTask" in settings else ""
    probe_body = probe.split("class StatusTask")[0] if probe else ""
    check("setText" not in probe_body,
          "ProbeTask (后台线程) 里没有 setText")
    check("runOnUiThread" in settings, "UI 更新走 runOnUiThread")
    check("vaultInput.setText" in code_only(settings),
          "vault 位置在主线程上回填")
    check("getPartial" in settings,
          "下载链路测试只读文件头 (不拉整篇 PDF)")

    print("\n[13] 凭证不能被系统备份出去")
    check('android:allowBackup="false"' in manifest, "allowBackup=false")

    print("\n[14] 原生壳的交互不能是死的")
    check("onShowFileChooser" in main_java,
          "有 WebChromeClient.onShowFileChooser (导入 vault 包才有点击反应)")
    check("REQ_FILE" in main_java, "文件选择有独立的请求码")
    check("parseResult" in main_java, "文件选择结果回传给网页")
    check("onBackPressed" in main_java, "物理返回键不是直接退出应用")
    check("onDestroy" in main_java and "destroy()" in main_java,
          "WebView 在 onDestroy 里销毁 (不然每次切后台都漏一份)")
    check("httpPost" in bridge and "httpPost" in app_js,
          "AI 请求走原生代理 (APK 里页面的 fetch 会被 CORS 打死)")

    print("\n[15] 页面侧的同步链路不能断")
    check('state.syncMode === "dir"' not in app_js,
          "同步按钮不再判断永远不成立的 \"dir\"")
    idx = app_js.find('btn-sync").onclick')
    handler = app_js[idx:idx + 500] if idx >= 0 else ""
    check("currentBackend()" in handler,
          "同步按钮按 currentBackend() 判断")
    check("pullFromDir" in handler and "pushToDir" in handler,
          "同步按钮真的会拉 + 写")
    check("pushToDir(true)" in app_js, "保存后会自动写回云盘")
    check("scan.pdfs" in app_js, "PDF 清单从云盘实时扫 (云盘新加的论文能看见)")
    check("parseSse" in app_js, "SSE 解析抽成了共用函数 (原生代理也用)")
    check("httpPost" in app_js, "AI 请求在 APK 里走原生代理")

    print("\n[16] 译文 / 批注：必须能读到「同步根」")
    # 同步根是 vault 根的**兄弟目录**（我的坚果云/MicroBench同步），
    # 不在 vault 里面 —— 桌面端把译文快照和批注放在那儿。
    # 没有它，平板就只剩「能看 PDF」，跟用微信直接打开没区别。
    prefs_java = read(JAVA / "Prefs.java")
    handler_java = read(JAVA / "DavPathHandler.java")
    check("davSyncRoot" in prefs_java, "Prefs 存了同步根的位置")
    check("syncClient" in prefs_java, "Prefs 能造一个同步根的客户端")
    check("/dav/sync/list/" in handler_java,
          "DavPathHandler 有同步根的列目录路由")
    check("/dav/sync/file/" in handler_java,
          "DavPathHandler 有同步根的读文件路由")
    check("detectSyncRoot" in dav, "能自动探测同步根（vault 根的兄弟目录）")
    check("putSync" in bridge, "有写同步根的桥（手写标注要存到共享目录）")
    check("syncUrl" in app_js, "页面侧用 syncUrl 访问同步根")
    check("loadTranslation" in app_js, "页面侧会拉译文快照")
    check("renderTranslationInto" in app_js, "译文会渲染到 PDF 页卡里")
    check("saveInk" in app_js, "手写笔迹能存回共享目录")
    check("pdfFitScale" in app_js, "PDF 按容器宽度缩放（别再只显示一半）")

    print("\n[17] 坚果云限流要有防护（503 BlockedTemporarily）")
    # 一次完整同步是 50+ 个请求（每个目录一次 PROPFIND、每篇笔记一次 GET）。
    # 坚果云限流后返回 503「Too many requests are received recently」——
    # 不节流 + 不退避的话，用户点几次「测试并保存」就必然失败。
    check("MIN_INTERVAL_MS" in dav, "有请求间隔节流")
    check("throttle()" in dav, "请求前会过节流闸门")
    check("isRateLimited" in dav, "认得出 BlockedTemporarily")
    check("backoff" in dav, "限流后退避重试")
    check("RATE_LIMIT_MSG" in dav, "限流时给的是人话提示，不是甩一段 XML")
    check("MAX_PROPFIND_CALLS = 12" in dav,
          "探测预算收紧（原来 24 次能把坚果云打到限流）")
    check("findSmallestNote" in dav,
          "下载链路自检用小文件（不再为了找 PDF 发一串请求）")
    check("mtime" in dav, "目录项带 mtime（用来跳过没变的笔记）")
    check("local.mtime === e.mtime" in app_js,
          "页面侧按 mtime 跳过没变的笔记（同步请求数 50+ -> 20）")
    check('msg.indexOf("限流")' in app_js,
          "限流错误不会被静默吞掉（否则用户以为同步成功了）")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
