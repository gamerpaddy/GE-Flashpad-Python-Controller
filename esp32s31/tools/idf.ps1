# Run idf.py with the ESP-IDF v6.1 environment (needed for the esp32s31 target).
#   .\tools\idf.ps1 build
#   .\tools\idf.ps1 -p COM50 flash
# Override the IDF location with $env:FP_IDF_PATH (default C:\esp\v6.1\esp-idf).
$idf = if ($env:FP_IDF_PATH) { $env:FP_IDF_PATH } else { 'C:\esp\v6.1\esp-idf' }
$proj = Split-Path -Parent $PSScriptRoot
$env:IDF_TOOLS_PATH = 'C:\Espressif\tools'
$env:IDF_PATH = $idf
# export.ps1 prints its "Activating ESP-IDF" banner on stderr; that is not an error
$ErrorActionPreference = 'Continue'
. "$idf\export.ps1" *> $null
Push-Location $proj
try {
    & python "$idf\tools\idf.py" @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
