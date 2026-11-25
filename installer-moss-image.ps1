# install-moss-image.ps1
# Installs MOSS-Image (stable-diffusion.cpp master-1127-79dc7ca) into the moss-image-pc folder next to this script,
# runs a test image, and creates "MOSS Image.bat" on the Desktop.
# Safe to re-run: finished downloads are skipped, partial ones resume.

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$Root    = Join-Path $PSScriptRoot 'moss-image-pc'
$Bin     = Join-Path $Root 'bin'
$Models  = Join-Path $Root 'models'
$Outputs = Join-Path $Root 'outputs'
$LogFile = Join-Path $env:TEMP 'moss-image-install-log.txt'
$SdTag   = 'master-1127-79dc7ca'   # stable-diffusion.cpp release; bump both lines together
$SdShort = '79dc7ca'
$Release = "https://github.com/leejet/stable-diffusion.cpp/releases/download/$SdTag"
$GB = 1GB

try { Stop-Transcript | Out-Null } catch {}
Start-Transcript -Path $LogFile -Force | Out-Null

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ''; Write-Host "=== $text ===" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ''
    Write-Host "STOPPED: $text" -ForegroundColor Red
    Write-Host "Full log: $LogFile" -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
function Fmt($bytes) {
    if ($bytes -ge 1GB) { return ('{0:N2} GB' -f ($bytes / 1GB)) }
    return ('{0:N0} MB' -f ($bytes / 1MB))
}

# ---------------------------------------------------------------- Step 1: check the PC
Step '1. Checking your PC'

$os = Get-CimInstance Win32_OperatingSystem
$is64 = [Environment]::Is64BitOperatingSystem
$ram  = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$drive = (Split-Path -Qualifier $env:USERPROFILE).TrimEnd(':')
$free  = (Get-PSDrive -Name $drive).Free

Say ("Windows:      {0} (version {1}, build {2}, {3})" -f $os.Caption, $os.Version, $os.BuildNumber, $os.OSArchitecture) White
Say ("Memory:       {0}" -f (Fmt $ram)) White
Say ("Free space:   {0} on drive {1}:" -f (Fmt $free), $drive) White

# Graphics cards (AdapterRAM caps at 4 GB, so read the true size from the registry)
$vramByName = @{}
$classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
Get-ChildItem $classKey -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
    $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
    $v = $p.'HardwareInformation.qwMemorySize'
    if ($v -ne $null -and $p.DriverDesc) {
        if ($v -is [byte[]]) { $v = [BitConverter]::ToUInt64($v, 0) }
        $vramByName[$p.DriverDesc] = [int64]$v
    }
}
$gpus = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notmatch 'Microsoft Basic|Remote|Virtual|Parsec|Mirage|Meta Virtual' }
if (-not $gpus) { Say 'Graphics:     none found' White }
foreach ($g in $gpus) {
    $v = $vramByName[$g.Name]
    if (-not $v) { $v = [int64]$g.AdapterRAM }
    Say ("Graphics:     {0}, about {1} video memory (driver {2})" -f $g.Name, (Fmt $v), $g.DriverVersion) White
}

$nvidia = $false
$nvsmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if (-not $nvsmi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $nvsmi = "$env:SystemRoot\System32\nvidia-smi.exe" }
if ($nvsmi) {
    $nv = & $nvsmi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null
    if ($LASTEXITCODE -eq 0 -and $nv) {
        $nvidia = $true
        Say 'nvidia-smi:' White
        $nv | ForEach-Object { Say "              $_" White }
        Say '' ; & $nvsmi 2>$null | Out-Host
    }
}

$gpuKind = 'cpu'
if ($nvidia -or ($gpus | Where-Object { $_.Name -match 'NVIDIA' })) { $gpuKind = 'nvidia' }
elseif ($gpus | Where-Object { $_.Name -match 'AMD|Radeon|Intel|Arc' }) { $gpuKind = 'vulkan' }
Say ("Build to use: {0}" -f @{nvidia='NVIDIA (CUDA 12)'; vulkan='AMD/Intel (Vulkan)'; cpu='CPU only'}[$gpuKind]) White

