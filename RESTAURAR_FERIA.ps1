# Devuelve la demostración al escenario inicial en segundos (lo usan los pasos del recorrido que consumen su caso).
# No hace falta detener el servidor. Uso: .\RESTAURAR_FERIA.ps1
# Si el escenario se generó otro día, use REINICIAR_FERIA.ps1: los casos preparados dependen de la fecha.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) { throw 'Falta el entorno de Python. Ejecute primero INSTALAR.ps1.' }

& $python manage.py restaurar_feria --si
if ($LASTEXITCODE -ne 0) { throw 'No se pudo restaurar el escenario inicial. Revise el mensaje de arriba; si falta la copia o no coincide, ejecute REINICIAR_FERIA.ps1.' }
