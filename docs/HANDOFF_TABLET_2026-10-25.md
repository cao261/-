# 平板工作台 APK —— 2026-10-25 系统性审查与修复

> 这份文档记录 **2026-10-25 这一轮「整个代码审核 → 修复 → 验证」** 的结论。
> 目的是让下一台设备 / 下一个人知道：**哪些坑已经填了、为什么这么填**，
> 以及**还有哪些没验证到（真机边界）**。
>
> 状态：**VERDICT = PASS**（静态审查 + 模拟环境全绿；真机待用户实测）。

---

## 1. 一句话结论

上一版 APK **装到平板上走不通主流程**（配坚果云 → 同步 → 打开 PDF）：

| 级别 | 数量 | 典型 |
|---|---|---|
| 阻断级 | 4 | 「测试并保存」永远失败；Android 7.x 整个应用崩；同步按钮是死的；平板改动永远写不回云盘 |
| 重要级 | 12 | TLS 不验主机名；大文件 OOM；chunked 响应能撑爆内存；AI 被 CORS 打死 |
| 次要级 | 20+ | 重复 import、无用 import、注释与代码不符 |

**这一轮全部修完，并给每一项都补了回归测试**（护栏钉死，改回去会红）。

---

## 2. 阻断级问题的根因与修法

### B1　设置页跨线程改 UI ⇒「测试并保存」永远失败、凭证永不落盘

**根因**：`SettingsActivity.ProbeTask` 跑在后台线程，却直接调了
`act.vaultInput.setText(detected)`。`TextView.setText` → `requestLayout()` →
`ViewRootImpl.checkThread()` → 抛 `CalledFromWrongThreadException` →
被外层 `catch (Exception e)` 吞掉 ⇒ 判定成「失败」⇒ `saveIfOk()` 永远不执行。

用户看到的现象：**「测试并保存」永远失败，填好的账号密码存不进去。**

**修法**：
- 输入框的值在**主线程**上取（`testConnection()` 里），后台线程不再碰 View；
- `setText` 挪到 `StatusTask`（走 `runOnUiThread`）里；
- 探测出来的 vault 位置**直接传回** `StatusTask`，不再靠回读输入框
  （原来回读的是用户**手填的旧值**，不是探测结果）。

**护栏**：`android/_test_apk_contract.py` [12] 断言「ProbeTask 里没有 setText」。

### B2　用了 `java.util.Base64`（API 26 才有），而 minSdk 是 24

**硬证据**：SDK 自带 `platforms/android-34/data/api-versions.xml` 里
`<class name="java/util/Base64" since="26">`；构建脚本是 `--min-sdk-version 24`。

d8 **不会拦**它（只在 `--min-api` 之下给个警告），编译能过 ——
但 Android 7.0/7.1 上第一次网络调用就 `NoClassDefFoundError`，
而 `basicAuth()` 被 GET/PUT/DELETE/PROPFIND/MKCOL **所有路径**调用 ⇒ 整个应用不可用。

**修法**：自己写 20 行 Base64。理由：
- `java.util.Base64` → API 26 才有；
- `android.util.Base64` → API 8 就有，但它是 Android 专有的，
  **桌面 JVM 上跑不了**（我们用假 WebDAV 服务器对拍测试）；
- 自己写：两边都能跑，且不绑任何 API 级别。

**护栏**：`[11]` 断言没有 `java.util.Base64`；`android/TestDav.java` [9]
拿 JDK 的 `java.util.Base64` 对拍 8 组样本（含中文、长度 0/1/2/3/4 的边界）。

### B3　同步按钮判断的是**永远不成立**的值

```js
if (state.syncMode === "dir") { ... }   // syncMode 从来没有 "dir" 这个取值
```

`syncMode` 的实际取值只有 `"local"` / `"fsa"` / `"dav"` / `"none"`。
⇒ 点顶栏「同步」在 APK 里**只会再弹一次设置页**，真正的
`pullFromDir()` / `pushToDir()` 永远走不到。

**修法**：改用 `currentBackend()` 判断（后端抽象层本来就该这么用）。

**护栏**：[15] 断言 `state.syncMode === "dir"` 不存在 + 处理器里有
`currentBackend()` / `pullFromDir` / `pushToDir`。

### B4　`pushToDir()` 在 APK 里**永远不会被调用** ⇒ 双向同步其实是单向

全仓只有 3 处引用，其中 2 处都在 B3 那条死分支里。
`saveCurrent()` 只写 IndexedDB 并打 `dirty=1`，**不上传**。

后果链：平板编辑 → 只存本地 → 下次 pull 发现云端版本不同 →
走冲突分支 → **每改一次就多出一份「冲突合并」副本**。

**修法**：`saveCurrent()` 末尾在有后端时自动 `pushToDir(true)`（静默推送，
成功/失败分别给明确提示）；`pushToDir` 现在返回 `{ok, fail}`。

---

## 3. 重要级修复

