# 平板 APK 的签名密钥（**单点风险**）

> 2026-11-23 独立审查那一轮发现：**生成现有 APK 的那把私钥已经不在本机了**。
> 这份文档把事实和判断方法固定下来，免得下次再翻一遍。

---

## 1. 事实

| 项 | 值 |
|---|---|
| 密钥文件 | `android/microbench-tablet.jks`（**已被 `.gitignore` 忽略**，只在构建机本地） |
| 口令 / alias | 见 `android/build_apk.ps1`（`$KeyPass` / alias `microbench`） |
| **平板上已装版本**（11-18 构建）的证书 SHA-256 | `ac50efaf39045a110f60e9922848f0ad61cf689dc59c3ebd2b5de2731e964a55` |
| SHA-1 | `d7e0fadde5578914eb9709b589c555572a0e4258` |
| 现状 | ⚠ **`.jks` 文件在本机已找不到**（2026-11-23 实测：仓库内、`D:\Android\`、用户目录都没有） |

## 2. 后果（为什么不能随便重打）

`android/build_apk.ps1` 在**首次构建时会自动生成一把新密钥**（第 135~138 行 `keytool -genkeypair`）。
所以一旦重打：

- 新 APK 的证书指纹会**和上面那个不一样**
  → Android 校验签名不通过 → **不允许覆盖安装**
  → 平板上必须**先卸载旧版**再装 → **本地笔记 / 批注 / 同步状态会丢**
- 这不是「重打引入的新问题」——**旧密钥已经丢了，下一次构建一定会换证书**。
  区别只在于：早重打早知道，晚重打晚知道。

## 3. 怎么判断一个 APK 是不是同一把钥匙签的

```powershell
& 'D:\Desktop\杂物\z\Zotero\jdk\bin\java.exe' `
  -cp 'D:\Android\Sdk\build-tools\35.0.1\lib\apksigner.jar' `
  com.android.apksigner.ApkSignerTool verify --print-certs 'MicroBench-平板工作台.apk'
```

看 `Signer #1 certificate SHA-256 digest`：

- **等于** `ac50efaf…64a55` → 同一把钥匙 → **可以覆盖安装**，放心重打；
- **不等于** → 已经换过了 → 平板必须卸载重装（或找回旧密钥）。

## 4. 恢复顺序（推荐）

1. **先找旧密钥**：备份盘 / 旧电脑 / 密码管理器里如果有 `microbench-tablet.jks`，
   拷回 `android/microbench-tablet.jks` → 证书不变 → 平板可覆盖安装。
2. 找不到 → 接受换证书：重新构建 → 平板上**先卸载旧版**（记得先把本地笔记导出/同步到云盘）
   → 再装新的。

## 5. 交付物一致性（不要只重打一半）

`tests/test_apk_delivery.py` 要求 **APK 里的 `assets/index.html` 与仓库根目录
`MicroBench_平板工作台.html` 逐字节相同**。
`build_apk.ps1` 已经处理好了：它**只导出一次**，然后把同一份复制到根目录（第 60~66 行注释）——
所以**要么两个一起重打，要么都别动**。
