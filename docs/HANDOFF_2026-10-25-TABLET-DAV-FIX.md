# 交接说明：平板端「坚果云同步 0 篇 / PDF 全 404」的根因与修法（2026-10-25 晚）

> **给另一台设备（25353）的 agent**：这台机（30461）负责平板侧
> （`microbench/static/tablet/app.js` + `android/java/…`，APK 从这儿打）。
> 这轮修的是**用户真机反馈的阻断故障**，不是可选优化。
>
> 交接走仓库 → 对方 `git pull` 即可拿到。

---

## 0. 一句话

**平板端 WebDAV 的 vault 前缀探错了 —— 探到了「我的坚果云/MicroBench同步」
（同步根），而不是「我的坚果云/MicroBench工作台」（vault）。
于是 24 篇原文 PDF 全部 404、笔记同步永远是「0 篇」，
而界面显示「已连接坚果云」—— 因为它只看账号密码对不对。**

> ⚠ 前两轮都修错了地方：前两轮修的是「前缀为空」和「前缀配错」，
> 但**探测本身**就会选错 —— 因为 vault 和同步根是**兄弟目录、名字都含 MicroBench**，
> 而坚果云按名字排序，「同」永远排在「工」前面。
> 真凶见 [§5](#5-第三轮才找到的真凶探测选中了同步根2026-10-25-深夜)。

已修：**探测靠内容不靠名字 + 原生自愈 + 网页不再沉默 + 界面说真话**。

---

## 1. 故障现场（用户真机截图，别当成用户操作问题）

| 界面显示 | 实际 |
|---|---|
| 「坚果云 WebDAV（真双向）」 | 账号密码齐了，但 **vault 路径是空的** |
| 「已连接 0 篇 · 新增 0 · 更新 0」 | PROPFIND 打到了**不存在的目录**，服务器回 **207 + 0 个条目** |
| 24 篇 PDF 全部 404 | GET 打到「我的坚果云/01_Literature/xxx.pdf」，那里没有东西 |
| 文献库「⚠ 还没连接文件夹」 | 判据写错了（DAV 模式下 `state.dirHandle` 永远是 null）|
| 设置页「已连接坚果云」 | 只由 `davConfigured()`（账号密码齐没齐）推导，跟路径无关 |

**最阴的一点**：前缀错时 PROPFIND **不报 404**，回的是 **207 + 空集合**。
所以网页看到的是「目录是空的」，而不是「路径不对」——
界面显示「已连接 0 篇」，**一点错都看不出来**。
用户只会以为「坚果云还没同步完」，反复重试、反复失败。

---

## 2. 为什么 vault 前缀会是空的

它**只有**原生设置页的「测试并保存」会写（`Prefs.setDavVaultRoot`）。
以下任一条成立，它就是空的：

- 用户从没进过原生设置页（那个入口藏在「配置坚果云」→ 原生 Activity 里）；
- 用户装的是**加这个字段之前**的 APK（老用户的 `dav_vault_root` 根本不存在）；
- 探测失败 / 预算用尽（`MAX_PROPFIND_CALLS = 12`）→ 存了空串。

而网页侧**没有任何自愈**：空前缀 → 一直 404。

---

## 3. 修法（三层，缺一不可）

### ① 原生自愈（`Prefs` + `DavPathHandler`）

```java
// Prefs.java
public synchronized void resolveRootsIfMissing() {
    if (!davConfigured()) return;
    if (!davVaultRoot().isEmpty()) return;      // 已配好就不动
    DavClient c = new DavClient(davUser(), davPass(), davRoot());  // ⚠ 不带前缀
    String v = c.detectVaultRoot();
    if (v == null || v.isEmpty()) return;
    setDavVaultRoot(v);
    String s = c.detectSyncRoot(v);
    if (s != null && !s.isEmpty()) setDavSyncRoot(s);
}
```

```java
// DavPathHandler.java —— 每个请求前兜底一次
private void ensureRoots() {
    if (rootsResolved) return;
    if (!prefs.davVaultRoot().isEmpty()) { rootsResolved = true; return; }
    long now = System.currentTimeMillis();
    if (now - lastProbeAt < PROBE_RETRY_INTERVAL_MS) return;   // 5 分钟
    lastProbeAt = now;
    prefs.resolveRootsIfMissing();
    if (!prefs.davVaultRoot().isEmpty()) rootsResolved = true;
}
```

⚠ **两个坑**：
- 探测**必须用不带前缀的客户端**（带了就变成「我的坚果云/我的坚果云/…」）；
- 探测**失败不能置位** —— 一次网络抖动就永久放弃的话，
  老用户还是永远坏着。改成 5 分钟后可再试。

### ①之补：前缀「非空但配错了」也要能自愈（第二轮加的）

上面那层只管「前缀是**空**的」。但还有一类：**前缀非空、但是错的**：

- 用户在原生设置页**手填错**了；
- 云盘里**改过目录名**（vault 搬家）；
- 用户装的是**探测逻辑有 bug 的旧版 APK**，把错的位置存下来了。

这类情况下 `ensureRoots()` 看到「非空」就直接放行 → **症状跟空前缀一模一样**
（列目录 0 条、PDF 全 404），界面照样显示「已连接 0 篇」。

所以再加一层，**接在真实的两条故障路径上**：

```java
// Prefs.java —— 不管原来配的是空还是错，都重探一次
public synchronized DavClient redetectVaultRoot() {
    if (!davConfigured()) return null;
    DavClient c = new DavClient(davUser(), davPass(), davRoot());  // ⚠ 仍不带前缀
    String v = c.detectVaultRoot();
    if (v == null || v.isEmpty()) return null;
    if (v.equals(davVaultRoot())) return null;   // 位置没变 → 不是路径问题
    setDavVaultRoot(v);
    c.setVaultRoot(v);
    String s = c.detectSyncRoot(v);             // 同步根是兄弟目录，一起刷
    if (s != null && !s.isEmpty()) setDavSyncRoot(s);
    return c;
}
```

```java
// DavPathHandler.java
// 列目录返回空 → 重探一次再列
if (entries.isEmpty()) {
    DavClient fixed = tryRedetect(syncRoot);
    if (fixed != null) entries = fixed.list(dir);
}
// 取文件 404 → 重探一次再取
if (data == null) {
    DavClient fixed = tryRedetect(syncRoot);
    if (fixed != null) data = fixed.get(path);
}
```

⚠ **重探测必须节流**（`PROBE_RETRY_INTERVAL_MS = 5 分钟`）：
重探测要**递归整个云盘**（好几次 PROPFIND）——
要是「每个空目录都探一次」，几秒就能把坚果云打到 `503 BlockedTemporarily`。
→ 5 分钟内最多重探一次。

⚠ **同步根也要一起刷新**：同步根（译文快照 / 批注）是 vault 根的**兄弟目录**，
vault 位置变了它可能跟着变 —— 不刷的话译文还是读不到。

### ①之补之补：怎么在普通电脑上测「真跑在手机上的那条链路」

`DavPathHandler` / `Prefs` 依赖 Android 框架类，普通 JDK 编译不了 →
以前只能靠真机点。这次加了**三个桩类**：

```
android/_stub/android/content/Context.java
android/_stub/android/content/SharedPreferences.java
android/_stub/android/content/MemorySharedPreferences.java
android/_stub/android/webkit/WebResourceResponse.java
```

`android/TestDavPath.java` 靠它们，把 **DavPathHandler 真正跑一遍**（不是模拟）。

⚠⚠ **桩类绝对不能挪进 `android/java/`** ——
`build_apk.ps1` 会把 `android/java` 全编译一遍，桩类跟真 `android.jar` 撞类，
**APK 直接编不过**。`android/_test_apk_contract.py` [22] 就是钉这一条的。

### ② 网页不再沉默（`app.js` 的 `scanRemote`）

```js
let entriesSeen = 0;
// …每次 backend.list(dir) 之后 entriesSeen += entries.length
if (entriesSeen === 0) {
  throw new Error("云盘里没找到 vault 目录" + (vr ? "（当前 vault 位置：" + vr + "）" : "（vault 位置没定）")
    + "。请点设置里的「配置坚果云」重新测一次并保存。");
}
```

**空结果 ≠ 成功**。这条是本次修复的核心。

### ③ 界面说真话

- `DavBridge.davInfo()` 新增：返回 `{configured, vaultRoot, syncRoot}`；
- 网页 `davVaultRootLabel()` 读它，设置页**亮出 vault 的真实位置**；
- 文献库改用 `currentBackend()` 判断（**不能**用 `state.dirHandle` ——
  DAV 模式下它永远是 null）；
- PDF 打不开时，错误里带上 vault 位置。

---

## 4. 测试（这次全靠它才发现）

| 文件 | 钉住什么 |
|---|---|
| `android/_test_dav.py` + `TestDav.java` [14] | 用**真实 Java 客户端**对 mock WebDAV **复现故障**（列目录空 + 下载 404），再验自愈路径 |
| `android/TestDavPath.java` [15]-[20] | **真正跑 `DavPathHandler`**（靠 `_stub/` 垫 Android 类）：空前缀自愈 / 错前缀自愈 / PDF 404 自愈 / 空目录不误报 / 节流 / 没配账号给可读错误 / **兄弟目录陷阱** |
| `microbench/_test_tablet_dav.js` | 网页侧三条护栏（0 条目报错 / 界面显示位置 / 文献库不谎报）|
| `android/_test_apk_contract.py` [20][21][22] | 原生接线（探测 / 存回 / 只探一次 / davInfo 存在）+ 错前缀自愈接在**两条路径**上 + 桩类没混进 APK |

`TestDav.java` [14] 的关键断言（**这是复现，不是假设**）：

```
PASS  前缀为空时列目录**不报错、返回空**（用户看到的「0 篇」）
PASS  前缀为空时下载拿不到文件（404）—— 24 篇原文全挂在这里
PASS  能自动探测出 vault 位置: 我的坚果云/MicroBench工作台
PASS  自愈后能列出 vault 内的条目
PASS  自愈后能下载 vault 内的 PDF
```

`TestDavPath.java` 的关键断言（**跑的是手机上那条链路**）：

```
PASS  自愈后列出了条目（不再是空数组）
PASS  错前缀也能自愈列出条目
PASS  错前缀被改成了正确位置: 我的坚果云/MicroBench工作台
PASS  同步根也跟着刷新了（译文快照要靠它）: 我的坚果云/MicroBench同步
PASS  取文件返回 200（不是 404）
PASS  拿到的正是 vault 里那份 PDF: PDF-BYTES
PASS  空目录只触发了 1 次重探测（之后被节流）
```

> **反向验证**：把 `tryRedetect` 临时改成直接 `return null`（等于拆掉自愈），
> `TestDavPath` 立刻变成 **PASS 11 / FAIL 7**，红的正是
> 「错前缀自愈」和「PDF 404」那几条 ——
> 证明这些断言**真的在测修复**，不是空转。

---

## 5. 第三轮才找到的真凶：探测选中了**同步根**（2026-10-25 深夜）

用户装完前两版还是不行，截图里有一行是决定性的：

> 当前 vault 位置：**我的坚果云/MicroBench同步**

**那不是 vault，那是同步根**（放译文快照 / 批注的那个兄弟目录）。

### 真实云盘结构（本机 Nutstore 实测）

```
我的坚果云/
  ├── MicroBench工作台/      ← vault（PDF 原文、笔记）
  │     00_Dashboard  01_Literature  02_Reproduction
  │     03_Knowledge  04_Weekly_Reports  _Templates  .obsidian  assets
  └── MicroBench同步/        ← 同步根（译文快照、批注）
        snapshots  annotations  files  progress
```

**两个是同一层的兄弟目录，名字共享 `MicroBench` 前缀。**

### 为什么 100% 必错（不是偶发）

老实现的判据是**名字**：`isLikelyVaultName()` = 名字含 "microbench" 或 "工作台"。

而坚果云的 PROPFIND **按名字排序**：
「同」= U+540C < 「工」= U+5DE5 →
**「MicroBench同步」永远排在「MicroBench工作台」前面 → 永远先被选中。**

于是：
- 自愈把 `我的坚果云/MicroBench同步` 存成了 vault 位置；
- 列 `…/MicroBench同步/01_Literature` → 207 + 0 条 → 「0 篇」；
- 取 PDF → 404 → 「云盘里没找到这个文件」；
- 而界面显示「已连接」（只看账号密码）。

### 修法：名字只用来"值得一看"，必须验证内容

```java
// 名字像的候选 → 列它，看里面有没有 vault 的标志目录
for (DavEntry e : dirs) {
    if (!isLikelyVaultName(e.name)) continue;
    if (isVaultDir(e.path)) return e.path;     // ← 内容校验
}
```

判据要**成对**定义（只判「像 vault」会认错方向）：

| 类别 | 标志目录 | 用途 |
|---|---|---|
| vault | `00_Dashboard` `01_Literature` `02_Reproduction` `03_Knowledge` `04_Weekly_Reports` `_Templates` | 认它 |
| 同步根 | `snapshots` `annotations` | **排除**它 |

⚠ 同步根里全是 .md（译文快照就是 .md）→ 「有没有 .md」**不能**当 vault 判据；
递归进同步根之前要先按「含 snapshots/annotations」挡掉，
否则会返回 `…/MicroBench同步/snapshots` 这种更离谱的路径。

### ⚠⚠ 这一轮真正的教训：**mock 太善良 = 假绿**

前两轮的 mock 树里**只有 vault、没有同步根**，
而且 PROPFIND 按 `FILES` 字典的**插入顺序**返回条目
→ 恰好把对的那个排在前面 → 测试全绿 → 真机全坏。

改成真实结构（加同步根兄弟目录 + **按名字排序**）之后，
**旧代码立刻从 `48 PASS / 22 PASS` 变成 `40 PASS / 8 FAIL` + `12 PASS / 10 FAIL`**，
红的正是：

```
FAIL  探测到的是正确位置: 我的坚果云/MicroBench同步     ← 和用户截图一模一样
FAIL  取文件返回 200（不是 404）: 404                    ← 24 篇 PDF 全挂
FAIL  自愈后能列出 vault 里的条目: []                     ← 同步 0 篇
```

> **mock 必须照抄真实结构**：目录层级、兄弟关系、**排序规则**都要一样。
> 少一样，测试就是假绿。

---

## 6. 给对方的两条提醒


1. **桌面端别改 `android/`**（除非你也在改平板侧）——
   这套原生代码两台设备共用，但**只有 30461 这台负责重打 APK**。
   改完记得在 30461 上 `pwsh android/build_apk.ps1`。
2. **交付前跑一遍 `python android/_test_dav.py`** ——
   它会真的起一个 mock WebDAV 并让 Java 客户端打一遍。
   改了 `DavClient` / `Prefs` / `DavPathHandler` 必须跑。

> ⚠ **顺带修的跨设备坑**：`android/_test_dav.py` 原来把 JDK 路径写死成
> 「`D:\Desktop\杂物\z\Zotero\jdk`」—— **那是 30461 这台的路径**。
> 25353 上没有这个目录 → javac 直接失败 → 这个套件红 →
> **25353 的 auto_sync 永远判定「测试失败」，再也推不上去**，
> 而且那个红是环境问题、不是代码问题。
> → 现在改成多路查找（`MB_JDK` → `JAVA_HOME` → 常见安装位置 → PATH），
> 实在找不到就 **SKIP（不算失败）**。

---

## 7. 通用教训（值得抄进你自己的代码）

1. **「HTTP 200」和「有东西」是两回事** —— 空数组 / 空列表 / 0 条目，
   在 WebDAV、搜索、分页接口里**都是失败**。
   → **凡是「同步/拉取」的结果是空的，按失败处理**。
2. **界面上的一个布尔值会骗人** —— 「已连接」如果只由「账号密码齐了没」推导，
   它跟「路径对不对」「能不能读到」毫无关系。
   → 状态要显示**可核对的细节**（路径、条数）。
3. **静默失败比崩溃更贵** —— 崩溃用户会来问；静默返回空，
   用户只会自己反复重试，然后说「这东西根本没法用」。
4. **只有深层设置页会写的配置项，必须有运行期自愈** ——
   否则老用户升级后永远坏着，而且没人知道为什么。
5. **「兜底」要分「没配」和「配错了」两种** —— 只兜「没配」的话，
   手填错 / 搬过家 / 旧版 bug 存错值的用户，症状一模一样，
   但兜底根本不触发。→ **兜底要接到「结果异常」上，而不是接到「配置缺失」上**：
   列目录为空、文件 404 → 这才是用户看得见的信号。
6. **自愈动作要节流** —— 「重探一次」如果是全盘递归，
   每个空目录都探一次，几秒就能把云盘打到限流。
7. **测试要能「反向验证」** —— 把修复临时拆掉，测试必须变红。
   全绿的测试可能只是**根本没测到东西**。
8. **跨设备脚本别写死本机路径** —— 写死了就是「在对方机器上永远红」，
   而且红的原因跟代码无关，最难查。