| # | 问题 | 修法 |
|---|---|---|
| I1 | 裸 socket **不做 TLS 主机名验证** ⇒ 应用密码可被中间人截获 | 改成 `createSocket()`（无参）→ `setSSLParameters(endpointIdentificationAlgorithm=HTTPS)` → `connect()`；握手后再用 `getDefaultHostnameVerifier()` 验一次。**注意顺序**：用 `createSocket(host,port)` 反而是错的 —— 那个版本内部就完成了握手，那时再设参数来不及，主机名验证等于没开 |
| I4 | 整篇 PDF 一次性进 Java 堆，无上限 ⇒ 大文件 OOM | `Content-Length` 预检 + 累计字节上限 128 MB；缓冲区按 Content-Length 预分配（省掉反复 realloc）。实测最大 PDF 9.28 MB，128 MB 绰绰有余 |
| I5 | `readChunked` 的 chunk-size 直接来自网络 ⇒ `7FFFFFFF` 触发 OOM（**Error**，`catch (Exception)` 抓不住，线程直接死） | 加单块/总量上限；畸形 chunk-size **明确抛 IOException** 而不是静默 `break`（静默截断的话上层只看到「DAV error」，查不出原因） |
| I6 | 无 Content-Length 且非 chunked ⇒ body 被当成空，PROPFIND 解析出 0 条，页面显示「目录为空」（与真空目录无法区分） | 改成读到 EOF |
| I7 | vault 探测遍历整朵云盘 + **下载整篇 PDF** 做测试 ⇒「测试并保存」卡几分钟 | 探测加 PROPFIND 预算（24 次）+ 优先命中像 vault 的目录名；下载测试改成只读文件头 64 KB |
| I8 | `allowBackup="true"` ⇒ 含应用密码的 SharedPreferences 会被备份上云 | 改 `false` |
| I9 | WebView 从不销毁 + 不暂停 ⇒ Activity 泄漏、切后台还在跑 | `onDestroy` 里 `removeJavascriptInterface` + `destroy()`；补 `onPause`/`onResume` |
| I10 | 每次写文件 `new Thread` ⇒ 批量推 24 篇 = 24 条 TLS 连接；`nativeCall` 没超时 ⇒ Promise 永挂 | 改单线程池；JS 侧加 150 s 超时兜底 |
| I11 | **AI 功能在 APK 里必被 CORS 打死**（origin 是 `https://appassets.androidplatform.net`，外部 API 不会回这个 origin） | 新增原生代理 `MicroBench.httpPost(url, body, auth, cbId)`；网页在 APK 里改走原生代理（非流式，一次性拿回完整 SSE 文本再解析） |
| I12 | `URLDecoder` 把 `+` 解成空格 ⇒ 文件名含 `+` 的论文**永远打不开** | 解码前先把 `+` 换成 `%2B` |
| — | `parts[1]` 在 try 外（审查报告误报） | 实际就在 try 里，无需改 |

---

## 4. 顺手修的

- **导出器占位符顺序**：原来 `out.replace("__DATA__", data_json)` 在**内联 CSS/JS 之后**。
  只要内联的 app.js 里出现 `__DATA__` 这个字面量（哪怕只是注释里提了一句），
  就会被整份数据 JSON 顶掉 —— 实测导出的 HTML 从 72 KB 涨到 332 KB 且 JS 全废。
  **现在占位符先替换、再内联**；app.js 的注释里保留 `__DATA__` 字样当**金丝雀**。
- **导出测试会先重新导出再检查**：以前直接读仓库根目录那份 HTML（上次构建的旧产物），
  改了 app.js 忘了重导出，测试照样全绿，交付给用户的却是老版本。
- `<input type="file">` 回传：`onShowFileChooser` + `REQ_FILE` + `onActivityResult`，
  失败路径也回调 `null`（不清掉旧回调 → `<input>` 永久卡死）。
- 物理返回键：关 PDF 阅读器 → 关弹窗 → 网页后退 → 退出应用。
- `onReceivedSslError` 只放行 `SSL_UNTRUSTED` + 主机精确匹配（原来任何错误类型都放行）。
- `allowFileAccess` / `allowContentAccess` 关掉（本应用用不到）。
- 裸 socket 的 dav 根只允许 https（**回环地址豁免** —— 测试服务器就在本机）。
- `Mkdirs` 的错误不再被吞掉（吞掉的话 PUT 只报一个看不懂的 409）。
- `SettingsActivity` 的 `configChanges` 补齐（旋转/深色模式不再丢输入）。
- 页面侧去掉重复的 `btn-connect-dir` 赋值。

---

## 5. 验证情况

### 5.1 自动化测试（9 个套件，全绿）

| 套件 | 结果 |
|---|---|
| `pytest tests -m "not slow"` | **192 passed** |
| `microbench/_test_regressions` | 40 passed |
| `microbench/_test_lan` | 39 passed |
| `microbench/_test_tablet_sync.py` | 32 passed |
| `microbench/_test_tablet_renderer.js`（node） | 38 passed |
| `microbench/_test_tablet_export.py` | 41 passed |
| `microbench/_test_offline_export.py` | 17 passed |
| `android/_test_dav.py`（假 WebDAV 对拍） | **35 passed**（原 24，+11） |
| `android/_test_apk_contract.py` | **68 passed**（原 43，+25） |

