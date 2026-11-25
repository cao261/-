# 平板 APK 的签名密钥（**单点风险**）

> **2026-11-25 更新：上一版文档的前提是错的，已更正。**
> 旧版写着「生成现有 APK 的那把私钥已经不在本机了、不敢重打 APK」——
> **实测不成立**：密钥就在 `android/microbench-tablet.jks`，证书指纹与
> 平板上已装版本**逐字相同** → 可以安全重打、**覆盖安装**、不丢本地数据。

---

## 1. 事实（2026-11-25 实测）

| 项 | 值 |
|---|---|
| 密钥文件 | `android/microbench-tablet.jks`（**已被 `.gitignore` 忽略**，只在构建机本地） |
| 口令 / alias | 见 `android/build_apk.ps1`（`$KeyPass` / alias `microbench`） |
| **平板上已装版本**（11-18 构建）的证书 SHA-256 | `ac50efaf39045a110f60e9922848f0ad61cf689dc59c3ebd2b5de2731e964a55` |
| SHA-1 | `d7e0fadde5578914eb9709b589c555572a0e4258` |
| 本机 `.jks` 的证书 SHA-256 | **同上，逐字一致** |
| 现状 | ✅ **密钥在本机，且与已装 APK 同证书** → 可覆盖升级 |

## 2. 旧版结论为什么是错的（别再踩）

2026-11-23 那一轮查的是「仓库内、`D:\Android\`、用户目录」，结论是
「私钥已丢失，不敢重打 APK」。**漏查的正是密钥真正所在的位置**：
`android/microbench-tablet.jks` 在**仓库里**，但因为 `.gitignore`
排除了 `*.jks`，**在别的设备 / 干净克隆上根本看不到这个文件** →
「本机找不到」很容易被误读成「私钥丢了」。

⚠ 更要命的是：`android/build_apk.ps1` 在**首次构建时会自动生成一把新密钥**
（第 135~138 行 `keytool -genkeypair`）→ 在「找不到旧钥匙」的机器上重打，
**证书就变了** → Android 拒绝覆盖安装 → 平板必须卸载重装 →
**本地笔记 / 批注 / 同步状态全丢**。

→ 所以每次构建前，**先按 §3 验一遍**，别凭印象。

## 3. 构建前必查：是不是同一把钥匙签的

```powershell
# ① 密钥文件在不在
Test-Path 'android\microbench-tablet.jks'

# ② 密钥自己的指纹
& 'D:\Desktop\杂物\z\Zotero\jdk\bin\keytool.exe' -list -v `
  -keystore 'android\microbench-tablet.jks' -storepass microbench2026 `
  -alias microbench | Select-String 'SHA256'

# ③ APK 的证书
& 'D:\Desktop\杂物\z\Zotero\jdk\bin\java.exe' `
  -cp 'D:\Android\Sdk\build-tools\35.0.1\lib\apksigner.jar' `
  com.android.apksigner.ApkSignerTool verify --print-certs 'MicroBench-平板工作台.apk'
```

看 `Signer #1 certificate SHA-256 digest`：

- **等于** `ac50efaf…64a55` → 同一把钥匙 → **可以覆盖安装**，放心重打；
- **不等于** → 证书已经换过 → 平板必须卸载重装（**先备份本地笔记**）。

## 4. 出问题时的恢复顺序

1. **先找旧密钥**：备份盘 / 旧电脑 / 密码管理器里如果有 `microbench-tablet.jks`，
   拷回 `android/microbench-tablet.jks` → 证书不变 → 平板可覆盖安装。
2. 找不到 → 接受换证书：重新构建 → 平板上**先卸载旧版**（记得先把本地笔记导出/同步到云盘）
   → 再装新的。

## 5. 交付物一致性（不要只重打一半）

`tests/test_apk_delivery.py` 要求 **APK 里的 `assets/index.html` 与仓库根目录
`MicroBench_平板工作台.html` 逐字节相同**。

`build_apk.ps1` 已经处理好了：它**只导出一次**，然后把同一份复制到根目录（第 60~66 行注释）——
所以**要么两个一起重打，要么都别动**。该护栏已挂在构建的最后一步
（`build_apk.ps1` 末尾会跑 `pytest tests/test_apk_delivery.py`），
**构建不过 = 这个 APK 不要发出去**。

## 6. 换密钥的正确做法（万一真要换）

```powershell
# ⚠ 别在构建脚本里悄悄生成新密钥 —— 先显式生成、记下指纹、写进本文件
& 'D:\Desktop\杂物\z\Zotero\jdk\bin\keytool.exe' -genkeypair -v `
  -keystore 'android\microbench-tablet.jks' -alias microbench `
  -keyalg RSA -keysize 2048 -validity 10950 `
  -storepass microbench2026 -keypass microbench2026 `
  -dname "CN=MicroBench, OU=Personal, O=MicroBench, C=CN"
```

换完密钥 → 本文件 §1 的指纹要更新 → 并且**必须**告诉用户：
平板上要卸载重装，**本地笔记/批注会丢**（先同步到云盘）。