# Requirements
if (-not $is64) { Fail 'This is not 64-bit Windows.' }
if ($os.Caption -notmatch 'Windows (10|11)') { Fail "This needs Windows 10 or 11; found $($os.Caption)." }
if ($ram -lt 15.5GB) { Fail ("This needs at least 16 GB of memory; found {0}." -f (Fmt $ram)) }
$already = 0
if (Test-Path $Root) { $already = (Get-ChildItem $Root -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }
if (($free + $already) -lt 20GB) { Fail ("Not enough disk space: {0} free on drive {1}:, at least 20 GB needed." -f (Fmt $free), $drive) }
if ($gpuKind -eq 'cpu') { Say 'WARNING: no usable graphics card found. Each image will take a long time (likely an hour or more).' Yellow }
Say 'PC check passed.' Green

# ---------------------------------------------------------------- Step 2: folders
Step '2. Creating folders'
foreach ($d in $Root, $Bin, $Models, $Outputs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Say "Created $Root with bin, models and outputs." Green

# ---------------------------------------------------------------- download helper
function Get-RemoteSize($url) {
    $h = & curl.exe -sIL --max-time 60 $url 2>$null
    $len = $h | Select-String -Pattern '^content-length:\s*(\d+)' | Select-Object -Last 1
    $lnk = $h | Select-String -Pattern '^x-linked-size:\s*(\d+)' | Select-Object -Last 1
    if ($lnk) { return [int64]$lnk.Matches[0].Groups[1].Value }
    if ($len) { return [int64]$len.Matches[0].Groups[1].Value }
    return [int64]0
}

function Get-File($url, $dest, $approxBytes, $label) {
    $remote = Get-RemoteSize $url
    if ($remote -gt 0) { Say ("{0}: server size {1}" -f $label, (Fmt $remote)) } else { Say "${label}: server did not report a size" }
    for ($i = 1; $i -le 40; $i++) {
        $have = 0; if (Test-Path $dest) { $have = (Get-Item $dest).Length }
        if ($remote -gt 0 -and $have -eq $remote) { break }
        if ($remote -gt 0 -and $have -gt $remote) { Remove-Item $dest -Force; $have = 0 }
        if ($remote -eq 0 -and $have -gt 0 -and $approxBytes -gt 0 -and [math]::Abs($have - $approxBytes) -le 0.3 * $approxBytes) { break }
        if ($have -gt 0) { Say ("Resuming {0} at {1} (try {2})..." -f $label, (Fmt $have), $i) } else { Say "Downloading $label (try $i)..." }
        & curl.exe -L --fail --retry 5 --retry-delay 5 --connect-timeout 30 -C - -o $dest $url
        $code = $LASTEXITCODE
        if ($code -eq 0 -and $remote -eq 0) { break }
        if ($code -ne 0) { Say "curl stopped (code $code); retrying in 10 seconds..." Yellow; Start-Sleep 10 }
    }
    if (-not (Test-Path $dest)) { Fail "$label did not download." }
    $have = (Get-Item $dest).Length
    if ($remote -gt 0 -and $have -ne $remote) { Fail ("{0} is incomplete: {1} of {2}." -f $label, (Fmt $have), (Fmt $remote)) }
    if ($approxBytes -gt 0 -and [math]::Abs($have - $approxBytes) -gt 0.3 * $approxBytes) {
        Fail ("{0} is {1}, but about {2} was expected. It may be the wrong file." -f $label, (Fmt $have), (Fmt $approxBytes))
    }
    Say ("OK  {0}  {1}" -f $label, (Fmt $have)) Green
}

# ---------------------------------------------------------------- Step 3: stable-diffusion.cpp
Step "3. Installing stable-diffusion.cpp $SdTag"
$sdcli = Join-Path $Bin 'sd-cli.exe'
$builtFor = Join-Path $Bin 'build.txt'
$haveBuild = ''; if (Test-Path $builtFor) { $haveBuild = (Get-Content $builtFor -Raw).Trim() }
$wantBuild = "$gpuKind $SdTag"
if ((Test-Path $sdcli) -and $haveBuild -eq $wantBuild) {
    Say 'sd-cli.exe is already installed; skipping.' Green
} else {
    $zips = switch ($gpuKind) {
        'nvidia' { @("sd-master-$SdShort-bin-win-cuda12-x64.zip", 'cudart-sd-bin-win-cu12-x64.zip') }
        'vulkan' { @("sd-master-$SdShort-bin-win-vulkan-x64.zip") }
        default  { @("sd-master-$SdShort-bin-win-cpu-x64.zip") }
    }
    $dl = Join-Path $Root '_zips'
    New-Item -ItemType Directory -Force -Path $dl | Out-Null
    foreach ($z in $zips) {
        $zp = Join-Path $dl $z
        Get-File "$Release/$z" $zp 0 $z
        Say "Unzipping $z into bin..."
        try { Expand-Archive -Path $zp -DestinationPath $Bin -Force -ErrorAction Stop } catch { Fail "Could not unzip ${z}: $_" }
    }
    if (-not (Test-Path $sdcli)) {
        $found = Get-ChildItem $Bin -Recurse -Filter 'sd-cli.exe' | Select-Object -First 1
        if (-not $found) { Fail 'sd-cli.exe was not in the download.' }
        Get-ChildItem $found.DirectoryName -Force | Move-Item -Destination $Bin -Force
    }
    Get-ChildItem $Bin -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    Set-Content -Path $builtFor -Value $wantBuild -Encoding ASCII
    Remove-Item $dl -Recurse -Force -ErrorAction SilentlyContinue
    Say "sd-cli.exe is in $Bin" Green
}

# ---------------------------------------------------------------- Step 4: models
Step '4. Downloading model files (about 11.5 GB)'
$files = @(
    @{ url = 'https://huggingface.co/Comfy-Org/MOSS-Image/resolve/main/diffusion_models/moss_image_sft_int8_convrot.safetensors'; name = 'moss_image_sft_int8_convrot.safetensors'; size = 5.4GB },
    @{ url = 'https://huggingface.co/Comfy-Org/MOSS-Image/resolve/main/vae/flux2-vae.safetensors';                                name = 'flux2-vae.safetensors';                  size = 336MB },
    @{ url = 'https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf';                               name = 'Qwen3.5-9B-Q4_K_M.gguf';                 size = 5.7GB },
    @{ url = 'https://huggingface.co/OpenMOSS-Team/MOSS-Image-Base/resolve/main/tokenizer/tokenizer.json';                       name = 'tokenizer_qwen3.5.json';                 size = 20MB }
)
foreach ($f in $files) { Get-File $f.url (Join-Path $Models $f.name) $f.size $f.name }

# ---------------------------------------------------------------- Step 5: test image
Step '5. Making a test image'

function Read-Tail($path, $bytes = 4096) {
    if (-not (Test-Path $path)) { return '' }
    try {
        $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
        $start = [math]::Max(0, $fs.Length - $bytes); $null = $fs.Seek($start, 'Begin')
        $buf = New-Object byte[] ($fs.Length - $start); $null = $fs.Read($buf, 0, $buf.Length); $fs.Close()
        return [Text.Encoding]::UTF8.GetString($buf)
    } catch { return '' }
}

function Invoke-Sd($modelFile, $outRel, $seed, $prompt) {
    $outAbs = Join-Path $Root $outRel
    if (Test-Path $outAbs) { Remove-Item $outAbs -Force }
    $argLine = "--diffusion-model `"models\$modelFile`" --llm `"models\Qwen3.5-9B-Q4_K_M.gguf`" --vae `"models\flux2-vae.safetensors`" --tokenizer `"models\tokenizer_qwen3.5.json`" --cfg-scale 3.0 --steps 50 -H 768 -W 1360 -s $seed --offload-to-cpu -p `"$prompt`" -o `"$outRel`""
    $o = Join-Path $Root 'sd-run-out.txt'; $e = Join-Path $Root 'sd-run-err.txt'
    Say "Running: .\bin\sd-cli.exe $argLine"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $sdcli -ArgumentList $argLine -WorkingDirectory $Root -RedirectStandardOutput $o -RedirectStandardError $e -NoNewWindow -PassThru
    $null = $p.Handle
    $last = ''
    while (-not $p.HasExited) {
        Start-Sleep -Seconds 5
        $t = (Read-Tail $o) + "`n" + (Read-Tail $e 1024)
        $line = ($t -split "[`r`n]" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($line -and $line -ne $last) { Write-Host ("  [{0:mm\:ss}] {1}" -f $sw.Elapsed, $line.Trim()); $last = $line }
    }
    $p.WaitForExit(); $sw.Stop()
    $log = (Get-Content $o -Raw -ErrorAction SilentlyContinue) + "`n" + (Get-Content $e -Raw -ErrorAction SilentlyContinue)
    Add-Content -Path $LogFile -Value $log -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Ok = ($p.ExitCode -eq 0 -and (Test-Path $outAbs)); Code = $p.ExitCode; Time = $sw.Elapsed; Log = $log; Out = $outAbs }
}

$testPrompt = 'A white enamel sign on a brick wall that reads MOSS RUNS ON MY PC in bold black letters, soft daylight'
$model = 'moss_image_sft_int8_convrot.safetensors'
$r = Invoke-Sd $model 'outputs\test.png' 42 $testPrompt
if (-not $r.Ok) {
    Say ("First try failed (exit code {0}). Last messages:" -f $r.Code) Yellow
    ($r.Log -split "[`r`n]" | Where-Object { $_.Trim() } | Select-Object -Last 15) | ForEach-Object { Say "  $_" Yellow }
    Say 'Switching to the smaller Q4 GGUF model and trying again...' Yellow
    $model = 'MOSS-Image-SFT-UD-Q4_K_XL.gguf'
    Get-File 'https://huggingface.co/unsloth/MOSS-Image-SFT-GGUF/resolve/main/MOSS-Image-SFT-UD-Q4_K_XL.gguf' (Join-Path $Models $model) 0 $model
    $r = Invoke-Sd $model 'outputs\test.png' 42 $testPrompt
    if (-not $r.Ok) {
        ($r.Log -split "[`r`n]" | Where-Object { $_.Trim() } | Select-Object -Last 40) | ForEach-Object { Say "  $_" Red }
        Fail ("The test image failed with both models (exit code {0})." -f $r.Code)
    }
}
$testTime = $r.Time
$timeText = '{0} min {1} sec' -f [int][math]::Floor($testTime.TotalMinutes), $testTime.Seconds
Say ("Test image made in {0} using {1}." -f $timeText, $model) Green
Invoke-Item $r.Out

# ---------------------------------------------------------------- Step 6: desktop launcher
Step '6. Creating "MOSS Image.bat" on your Desktop'
$desktop = [Environment]::GetFolderPath('Desktop')
$bat = @'
@echo off
setlocal EnableExtensions EnableDelayedExpansion
title MOSS Image
cd /d "__ROOT__" || (echo Could not find the moss-image-pc folder. & pause & exit /b 1)
:ask
echo.
set "PROMPT_TEXT="
set /p "PROMPT_TEXT=What do you want to see? "
if not defined PROMPT_TEXT goto ask
set "PROMPT_TEXT=!PROMPT_TEXT:"='!"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm-ss"') do set "STAMP=%%i"
set /a SEED=!RANDOM! * 32768 + !RANDOM!
set "OUT=outputs\moss_!STAMP!_seed!SEED!.png"
echo.
echo Making your image. The test image took __TIME__, so please wait...
echo.
bin\sd-cli.exe --diffusion-model "models\__MODEL__" --llm "models\Qwen3.5-9B-Q4_K_M.gguf" --vae "models\flux2-vae.safetensors" --tokenizer "models\tokenizer_qwen3.5.json" --cfg-scale 3.0 --steps 50 -H 768 -W 1360 -s !SEED! --offload-to-cpu -p "!PROMPT_TEXT!" -o "!OUT!"
echo.
if exist "!OUT!" (
  echo Saved: !CD!\!OUT!
  start "" "!OUT!"
) else (
  echo Sorry, no image was made. See the messages above.
)
echo.
set "AGAIN="
set /p "AGAIN=Make another image? (Y/N) "
if /i "!AGAIN!"=="Y" goto ask
endlocal
'@
$bat = $bat.Replace('__MODEL__', $model).Replace('__TIME__', $timeText).Replace('__ROOT__', $Root)
$batPath = Join-Path $desktop 'MOSS Image.bat'
Set-Content -Path $batPath -Value $bat -Encoding ASCII
Say "Created $batPath" Green

# ---------------------------------------------------------------- summary
Remove-Item (Join-Path $Root 'sd-run-out.txt'), (Join-Path $Root 'sd-run-err.txt') -ErrorAction SilentlyContinue
$used = (Get-ChildItem $Root -Recurse -File | Measure-Object Length -Sum).Sum
Step 'Done'
Say ("Installed:   stable-diffusion.cpp {2} ({0} build) + MOSS-Image ({1})" -f $gpuKind, $model, $SdTag) White
Say ("Location:    {0}" -f $Root) White
Say ("Disk used:   {0}" -f (Fmt $used)) White
Say ("One image:   about {0} (1360x768, 50 steps)" -f $timeText) White
Say  'New image:   double-click "MOSS Image.bat" on your Desktop, type what you want, press Enter.' White
Say  "Log:         $LogFile" White
try { Stop-Transcript | Out-Null } catch {}