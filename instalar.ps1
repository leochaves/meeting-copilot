<#
  Instalador do Copiloto de Reuniões (Windows 10/11).

  Pode rodar de novo quando quiser: o que já estiver instalado é pulado.

  Opções:
    -ComOllama      instala o modo local (Ollama + modelo) sem perguntar
    -SemOllama      não instala o modo local (usa só o Claude)
    -SemAtalho      não cria o atalho na Área de Trabalho
    -ModeloLocal X  modelo do Ollama a baixar (padrão: gemma3:4b)
#>
param(
    [switch]$ComOllama,
    [switch]$SemOllama,
    [switch]$SemAtalho,
    [string]$ModeloLocal = "gemma3:4b"
)

$ErrorActionPreference = "Stop"
$Raiz = $PSScriptRoot
Set-Location $Raiz
$Py = Join-Path $Raiz ".venv\Scripts\python.exe"

function Passo($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "    OK  $t" -ForegroundColor Green }
function Aviso($t) { Write-Host "    !   $t" -ForegroundColor Yellow }

function Atualizar-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Instalar-Winget($id, $nome) {
    Write-Host "    instalando $nome (pode pedir confirmação do Windows)..."
    winget install -e --id $id --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # -1978335189 = já instalado
        throw "Falha ao instalar $nome (winget saiu com $LASTEXITCODE)."
    }
    Atualizar-Path
}

function Salvar-Config($json) {
    # Grava um padrão nos Ajustes do app (banco local em dados/). O JSON vai
    # por variável de ambiente: o PowerShell 5.1 estraga aspas em argumentos.
    $env:COPILOTO_CFG = $json
    & $Py -c "import json, os, db; db.Db().salvar_config(json.loads(os.environ['COPILOTO_CFG']))"
    Remove-Item Env:COPILOTO_CFG
}

# --- 1. Sistema ---------------------------------------------------------------
Passo "Verificando o sistema"
if (-not [Environment]::Is64BitOperatingSystem) { throw "O Copiloto precisa de Windows 64 bits." }
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget não encontrado. Instale o 'Instalador de Aplicativo' pela Microsoft Store e rode de novo."
}
$livreGB = [math]::Round((Get-PSDrive ($Raiz.Substring(0, 1))).Free / 1GB)
if ($livreGB -lt 10) { Aviso "Só $livreGB GB livres no disco; a instalação completa usa ~10 GB." } else { Ok "$livreGB GB livres" }

