# download-models.ps1
# Downloads the optional models for MOSS Image Studio into moss-image-pc\models (next to this script).
# Run installer-moss-image.ps1 first: it installs the engine and the main MOSS-Image model.
#   powershell -ExecutionPolicy Bypass -File .\download-models.ps1            (asks which ones)
#   powershell -ExecutionPolicy Bypass -File .\download-models.ps1 flux qwen zimage  (no questions)
# Safe to re-run: finished files are skipped and interrupted ones resume.

param([string[]]$Packs)
$ErrorActionPreference = 'Stop'
$Models = Join-Path $PSScriptRoot 'moss-image-pc\models'
$HF = 'https://huggingface.co'

$catalog = [ordered]@{
    'moss-edit' = @{
        Name = 'MOSS-Image editing add-on'; Size = '1.4 GB'; License = 'Apache-2.0'
        Files = @(
            @('loras\armV5-moss-edit-lora.safetensors', "$HF/akhaliq/MOSS-Image-Edit-HQ/resolve/main/armV5-moss-edit-lora.safetensors"),
            @('Qwen3.5-9B-mmproj-BF16.gguf',            "$HF/unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-BF16.gguf"))
    }
    'flux' = @{
        Name = 'FLUX.2 Klein 4B (fast, also edits)'; Size = '8.6 GB'; License = 'Apache-2.0'
        Files = @(
            @('flux2-klein-4b\flux-2-klein-4b-Q8_0.gguf', "$HF/leejet/FLUX.2-klein-4B-GGUF/resolve/main/flux-2-klein-4b-Q8_0.gguf"),
            @('flux2-klein-4b\Qwen3-4B-Q8_0.gguf',        "$HF/unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q8_0.gguf"))
    }
    'flux3' = @{
        Name = 'FLUX.3 Image Klein (newer FLUX: better pictures and edits, 16 steps)'; Size = '10.4 GB'; License = 'FLUX Kommunity (non-commercial; small businesses OK)'
        Files = @(
            @('flux3-klein\flux3-image-klein-Q8_0.gguf',    "$HF/leejet/flux-3-image-klein-GGUF/resolve/main/flux3-image-klein-Q8_0.gguf"),
            @('flux3-klein\Qwen3VL-4B-Instruct-Q8_0.gguf', "$HF/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q8_0.gguf"))
    }
    'zimage' = @{
        Name = 'Z-Image Turbo (fast photos, good text; shares the Qwen3-4B file with FLUX)'; Size = '6.9 GB, +4.3 GB without FLUX'; License = 'Apache-2.0'
        Files = @(
            @('z-image-turbo\z_image_turbo-Q8_0.gguf', "$HF/leejet/Z-Image-Turbo-GGUF/resolve/main/z_image_turbo-Q8_0.gguf"),
            @('z-image-turbo\ae.safetensors',          "$HF/Comfy-Org/Z-Image-Turbo/resolve/main/split_files/vae/ae.safetensors"),
            @('flux2-klein-4b\Qwen3-4B-Q8_0.gguf',     "$HF/unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q8_0.gguf"))
    }
    'upscaler' = @{
        Name = 'AI upscaler (Real-ESRGAN x4plus, for sharper big pictures)'; Size = '67 MB'; License = 'BSD-3-Clause'
        Files = @(,
            # GitHub has no checksum header, so the exact size is given here. (Leading comma: a one-file list.)
            @('upscalers\RealESRGAN_x4plus.pth', 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth', 67040989))
    }
    'qwen' = @{
        Name = 'Qwen Image 2.1 (text in pictures, also edits)'; Size = '14.1 GB'; License = 'Qwen Research (non-commercial)'
        Files = @(
            @('qwen-image-2.1\qwen_image_2.1_int8_convrot.safetensors', "$HF/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors"),
            @('qwen-image-2.1\qwen_image_2.1_vae_bf16.safetensors',     "$HF/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors"),
            @('qwen-image-2.1\Qwen3VL-8B-Instruct-Q4_K_M.gguf',         "$HF/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
            @('qwen-image-2.1\mmproj-Qwen3VL-8B-Instruct-F16.gguf',     "$HF/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-F16.gguf"))
    }
}

if (-not (Test-Path (Join-Path $Models 'flux2-vae.safetensors'))) {
    Write-Host 'Run installer-moss-image.ps1 first (FLUX also uses the VAE it downloads).' -ForegroundColor Yellow
}

if (-not $Packs) {
    Write-Host 'Optional models for MOSS Image Studio:' -ForegroundColor Cyan
    $i = 1
    foreach ($k in $catalog.Keys) {
        $c = $catalog[$k]
        Write-Host ("  {0}. {1}  ({2}, license: {3})" -f $i, $c.Name, $c.Size, $c.License)
        $i++
    }
    $answer = Read-Host 'Which ones? Type numbers separated by spaces (e.g. 1 2 3), or press Enter to cancel'
    $keys = @($catalog.Keys)
    $Packs = foreach ($n in ($answer -split '[ ,]+' | Where-Object { $_ })) { if ($n -match '^\d+$' -and [int]$n -ge 1 -and [int]$n -le $keys.Count) { $keys[[int]$n - 1] } }
    if (-not $Packs) { Write-Host 'Nothing chosen.'; exit 0 }
}

foreach ($p in $Packs) {
    if (-not $catalog.Contains($p)) { Write-Host "Unknown model '$p'. Choose from: $($catalog.Keys -join ', ')" -ForegroundColor Red; exit 1 }
    $c = $catalog[$p]
    Write-Host ''
    Write-Host "=== $($c.Name) ($($c.Size)) ===" -ForegroundColor Cyan
    foreach ($f in $c.Files) {
        $dest = Join-Path $Models $f[0]
        New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
        # Hugging Face reports each file's exact size and SHA-256 in its X-Linked-* headers.
        $headers = (& curl.exe -sI $f[1]) -join "`n"
        $size = if ($f.Count -gt 2) { [int64]$f[2] } elseif ($headers -match '(?im)^x-linked-size:\s*(\d+)') { [int64]$Matches[1] } else { -1 }
        $sha  = if ($headers -match '(?im)^x-linked-etag:\s*"?([0-9a-f]{64})"?') { $Matches[1] } else { '' }
        if ((Test-Path $dest) -and (Get-Item $dest).Length -eq $size) { Write-Host "  already have $($f[0])" -ForegroundColor Green; continue }
        Write-Host "  downloading $($f[0]) ..."
        $ok = $false
        for ($try = 1; $try -le 10 -and -not $ok; $try++) {
            # A fresh download each try (resuming through Hugging Face's redirects isn't reliable).
            # It goes to .part first, so a half-finished file never looks installed. A connection
            # slower than 1 MB/s for 60 s is dropped and tried again.
            & curl.exe -L --fail --speed-limit 1000000 --speed-time 60 --progress-bar -o "$dest.part" $f[1]
            if ($LASTEXITCODE -ne 0) { Start-Sleep -Seconds 3; continue }
            $len = (Get-Item "$dest.part").Length
            if ($size -gt 0 -and $len -ne $size) { Write-Host "  wrong size ($len, expected $size); trying again" -ForegroundColor Yellow; continue }
            if ($sha) {
                Write-Host '  checking the download...'
                if ((Get-FileHash "$dest.part" -Algorithm SHA256).Hash.ToLower() -ne $sha) { Write-Host '  checksum mismatch; trying again' -ForegroundColor Yellow; continue }
            }
            $ok = $true
        }
        if (-not $ok) { Write-Host "  FAILED: $($f[0]). Check your connection and run this script again." -ForegroundColor Red; exit 1 }
        Move-Item -Force "$dest.part" $dest
    }
    Write-Host "  $($c.Name) is ready." -ForegroundColor Green
}
Write-Host ''
Write-Host 'Done. Pick the model in MOSS Image Studio (Model, at the top left).' -ForegroundColor Green
