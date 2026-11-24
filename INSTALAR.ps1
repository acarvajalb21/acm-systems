# Instala la demostración en este equipo: entorno de Python, dependencias, base local y escenario sintético.
# Uso: clic derecho → «Ejecutar con PowerShell», o desde una terminal:  .\INSTALAR.ps1
# Si Python 3.13 no está en el lanzador «py» ni en el PATH:  .\INSTALAR.ps1 -Interprete "<ruta completa de python.exe>"
param([string]$Interprete = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

# Ejecuta un programa sin que sus avisos por la salida de error detengan el guion; se decide por el código de salida.
function Invoke-Tranquilo([string]$Exe, [string[]]$Argumentos) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & $Exe @Argumentos 2>&1 | Where-Object { $_ -is [string] } } catch { $out = @(); $global:LASTEXITCODE = 1 }
    $ErrorActionPreference = $previous
    return $out
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Creando el entorno de Python (.venv)...'
    # Se prueba, en orden: el intérprete indicado con -Interprete, el lanzador «py -3.13» y el «python» del sistema.
    $created = $false
    $candidates = @()
    if ($Interprete) { $candidates += , @($Interprete) }
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3.13') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python') }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $extra = @($candidate | Select-Object -Skip 1)
        # Solo se acepta Python 3.13 (la versión con la que se probó); con -Interprete se respeta la elección y se avisa.
        $found = Invoke-Tranquilo $exe ($extra + @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")) | Select-Object -Last 1
        if ($LASTEXITCODE -ne 0 -or -not $found) { continue }
        if ($found -ne '3.13' -and -not ($Interprete -and $exe -eq $Interprete)) {
            Write-Host "Se encontró Python $found ($exe); se necesita 3.13. Se sigue buscando."
            continue
        }
        Invoke-Tranquilo $exe ($extra + @('-m', 'venv', '.venv')) | Out-Null
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $python)) { $created = $true; break }
        if (Test-Path -LiteralPath '.venv') { Remove-Item -LiteralPath '.venv' -Recurse -Force }
    }
    if (-not $created) {
        throw 'No se pudo crear el entorno. Instale Python 3.13 o indique su ruta:  .\INSTALAR.ps1 -Interprete "<ruta completa de python.exe>"'
    }
}

$version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($version -ne '3.13') { Write-Warning "El entorno usa Python $version; la demostración se probó con Python 3.13." }

Write-Host 'Instalando dependencias...'
& $python -m pip install --disable-pip-version-check --quiet -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias.' }

Write-Host 'Preparando la base local...'
& $python manage.py migrate --verbosity 0
if ($LASTEXITCODE -ne 0) { throw 'No se pudo preparar la base local.' }

Write-Host 'Generando el escenario sintético (tarda unos minutos)...'
& $python manage.py reiniciar_feria --si
if ($LASTEXITCODE -ne 0) { throw 'No se pudo generar el escenario.' }

Write-Host ''
Write-Host 'Instalación terminada. Para abrir la demostración ejecute INICIAR_FERIA.ps1.'
Write-Host 'Las cuentas de demostración quedaron en datos\ACCESOS_DEMO.md.'