# --- 2. Python ----------------------------------------------------------------
Passo "Python 3.10 a 3.13"
function Achar-Python {
    foreach ($c in @(@("py", "-3.12"), @("py", "-3.11"), @("py", "-3.13"), @("python"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $extra = @($c | Select-Object -Skip 1)
        try {
            $v = & $c[0] @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $v) { continue }
        $partes = "$v".Trim().Split(".")
        if ([int]$partes[0] -eq 3 -and [int]$partes[1] -ge 10 -and [int]$partes[1] -le 13) { return , $c }
    }
    return $null
}
if (Test-Path $Py) {
    Ok "ambiente já existe (.venv)"
} else {
    $python = Achar-Python
    if (-not $python) {
        Instalar-Winget "Python.Python.3.12" "Python 3.12"
        $python = Achar-Python
        if (-not $python) { throw "Python instalado, mas não encontrado. Feche esta janela e rode o instalador de novo." }
    }
    Ok ("usando " + ($python -join " "))
    $extra = @($python | Select-Object -Skip 1)
    & $python[0] @extra -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Não consegui criar o ambiente Python (.venv)." }
}

# --- 3. Dependências ------------------------------------------------------------
Passo "Bibliotecas do app (alguns minutos na 1ª vez)"
& $Py -m pip install --upgrade pip --quiet --disable-pip-version-check
& $Py -m pip install -r requirements.txt --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as bibliotecas (requirements.txt)." }
Ok "bibliotecas instaladas"

$nvidia = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $nvidia = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
}
if ($nvidia) {
    Ok "placa NVIDIA: $nvidia"
    & $Py -m pip install -r requirements-gpu.txt --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar as bibliotecas CUDA (requirements-gpu.txt)." }
    Ok "bibliotecas CUDA instaladas"
} else {
    Aviso "Sem placa NVIDIA: a transcrição vai rodar no processador (mais lenta, modelo small)."
    Salvar-Config '{"whisper_dispositivo": "cpu", "whisper_modelo": "small", "local_dispositivo": "cpu", "intervalo": 120}'
}

# --- 4. WebView2 (motor da janela) ------------------------------------------------
Passo "Motor da janela (Microsoft Edge WebView2)"
$wv = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
$wvUser = "HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
if ((Test-Path $wv) -or (Test-Path $wvUser)) { Ok "já instalado" } else { Instalar-Winget "Microsoft.EdgeWebView2Runtime" "WebView2" ; Ok "instalado" }

# --- 5. Modelo de transcrição ---------------------------------------------------
$whisper = if ($nvidia) { "large-v3-turbo" } else { "small" }
Passo "Baixando o modelo de transcrição ($whisper)"
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = "1"
& $Py -c "from faster_whisper.utils import download_model; download_model('$whisper')"
if ($LASTEXITCODE -ne 0) { throw "Falha ao baixar o modelo do Whisper." }
Ok "modelo pronto"

# --- 6. Modo local (Ollama) -------------------------------------------------------
Passo "Modo local (Ollama) — explicações sem internet e sem custo"
$querOllama = $ComOllama
if (-not $ComOllama -and -not $SemOllama) {
    $r = Read-Host "    Instalar o modo local? Baixa ~5 GB (Ollama + modelo $ModeloLocal). [S/n]"
    $querOllama = ($r -eq "" -or $r -match "^[sSyY]")
}
if ($querOllama) {
    $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ollama)) {
        $cmd = Get-Command ollama -ErrorAction SilentlyContinue
        if ($cmd) { $ollama = $cmd.Source } else { Instalar-Winget "Ollama.Ollama" "Ollama" }
    }
    if (-not (Test-Path $ollama)) { $ollama = (Get-Command ollama -ErrorAction Stop).Source }
    Ok "Ollama instalado"

    # Garante o servidor do Ollama no ar antes de baixar o modelo
    $noAr = $false
    for ($i = 0; $i -lt 30; $i++) {
        # 127.0.0.1 e não localhost: o PowerShell tenta IPv6 primeiro e o Ollama só escuta IPv4
        try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 | Out-Null; $noAr = $true; break }
        catch { if ($i -eq 0) { Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden }; Start-Sleep 1 }
    }
    if (-not $noAr) { throw "O Ollama não respondeu. Abra o app Ollama e rode o instalador de novo." }

    & $ollama pull $ModeloLocal
    if ($LASTEXITCODE -ne 0) { throw "Falha ao baixar o modelo $ModeloLocal." }
    Salvar-Config "{`"modelo_local`": `"$ModeloLocal`"}"
    Ok "modelo $ModeloLocal pronto"
} else {
    Aviso "Pulado. O app vai usar o Claude: cole sua chave da API em Ajustes."
    Salvar-Config '{"cerebro": "claude"}'
}

# --- 7. Atalho ------------------------------------------------------------------
if (-not $SemAtalho) {
    Passo "Atalho na Área de Trabalho"
    $atalho = Join-Path ([Environment]::GetFolderPath("Desktop")) "Copiloto de Reuniões.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($atalho)
    $s.TargetPath = Join-Path $Raiz ".venv\Scripts\pythonw.exe"
    $s.Arguments = "`"$(Join-Path $Raiz 'app.py')`""
    $s.WorkingDirectory = $Raiz
    $s.Description = "Copiloto de Reuniões"
    $s.Save()
    Ok $atalho
}

Write-Host "`nPronto! Abra pelo atalho 'Copiloto de Reuniões' na Área de Trabalho (ou Copiloto.bat)." -ForegroundColor Green
