# Deja la demostración como nueva: borra la base local y genera otra vez el escenario para la fecha de hoy.
# Detenga antes el servidor (INICIAR_FERIA.ps1). Uso: .\REINICIAR_FERIA.ps1   o   .\REINICIAR_FERIA.ps1 -Fecha 2026-11-18
param(
    [string]$Fecha = '',
    [int]$Semilla = 4711
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) { throw 'Falta el entorno de Python. Ejecute primero INSTALAR.ps1.' }

$opciones = @('manage.py', 'reiniciar_feria', '--si', '--semilla', "$Semilla")
if ($Fecha) { $opciones += @('--fecha', $Fecha) }
& $python @opciones
if ($LASTEXITCODE -ne 0) { throw 'No se pudo reiniciar la demostración. Si el servidor está encendido, deténgalo y vuelva a intentarlo.' }

& $python manage.py verificar_escenario
if ($LASTEXITCODE -ne 0) { throw 'El escenario se generó, pero alguna comprobación no cuadra. Revise el detalle de arriba.' }

Write-Host ''
Write-Host 'Demostración reiniciada. Enciéndala con INICIAR_FERIA.ps1.'
