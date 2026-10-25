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

    print("\n[18] 本地优先：断网也要能存能看，联网时同步上去")
    check('"annos_" + key' in app_js,
          "本地批注存在 meta store（不升 IndexedDB 版本，零升级风险）")
    check("DB_VER = 2" in app_js,
          "IndexedDB 版本没升（升级会被旧标签页挡住 → 白屏）")
    check("openDBWithoutVersion" in app_js,
          "版本对不上时降级打开（不白屏）")
    check("saveAnnotations" in app_js, "有批注保存入口")
    check("pushAnnosToCloud" in app_js, "批注会同步到坚果云")
    check("mergeAnnos" in app_js, "批注按 id 合并（不能整份覆盖）")
    # 顺序：先写本地，再同步云盘
    save_i = app_js.find("async function saveAnnotations")
    body = app_js[save_i:save_i + 900] if save_i >= 0 else ""
    check(body.find("putAnnos") < body.find("pushAnnosToCloud"),
          "保存顺序是「先写本地，再同步云盘」")

    print("\n[19] 平板要达到电脑端的同等能力")
    check("pdf-layout" in app_js, "有布局三选一（原文+译文 / 仅原文 / 仅译文）")
    check("仅原文" in app_js and "仅译文" in app_js, "三种布局都在")
    check("captureInkPage" in app_js, "能把原文+手写合成图片")
    check("askAiWithInk" in app_js, "手写能直接问 AI（多模态）")
    check("image_url" in app_js, "用 image_url 把图发给模型")
    check("saveQuoteAsAnno" in app_js, "划词能存成批注")
    check("collectSelectionRects" in app_js, "选区矩形归一化（跟电脑端同一套）")
    check("data-pdf-anno" in app_js, "划词气泡上有「标注」按钮")

    print("\n[20] vault 前缀没配时必须能自愈（2026-10-25 用户真机故障）")
    # 故障现场：网页显示「已连接坚果云」，同步结果是「已连接 0 篇」，
    # 24 篇原文 PDF 全部 404。
    # 根因：vault 前缀（「我的坚果云/MicroBench工作台」，两段）是空的 ——
    #   它只有原生设置页的「测试并保存」会写，用户没跑过就是空的。
    # ⚠ 最阴的地方：前缀错时 PROPFIND **不报 404**，回的是 207 + 0 个条目
    #   → 页面看到「目录是空的」→ 界面显示「已连接 0 篇」，一点错都看不出来。
    prefs_java2 = read(JAVA / "Prefs.java")
    handler_java2 = read(JAVA / "DavPathHandler.java")
    check("resolveRootsIfMissing" in prefs_java2,
          "Prefs 有「运行时就地探测 vault 位置」的兜底")
    check("detectVaultRoot" in code_only(prefs_java2),
          "兜底真的调用了 detectVaultRoot（不是空壳）")
    check("setDavVaultRoot" in code_only(prefs_java2),
          "探测到的位置会被存下来（下次不用再探）")
    check("resolveRootsIfMissing" in code_only(handler_java2),
          "DavPathHandler 处理请求前会先兜底")
    check("rootsResolved" in handler_java2,
          "兜底只做一次（探测要发好几次 PROPFIND，不能每个请求都探）")
    check("davInfo" in bridge,
          "DavBridge 暴露 davInfo（网页能问出 vault 的真实位置）")
    check("davInfo" in app_js and "davVaultRootLabel" in app_js,
          "网页侧会问出并显示 vault 的真实位置")
    check("entriesSeen" in app_js,
          "列目录 0 条目会被当成「路径不对」报错（不再静默显示 0 篇）")
    # ⚠ 界面不能再只看 dirHandle —— APK 走 WebDAV，dirHandle 永远是 null
    check("const backendNow = currentBackend()" in app_js,
          "文献库按 currentBackend() 判断（DAV 模式下不再谎报「还没连接文件夹」）")

    print("\n[21] 前缀「配错了」也要能自愈（非空但错的那一类）")
    # [20] 管的是「前缀是空的」。但还有一类：前缀**非空但是错的** ——
    #   用户手填错、云盘里改过目录名、或者装过探测有 bug 的旧版 APK。
    #   那时 ensureRoots() 看到「非空」就放行 → 症状跟空前缀一模一样
    #   （列目录 0 条、PDF 全 404），用户看到的还是「已连接 0 篇」。
    prefs_java3 = read(JAVA / "Prefs.java")
    handler_java3 = read(JAVA / "DavPathHandler.java")
    code_handler3 = code_only(handler_java3)
    code_prefs3 = code_only(prefs_java3)
    check("redetectVaultRoot" in code_prefs3,
          "Prefs 有「重新探测 vault 位置」的兜底")
    check("detectVaultRoot" in code_prefs3 and "setDavVaultRoot" in code_prefs3,
          "重探测真的探了位置并写回去")
    check("tryRedetect" in code_handler3,
          "DavPathHandler 有重探测的入口")
    # ⚠ 必须真的接在**两条故障路径**上：
    #   列目录返回空（用户看到「0 篇」）+ 取文件 404（24 篇原文全挂）
    #   只接一条的话，另一条的症状照样复现 —— 所以逐条查，不靠数个数。
    check(re.search(r"if \(entries\.isEmpty\(\)\) \{\s*DavClient fixed = tryRedetect",
                    code_handler3) is not None,
          "重探测接在「列目录返回空」这条路径上（用户看到的 0 篇）")
    check(re.search(r"if \(data == null\) \{\s*DavClient fixed = tryRedetect",
                    code_handler3) is not None,
          "重探测接在「取文件 404」这条路径上（24 篇原文全挂）")
    check("PROBE_RETRY_INTERVAL_MS" in code_handler3,
          "重探测有节流（探测要递归整个云盘，不能每个空目录都扫一遍）")
    # 同步根是 vault 根的兄弟目录 —— vault 位置变了，同步根也得跟着刷新
    check("detectSyncRoot" in code_prefs3 and "setDavSyncRoot" in code_prefs3,
          "重探测时同步根也一起刷新（否则译文快照还是读不到）")

    print("\n[22] 测试用的 Android 桩类不能进 APK")
    # android/_stub/ 里放了 android.content / android.webkit 的桩，
    # 让 DavPathHandler 的自愈逻辑能在普通 JVM 里被真正跑一遍。
    # ⚠ 它们**绝不能**挪进 android/java/ —— build_apk.ps1 会把那儿全编译一遍，
    #   桩类跟真 android.jar 撞类，APK 直接编不过。
    stub_dir = HERE / "_stub"
    check(stub_dir.is_dir(), "桩类目录存在（测试台要靠它）")
    stub_in_java = list((HERE / "java").rglob("android/content/*.java")) + \
        list((HERE / "java").rglob("android/webkit/*.java"))
    check(not stub_in_java,
          "桩类没被挪进 android/java（否则 APK 编不过）: "
              + (", ".join(str(p) for p in stub_in_java) or "(无)"))
    check((HERE / "TestDavPath.java").is_file(),
          "有 TestDavPath（自愈链路的回归测试）")

    print("\n[23] 探测 vault 必须靠**内容**，不能靠名字（2026-10-25 真机故障·第三轮）")
    # 用户真机截图：界面亮出「当前 vault 位置: 我的坚果云/MicroBench同步」。
    # 根因：真实云盘里 vault 和同步根是**兄弟目录**，名字共享前缀：
    #     我的坚果云/MicroBench工作台/   ← vault
    #     我的坚果云/MicroBench同步/     ← 同步根
    # 坚果云按名字排序，「同」(U+540C) 在「工」(U+5DE5) 前面
    # → 只按名字判断的实现 **100% 选错**，不是偶发。
    client_java = read(JAVA / "DavClient.java")
    code_client = code_only(client_java)
    check("isVaultMarkerDir" in code_client,
          "有「vault 标志目录」判据（00_Dashboard / 01_Literature …）")
    check("isSyncMarkerDir" in code_client,
          "有「同步根标志目录」判据（snapshots / annotations）")
    check(re.search(r"if \(isVaultDir\(e\.path\)\)", code_client) is not None,
          "名字像的候选**必须验证内容**才认（不能只看名字）")
    check("isVaultDir" in code_client and "listRaw" in code_client,
          "内容验证是真的列目录去看，不是空壳")
    # ⚠ mock 必须忠实：兄弟目录 + 按名字排序。
    #   少了任一条，这个 bug 就测不出来（还给人「已经修好了」的错觉）。
    dav_py = read(HERE / "_test_dav.py")
    check("我的坚果云/MicroBench同步/snapshots" in dav_py,
          "mock 里有同步根（vault 的兄弟目录）")
    check(re.search(r"items\.sort\(", dav_py) is not None,
          "mock 的 PROPFIND 按名字排序（真实服务器都这样）")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