合计 **502 项断言**。

### 5.2 APK 拆包验证（只读，实测产物）

```
package: microbench.tablet  versionCode=1  versionName=1.0
minSdkVersion: 24   targetSdkVersion: 34
allowBackup = false
configChanges = 0x1fa0（两个 Activity 都是）
assets/index.html = 2500 KB

classes.dex 字符串扫描：
  setEndpointIdentificationAlgorithm  True
  getDefaultHostnameVerifier          True
  onShowFileChooser / onBackPressed    True
  httpPost / getPartial / isLoopback   True
  java/util/Base64                    False   ← API 26 依赖已清除
```

### 5.3 构建

```
aapt2 compile → aapt2 link (-A assets) → javac (-source 8 -target 8
  -bootclasspath android-34/android.jar) → d8 (build-tools 35.0.1)
  → zipalign → apksigner
产物: MicroBench-平板工作台.apk  0.89 MB
签名证书 SHA-256: ac50efaf39045a110f60e9922848f0ad61cf689dc59c3ebd2b5de2731e964a55
```

⚠ **签名证书与上一版一致** ⇒ 直接覆盖安装即可，**不用卸载**（数据不丢）。

---

## 6. 还没验证到的（真机边界）

本机没有 emulator / ADB，也没有真实坚果云账号，所以下面这些**只在静态分析与
模拟环境里验过**，需要你在平板上实测：

1. **真机 TLS**：坚果云证书链能不能在 Android 的信任库里通过。
   代码路径已验（顺序、参数、双保险），但没在真设备上握过手。
2. **vault 自动探测**：真实云盘里「我的坚果云/MicroBench工作台」那两层的
   PROPFIND 往返时间与返回格式。探测有 24 次预算兜底，失败时设置页可手填。
3. **AI 原生代理**：MiniMax 的 SSE 响应经原生代理回来后能否正确拼出正文。
   浏览器路径是流式，APK 路径是一次性。
4. **大 PDF**：9.28 MB 那篇在平板上的打开耗时与内存峰值。
5. **坚果云 WebDAV 的实际响应细节**：`Content-Length` 是否总在、
   `Depth: 1` 的行为、chunked 用不用得上。

### 平板上的第一次操作顺序（建议照着走）

1. 装 APK（覆盖安装即可）→ 打开。
2. 顶栏应显示「坚果云」（如果之前配过，**启动会自动拉一次云盘**）。
3. 没配过：点「配置坚果云」→ 填**邮箱**和**应用密码**（不是账号密码）→
   点「**测试并保存**」→ 提示「连接正常, 已保存」并自动关页。
   - 若探测不出 vault 位置，设置页第三个输入框可手填
     `我的坚果云/MicroBench工作台`。
4. 回到工作台 → 点「📄 文献库」看 24 篇原文是否都在
   （现在清单是**从云盘实时扫**的，云盘新加的论文也会立刻出现）。
5. 点任意一篇 → PDF 阅读器打开 → 划词 → 右侧「AI 解读 / 翻译 / 提问」。
6. 改一篇笔记 → 保存 → 应提示「已保存并写回云盘」。

---

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `android/java/microbench/tablet/MainActivity.java` | WebView 外壳、请求拦截、文件选择、返回键、生命周期 |
| `android/java/microbench/tablet/DavClient.java` | WebDAV 客户端（裸 socket PROPFIND/MKCOL、TLS、Base64、chunked） |
| `android/java/microbench/tablet/DavPathHandler.java` | `/dav/list/*` `/dav/file/*` → JSON / 字节 |
| `android/java/microbench/tablet/DavBridge.java` | JS 桥：写/删云盘文件 + `httpPost` AI 代理 |
| `android/java/microbench/tablet/SettingsActivity.java` | 原生设置页（凭证只留原生侧） |
| `android/java/microbench/tablet/Prefs.java` | SharedPreferences 凭证存储 |
| `android/build_apk.ps1` | 手工打包（不走 Gradle） |
| `android/_test_dav.py` + `TestDav.java` | 假 WebDAV 对拍（35 项） |
| `android/_test_apk_contract.py` | 源码契约护栏（68 项） |
| `microbench/static/tablet/app.js` | 平板工作台逻辑（后端抽象、PDF 阅读器、AI、同步） |
| `microbench/export_offline.py` | 单文件导出（`--tablet`） |

---

## 8. 部署提醒

- 签名密钥 `android/microbench-tablet.jks` **不在版本库里**（有意如此），
  口令明文写在 `android/build_apk.ps1` 里。**丢了就升不了级**（系统会拒绝
  覆盖安装），换设备前记得单独备份这个文件。
- vault 已在坚果云（8 个顶层目录全是 junction），**不需要再搬迁**。
- 坚果云同步不传 PDF 本体（`SYNC_PDF_BODY=0`），因为 vault 本体已在云端；
  平板上的原文 PDF 是**点开时按需从云盘下载**，另有「缓存全部原文」做离线阅读。
