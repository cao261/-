#!/usr/bin/env pwsh
# =====================================================================
#  构建 MicroBench 平板工作台 APK
#
#  刻意**不用 Gradle** —— 那要拉一整套 Maven 依赖，慢且容易卡在网络上。
#  手工打包只要四步工具，全在 Android SDK 的 build-tools 里：
#      aapt2 compile/link  ->  资源 + manifest + 生成的 R.java
#      javac + d8          ->  .class -> classes.dex
#      zipalign            ->  4 字节对齐
#      apksigner           ->  签名
#
#  用法:
#      pwsh android/build_apk.ps1
#      pwsh android/build_apk.ps1 -Out "D:\somewhere\MicroBench.apk"
# =====================================================================
param(
    [string]$Out = "",
    [string]$Sdk = "D:\Android\Sdk",
    [string]$Jdk = "D:\Desktop\杂物\z\Zotero\jdk",
    # build-tools 34 自带的 d8 是 8.2.2 (2023), 读 javac 21 生成的 class
    # 会抛 NullPointerException。35.0.1 自带的 d8 没问题。
    [string]$BuildTools = "35.0.1",
    # 构建暂存目录。必须是**纯 ASCII 路径** —— aapt2 是原生 Windows 程序,
    # 用 ANSI 打开路径, 工作区里带中文就报「系统找不到指定的文件」。
    [string]$Stage = "D:\Android\mbbuild"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path      # android\
$Repo = Split-Path -Parent $Root                            # 工作台根
$Build = $Stage
$Bt = Join-Path $Sdk "build-tools\$BuildTools"
$AndroidJar = Join-Path $Sdk "platforms\android-34\android.jar"
$KeyStore = Join-Path $Root "microbench-tablet.jks"
$KeyPass = "microbench2026"

function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------------
Step "准备"
foreach ($p in @($Bt, $AndroidJar, $Jdk)) {
    if (-not (Test-Path $p)) { throw "缺少: $p" }
}
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force -Path $Build, "$Build\gen", "$Build\classes", "$Build\dex", "$Build\assets" | Out-Null

# 1) 生成 assets 里的平板工作台单文件 (直接落到 ASCII 暂存目录)
Write-Host "  导出平板工作台单文件..."
& python (Join-Path $Repo "microbench\export_offline.py") --tablet "$Build\assets\index.html" --quiet
if ($LASTEXITCODE -ne 0) { throw "导出失败" }
$assetSize = [math]::Round((Get-Item "$Build\assets\index.html").Length / 1MB, 2)
Write-Host "  assets/index.html  $assetSize MB"

# 2) 图标
Write-Host "  生成图标..."
& python (Join-Path $Root "_make_icons.py")
if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }

# 2b) 把 res / manifest / java 复制到 ASCII 暂存目录
Write-Host "  暂存到 ASCII 路径: $Build"
Copy-Item -Recurse -Force (Join-Path $Root "res") "$Build\res"
Copy-Item -Force (Join-Path $Root "AndroidManifest.xml") "$Build\AndroidManifest.xml"
Copy-Item -Recurse -Force (Join-Path $Root "java") "$Build\java"

# 3) 编译资源
Step "aapt2 compile"
& "$Bt\aapt2.exe" compile --dir "$Build\res" -o "$Build\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile 失败" }

Step "aapt2 link (资源 + manifest + assets -> base.apk + R.java)"
# -A 把 assets 目录整个打进 APK —— 平板工作台的单文件 HTML 就放在这儿。
# 漏掉它会得到一个 50KB 的空壳 APK。
& "$Bt\aapt2.exe" link `
    -o "$Build\base.apk" `
    -I $AndroidJar `
    -A "$Build\assets" `
    --manifest "$Build\AndroidManifest.xml" `
    --java "$Build\gen" `
    --min-sdk-version 24 `
    --target-sdk-version 34 `
    --version-code 1 `
    --version-name "1.0" `
    "$Build\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 link 失败" }

# 确认 assets 真的进去了
$apkSize = [math]::Round((Get-Item "$Build\base.apk").Length / 1MB, 2)
Write-Host "  base.apk  $apkSize MB"
if ($apkSize -lt 0.4) { throw "base.apk 太小 —— assets 没被打进去？" }

# 4) 编译 Java
Step "javac"
$sources = @()
$sources += (Get-ChildItem "$Build\java" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
$sources += (Get-ChildItem "$Build\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
& "$Jdk\bin\javac.exe" -encoding UTF-8 -source 8 -target 8 `
    -bootclasspath $AndroidJar -cp $AndroidJar `
    -d "$Build\classes" @sources
if ($LASTEXITCODE -ne 0) { throw "javac 失败" }
Write-Host "  编译了 $($sources.Count) 个源文件"

# 5) dex
#    d8.bat / apksigner.bat 是批处理, 会用 ANSI 读 JAVA_HOME ——
#    路径里有中文就变成乱码, 报 "JAVA_HOME is set to an invalid directory"。
#    直接用 java -cp 调它们的 jar, 绕开 bat。
Step "d8"
$classFiles = Get-ChildItem "$Build\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName }
& "$Jdk\bin\java.exe" -cp "$Bt\lib\d8.jar" com.android.tools.r8.D8 `
    --lib $AndroidJar --min-api 24 --output "$Build\dex" @classFiles
if ($LASTEXITCODE -ne 0) { throw "d8 失败" }

# 6) 把 dex 塞进 apk
Step "打包 dex"
& python (Join-Path $Root "_add_dex.py") "$Build\base.apk" "$Build\dex\classes.dex"
if ($LASTEXITCODE -ne 0) { throw "加 dex 失败" }

# 7) zipalign
Step "zipalign"
& "$Bt\zipalign.exe" -f -p 4 "$Build\base.apk" "$Build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign 失败" }

# 8) 签名
Step "签名"
if (-not (Test-Path $KeyStore)) {
    Write-Host "  首次构建: 生成签名密钥"
    & "$Jdk\bin\keytool.exe" -genkeypair -v `
        -keystore $KeyStore -alias microbench `
        -keyalg RSA -keysize 2048 -validity 10950 `
        -storepass $KeyPass -keypass $KeyPass `
        -dname "CN=MicroBench, OU=Personal, O=MicroBench, C=CN" | Out-Null
}
if (-not $Out) { $Out = Join-Path $Repo "MicroBench-平板工作台.apk" }
& "$Jdk\bin\java.exe" -cp "$Bt\lib\apksigner.jar" com.android.apksigner.ApkSignerTool sign `
    --ks $KeyStore --ks-pass "pass:$KeyPass" --key-pass "pass:$KeyPass" `
    --out $Out "$Build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "签名失败" }

Step "完成"
& "$Jdk\bin\java.exe" -cp "$Bt\lib\apksigner.jar" com.android.apksigner.ApkSignerTool verify --print-certs $Out | Select-Object -First 3
$finalMB = [math]::Round((Get-Item $Out).Length / 1MB, 2)
Write-Host "  APK: $Out  ($finalMB MB)" -ForegroundColor Green
Write-Host "  装到平板: 把这个文件传过去 -> 点它 -> 允许「安装未知来源应用」"
