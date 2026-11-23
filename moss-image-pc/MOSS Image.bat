@echo off
setlocal EnableExtensions EnableDelayedExpansion
title MOSS Image
cd /d "%~dp0" || (echo Could not find the moss-image-pc folder. & pause & exit /b 1)
rem Starting picture size. Changing it in the menu keeps the new size until the window closes.
set "W=1360"
set "H=768"
:ask
echo.
echo What do you want to see? Type or paste it below - several lines are fine.
echo When you are done, type /go on its own line and press Enter.
echo To throw it away and start over, type /clear.
echo.
set "PROMPT_TEXT="
rem Collect lines until /go. Each line is joined to the prompt with a space.
:readline
set "LINE="
set /p "LINE=> "
if not defined LINE goto readline
set "CMD=!LINE: =!"
if /i "!CMD!"=="/go" goto run
if /i "!CMD!"=="/clear" (
  echo Cleared.
  goto ask
)
set "LINE=!LINE:"='!"
if defined PROMPT_TEXT (set "PROMPT_TEXT=!PROMPT_TEXT! !LINE!") else (set "PROMPT_TEXT=!LINE!")
goto readline
:run
if not defined PROMPT_TEXT (
  echo There is no prompt yet. Type what you want to see first, then /go.
  goto readline
)
echo.
echo Prompt: !PROMPT_TEXT!
:seedask
rem The seed is the random number behind a picture. The same seed and size with a
rem slightly changed prompt gives nearly the same picture (e.g. orange cat -> black cat).
set "SEED="
echo.
echo Seed - keep a picture and change a detail by reusing its seed:
if defined LAST_SEED echo   Last picture: seed !LAST_SEED!, !LAST_W! x !LAST_H!
echo   Enter = new random picture    last = reuse the last picture's seed and size
echo   Or type a seed number from an earlier picture (it is in the file name, e.g. seed935799499)
set "SD="
set /p "SD=Seed: "
if not defined SD goto size
set "SD=!SD: =!"
set "SD=!SD:"=!"
if not defined SD goto size
set "SD=!SD:seed=!"
if /i "!SD!"=="last" goto seedlast
set "BAD="
for /f "delims=0123456789" %%z in ("!SD!") do set "BAD=1"
if not defined BAD if not "!SD:~18!"=="" set "BAD=1"
if defined BAD (
  echo Sorry, a seed is a whole number, like 935799499. Or press Enter, or type last.
  goto seedask
)
set "SEED=!SD!"
echo Using seed !SEED!. Pick the same size as that picture to keep it close.
goto size
:seedlast
if not defined LAST_SEED (
  rem Nothing made in this window yet: use the newest picture in the outputs folder.
  set "LASTFILE="
  for /f "delims=" %%f in ('dir /b /a-d /o-d "outputs\moss_*.png" 2^>nul') do if not defined LASTFILE set "LASTFILE=%%f"
  if not defined LASTFILE (
    echo There is no earlier picture yet. Press Enter for a random seed, or type a number.
    goto seedask
  )
  rem Name looks like moss_2026-11-11_22-17-47_512x512_seed935799499.png
  for /f "tokens=4,5 delims=_" %%a in ("!LASTFILE!") do (set "LWH=%%a" & set "LSD=%%b")
  for /f "tokens=1,2 delims=x" %%a in ("!LWH!") do (set "LAST_W=%%a" & set "LAST_H=%%b")
  set "LSD=!LSD:seed=!"
  set "LAST_SEED=!LSD:.png=!"
  echo From !LASTFILE!
)
set "SEED=!LAST_SEED!" & set "W=!LAST_W!" & set "H=!LAST_H!"
echo Reusing seed !SEED! at !W! x !H!.
goto sizeok
:size
echo.
echo Picture size (width x height):
echo   Standard, about 1 minute:
echo      1  1360 x 768    16:9 wide
echo      2  768 x 1360    9:16 tall
echo      3  1024 x 1024   1:1 square
echo      4  1152 x 896    4:3 wide
echo      5  896 x 1152    3:4 tall
echo      6  1216 x 832    3:2 wide
echo      7  832 x 1216    2:3 tall
echo      8  1536 x 640    21:9 ultrawide
echo   Large, slower:
echo      9  1920 x 1088   16:9 full HD          about 2-3 min
echo     10  2560 x 1440   16:9 QHD              about 5 min
echo     11  1440 x 2560   9:16 tall QHD         about 5 min
echo     12  2048 x 2048   1:1 big square        about 6 min
echo     13  2560 x 1088   21:9 ultrawide        about 3-4 min
echo     14  3440 x 1440   21:9 ultrawide QHD    about 7 min
echo   Or type your own size, like 1280x720 (any shape, up to the pixels of 3440 x 1440)
set "SZ="
set /p "SZ=Pick a size, or press Enter to keep !W!x!H!: "
if not defined SZ goto sizeok
set "SZ=!SZ: =!"
set "SZ=!SZ:"=!"
if not defined SZ goto sizeok
set "PICKED="
for %%p in (1:1360x768 2:768x1360 3:1024x1024 4:1152x896 5:896x1152 6:1216x832 7:832x1216 8:1536x640 9:1920x1088 10:2560x1440 11:1440x2560 12:2048x2048 13:2560x1088 14:3440x1440) do (
  for /f "tokens=1,2,3 delims=:x" %%a in ("%%p") do if "!SZ!"=="%%a" (set "W=%%b" & set "H=%%c" & set "PICKED=1")
)
if defined PICKED goto sizeok
rem Custom size: two whole numbers separated by x, each up to 4 digits.
set "NW=" & set "NH="
for /f "tokens=1,2 delims=xX*" %%a in ("!SZ!") do (set "NW=%%a" & set "NH=%%b")
set "BAD="
if not defined NW set "BAD=1"
if not defined NH set "BAD=1"
if not defined BAD for /f "delims=0123456789" %%z in ("!NW!!NH!") do set "BAD=1"
if not defined BAD if not "!NW:~4!!NH:~4!"=="" set "BAD=1"
if not defined BAD if "!NW:~0,1!"=="0" set "BAD=1"
if not defined BAD if "!NH:~0,1!"=="0" set "BAD=1"
if defined BAD (
  echo Sorry, that is not a size. Type a number from 1 to 14, or a size like 1280x720.
  goto size
)
rem The model needs sizes that are multiples of 16. Each side is kept between 256 and 3440,
rem and the whole picture can have at most as many pixels as 3440 x 1440 (4,953,600).
set /a NW=(NW+8)/16*16, NH=(NH+8)/16*16
if !NW! LSS 256 set "NW=256"
if !NH! LSS 256 set "NH=256"
if !NW! GTR 3440 set "NW=3440"
if !NH! GTR 3440 set "NH=3440"
set /a PIX=NW*NH
if !PIX! GTR 4953600 (
  echo Sorry, !NW!x!NH! is too big. The largest is the pixel count of 3440 x 1440, like 3440x1440 or 2224x2224.
  goto size
)
if not "!NW!x!NH!"=="!SZ!" echo Adjusted to !NW!x!NH! (sizes must be multiples of 16, each side 256 to 3440).
set "W=!NW!" & set "H=!NH!"
:sizeok
echo Size: !W! x !H!
rem Big pictures run out of video memory at the last step, so decode them in tiles.
set /a PIX=W*H
set "TILE="
if !PIX! GTR 2000000 set "TILE=--vae-tiling"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm-ss"') do set "STAMP=%%i"
if not defined SEED set /a SEED=!RANDOM! * 32768 + !RANDOM!
set "OUT=outputs\moss_!STAMP!_!W!x!H!_seed!SEED!.png"
set "TXT=!OUT:~0,-4!.txt"
set "STEPS=50"
set "CFG=3.0"
if not exist outputs mkdir outputs
echo.
rem Save the prompt next to the picture (same name, .txt), with its token count.
rem The helper reads the prompt from PROMPT_TEXT, so special characters arrive intact.
set "PYINFO=tools\venv\Scripts\python.exe"
if exist "!PYINFO!" "!PYINFO!" "tools\prompt_info.py" "!TXT!" !W! !H! !SEED! !STEPS! !CFG!
if not exist "!TXT!" (
  rem Helper missing or failed: still save the prompt, just without a count.
  > "!TXT!" (echo Prompt:& echo(!PROMPT_TEXT!& echo.& echo Tokens: unknown)
)
echo.
echo Making your image. 1360 x 768 takes about 1 minute and 3440 x 1440 about 7 minutes. Please wait...
echo.
bin\sd-cli.exe --diffusion-model "models\moss_image_sft_int8_convrot.safetensors" --llm "models\Qwen3.5-9B-Q4_K_M.gguf" --vae "models\flux2-vae.safetensors" --tokenizer "models\tokenizer_qwen3.5.json" --cfg-scale !CFG! --steps !STEPS! -H !H! -W !W! -s !SEED! --offload-to-cpu !TILE! -p "!PROMPT_TEXT!" -o "!OUT!"
echo.
if exist "!OUT!" (
  echo Saved: !CD!\!OUT!
  echo Prompt: !CD!\!TXT!
  echo Seed: !SEED!  - type last at the Seed question to make a variation of this picture.
  set "LAST_SEED=!SEED!" & set "LAST_W=!W!" & set "LAST_H=!H!"
  start "" "!OUT!"
) else (
  del "!TXT!" 2>nul
  echo Sorry, no image was made. See the messages above.
)
echo.
set "AGAIN="
set /p "AGAIN=Make another image? (Y/N) "
if /i "!AGAIN!"=="Y" goto ask
endlocal
