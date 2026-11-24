# Enciende la demostración en este equipo. Deje esta ventana abierta; Ctrl+C la detiene.
# Uso: .\INICIAR_FERIA.ps1   y abra http://127.0.0.1:8790/ en el navegador.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) { throw 'Falta el entorno de Python. Ejecute primero INSTALAR.ps1.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'datos\feria.sqlite3'))) { throw 'Falta la base de la demostración. Ejecute primero INSTALAR.ps1.' }

& $python manage.py migrate --verbosity 0
if ($LASTEXITCODE -ne 0) { throw 'No se pudo preparar la base local.' }

Write-Host 'Demostración con datos sintéticos: http://127.0.0.1:8790/'
Write-Host 'Cuentas de demostración: datos\ACCESOS_DEMO.md (o un clic en la pantalla de ingreso).'
& $python serve.py
