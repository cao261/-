$ErrorActionPreference = 'Stop'
$sdk = "D:\Android\Sdk"
$zip = "$env:TEMP\cmdline-tools.zip"
$jdk = "D:\Desktop\杂物\z\Zotero\jdk"

Write-Output "=== 解压 command-line tools ==="
$tmp = "$env:TEMP\cmdline-tools-x"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Join-Path $tmp "cmdline-tools"
$dst = Join-Path $sdk "cmdline-tools\latest"
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
Move-Item $src $dst
Write-Output "  -> $dst"

$env:JAVA_HOME = $jdk
$env:PATH = "$jdk\bin;" + $env:PATH
$sdkm = Join-Path $dst "bin\sdkmanager.bat"

Write-Output "=== 接受 license ==="
$yes = (1..20 | ForEach-Object { "y" }) -join "`n"
$yes | & $sdkm --sdk_root="$sdk" --licenses 2>&1 | Select-Object -Last 3

Write-Output "=== 安装 platform-tools / android-34 / build-tools 34.0.0 ==="
& $sdkm --sdk_root="$sdk" "platform-tools" "platforms;android-34" "build-tools;34.0.0" 2>&1 | Select-Object -Last 8

Write-Output "=== 结果 ==="
foreach ($p in @("build-tools\34.0.0\aapt2.exe","build-tools\34.0.0\d8.bat","build-tools\34.0.0\apksigner.bat","build-tools\34.0.0\zipalign.exe","platforms\android-34\android.jar","platform-tools\adb.exe")) {
    $full = Join-Path $sdk $p
    if (Test-Path $full) { Write-Output ("  OK   " + $p) } else { Write-Output ("  MISS " + $p) }
}
