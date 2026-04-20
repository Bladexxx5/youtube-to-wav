# WAVify – Arranca el backend local y lo registra en Render automaticamente
# Este script corre en segundo plano al iniciar Windows

$PYTHON     = "c:\Users\ttrac\Documents\converter youtube to wav\.venv\Scripts\python.exe"
$APP        = "c:\Users\ttrac\Documents\converter youtube to wav\app.py"
$CF         = "C:\Users\ttrac\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
$RENDER_URL = "https://wavify-igxp.onrender.com"
$SECRET     = "b39a6aaed319a982786d0dc9faccbda7"
$CF_LOG     = "$env:TEMP\wavify_cf.log"

# Evitar doble arranque
$running = Get-Process -Name "python" -ErrorAction SilentlyContinue |
           Where-Object { $_.MainWindowTitle -eq "" }
if ($running) {
    Write-Host "WAVify ya esta corriendo."
    exit
}

Write-Host "Iniciando WAVify backend..."

# 1. Arrancar app.py en segundo plano
Start-Process $PYTHON -ArgumentList "`"$APP`"" -WindowStyle Hidden

Start-Sleep 4

# 2. Arrancar cloudflared y guardar log
if (Test-Path $CF_LOG) { Remove-Item $CF_LOG -Force }
Start-Process $CF -ArgumentList "tunnel --url http://localhost:5000 --no-autoupdate" `
    -RedirectStandardError $CF_LOG -WindowStyle Hidden

# 3. Esperar URL del tunnel (hasta 40 segundos)
Write-Host "Esperando tunnel de Cloudflare..."
$tunnelUrl = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep 2
    if (Test-Path $CF_LOG) {
        $content = Get-Content $CF_LOG -Raw -ErrorAction SilentlyContinue
        if ($content -match "https://[a-z0-9\-]+\.trycloudflare\.com") {
            $tunnelUrl = $Matches[0]
            break
        }
    }
}

if (-not $tunnelUrl) {
    Write-Host "ERROR: No se pudo obtener URL del tunnel."
    exit 1
}

Write-Host "Tunnel: $tunnelUrl"

# 4. Registrar en Render
$body = @{ url = $tunnelUrl; secret = $SECRET } | ConvertTo-Json
try {
    $resp = Invoke-RestMethod -Uri "$RENDER_URL/register-backend" `
        -Method POST -Body $body -ContentType "application/json" -TimeoutSec 30
    Write-Host "Registrado en Render: $($resp.status)"
} catch {
    Write-Host "Error registrando en Render: $_"
}

Write-Host "WAVify listo. La web funciona para todos."
