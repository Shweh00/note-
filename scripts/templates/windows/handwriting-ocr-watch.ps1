$ErrorActionPreference = "Stop"

$ProjectDir = "__PROJECT_DIR__"
$ConfigPath = "__CONFIG_PATH__"
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $env:USERPROFILE "AppData\Local\handwriting-ocr\logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $ProjectDir

$env:PYTHONUNBUFFERED = "1"
# For online OpenAI OCR, uncomment and set this in the user environment or here:
# $env:OPENAI_API_KEY = "sk-..."

& $Python -m handwriting_ocr watch --config $ConfigPath `
  1>> (Join-Path $LogDir "watch.log") `
  2>> (Join-Path $LogDir "watch.err.log")

exit $LASTEXITCODE
