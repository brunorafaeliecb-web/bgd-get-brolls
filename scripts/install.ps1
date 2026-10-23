param(
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Native {
    param(
        [string]$Label,
        [string]$File,
        [string[]]$Arguments
    )
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label falhou com código $LASTEXITCODE."
    }
}

$Root = Split-Path -Parent $PSScriptRoot
$Missing = $false
foreach ($Tool in @('python', 'ffmpeg', 'ffprobe', 'curl', 'node', 'npm', 'npx')) {
    if (-not (Get-Command $Tool -CommandType Application -ErrorAction SilentlyContinue)) {
        Write-Host "MISSING: $Tool"
        $Missing = $true
    }
}
if ($Missing) {
    throw 'Instale os executáveis conforme docs/GUIDE.md e repita.'
}

Invoke-Native -Label 'Verificação do Python' -File 'python' -Arguments @('-c', 'import sys; assert sys.version_info >= (3,11), ''Python 3.11+ obrigatório''')

$Filters = (& ffmpeg -hide_banner -filters 2>$null | Out-String)
if ($Filters -notmatch ' drawtext ') {
    Write-Host 'AVISO: FFmpeg sem o filtro drawtext (libfreetype): o contact sheet sai sem número e timecode nas células. Instale um build com freetype (gyan.dev ou BtbN) e coloque no PATH.'
}

$NodeVersion = (& node --version).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Não foi possível consultar a versão do Node.' }
$NodeMajor = [int](($NodeVersion -replace '^v', '').Split('.')[0])
if ($NodeMajor -lt 22) { throw "Node 22+ obrigatório (encontrado $NodeVersion)." }

if ($Check) {
    Write-Host 'Pré-requisitos do instalador encontrados; check não instala nem testa rede/login.'
    exit 0
}

Invoke-Native -Label 'Criação da venv' -File 'python' -Arguments @('-m', 'venv', (Join-Path $Root '.venv'))
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$PythonVersion = (& $VenvPython -c 'import sys; print(''%d.%d.%d'' % sys.version_info[:3])').Trim()
# The interpreter's bundled pip vendors an older certifi: the first thing it downloads is the pip in
# requirements-bootstrap.txt, refused unless its sha256 matches. Both steps install wheels only, since
# pip does not hash-check the build tools an sdist pulls in.
$PipLog = Join-Path $Root '.venv\pip-install.log'
# Name the cause the pip log shows (network, hash, Python range) instead of one guess for every failure.
function Write-PipFailure {
    param(
        [string]$Message,
        [string]$File
    )
    Write-Host $Message
    $Log = ''
    if (Test-Path -LiteralPath $PipLog) { $Log = [string](Get-Content -LiteralPath $PipLog -Raw) }
    # Hash first: it is the final error, and a retried connection earlier in the same log must not hide it.
    if ($Log -match 'DO NOT MATCH THE HASHES|Hashes are required') {
        Write-Host "Causa: um arquivo baixado não confere com o sha256 fixado em $File. Não instale por fora: atualize o get-brolls (git pull ou /plugin update) e rode o instalador de novo; se repetir, algo na rede pode estar alterando os downloads."
    } elseif ($Log -match 'NewConnectionError|Failed to establish a new connection|Max retries exceeded|ConnectTimeout|Read timed out|ProxyError|Temporary failure in name resolution|getaddrinfo failed|SSLError|CERTIFICATE_VERIFY_FAILED|Connection refused|Network is unreachable') {
        Write-Host 'Causa: o pip não alcançou o índice de pacotes (rede, proxy, VPN, firewall ou PIP_INDEX_URL). Confira a conexão e rode o instalador de novo.'
    } elseif ($Log -match 'No matching distribution|requires a different Python|Could not find a version') {
        Write-Host "Causa provável: seu Python é $PythonVersion; o conjunto fixado foi validado em Python 3.11-3.13. Crie a venv com um interpretador dessa faixa e rode o instalador de novo."
    } else {
        Write-Host "Veja a mensagem do pip acima; o registro completo está em $PipLog."
    }
}
Set-Content -LiteralPath $PipLog -Value ''
try {
    Invoke-Native -Label 'Instalação do pip (requirements-bootstrap.txt, com hash)' -File $VenvPython -Arguments @('-m', 'pip', 'install', '--require-hashes', '--only-binary', ':all:', '-r', (Join-Path $Root 'requirements-bootstrap.txt'), '--log', $PipLog)
} catch {
    Write-PipFailure -Message 'Falha ao instalar o pip da venv (requirements-bootstrap.txt, com hash).' -File 'requirements-bootstrap.txt'
    throw
}
Set-Content -LiteralPath $PipLog -Value ''
try {
    Invoke-Native -Label 'Instalação Python' -File $VenvPython -Arguments @('-m', 'pip', 'install', '--require-hashes', '--only-binary', ':all:', '-r', (Join-Path $Root 'requirements.txt'), '--log', $PipLog)
} catch {
    Write-PipFailure -Message 'Falha ao instalar as dependências Python (requirements.txt, com hash).' -File 'requirements.txt'
    throw
}
Invoke-Native -Label 'Importação yt-dlp/EJS' -File $VenvPython -Arguments @('-c', 'import yt_dlp, yt_dlp_ejs; print(''yt-dlp e EJS importados'')')
$Tools = Join-Path $Root '.tools'
New-Item -ItemType Directory -Path $Tools -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Root 'package.json'), (Join-Path $Root 'package-lock.json') -Destination $Tools -Force
Invoke-Native -Label 'Instalação Playwright CLI' -File 'npm' -Arguments @('--cache', (Join-Path $Root '.tools\npm-cache'), 'ci', '--prefix', $Tools, '--ignore-scripts', '--no-audit', '--no-fund')
$Playwright = Join-Path $Root '.tools\node_modules\.bin\playwright-cli.cmd'
Invoke-Native -Label 'Verificação Playwright CLI' -File $Playwright -Arguments @('--version')
$env:PATH = "$(Join-Path $Root '.venv\Scripts');$env:PATH"
Invoke-Native -Label 'Doctor' -File $VenvPython -Arguments @((Join-Path $Root 'scripts\gb.py'), 'doctor')

Write-Host "`nDependências instaladas em .venv\ e .tools\; não fazem parte do repositório."
Write-Host 'Navegador existente: siga docs/GUIDE.md para reutilizar a sessão autorizada.'
