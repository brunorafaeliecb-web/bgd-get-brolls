#!/usr/bin/env bash
# Dependencies are installed from PyPI/npm on the recipient's computer, never vendored.
# Python ones with --require-hashes (every file must match a sha256 listed in the requirements files).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-}" in ''|--check) ;; *) printf 'Uso: bash scripts/install.sh [--check]\n'; exit 2;; esac
missing=0
for tool in python3 ffmpeg ffprobe curl bash awk node npm npx; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'MISSING: %s\n' "$tool"; missing=1
  fi
done
if command -v node >/dev/null 2>&1; then
  node_version="$(node --version)"
  node_major="${node_version#v}"; node_major="${node_major%%.*}"
  if ! [[ "$node_major" =~ ^[0-9]+$ ]] || [ "$node_major" -lt 22 ]; then
    printf 'MISSING: Node 22+ (encontrado %s)\n' "$node_version"; missing=1
  fi
fi
if [ "$missing" -ne 0 ]; then
  printf 'Instale os executáveis conforme docs/GUIDE.md e repita.\n'; exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ obrigatório"'
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' drawtext '; then
  printf 'AVISO: FFmpeg sem o filtro drawtext (libfreetype): o contact sheet sai sem número e timecode nas células. Reinstale o FFmpeg com freetype (Homebrew: brew reinstall ffmpeg; Ubuntu: apt install ffmpeg).\n'
fi
if [ "${1:-}" = "--check" ]; then
  printf 'Pré-requisitos do instalador encontrados; check não instala nem testa rede/login.\n'
  exit 0
fi
python3 -m venv "$ROOT/.venv"
python_version="$("$ROOT/.venv/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
# The interpreter's bundled pip vendors an older certifi: the first thing it downloads is the pip in
# requirements-bootstrap.txt, refused unless its sha256 matches. Both steps install wheels only, since
# pip does not hash-check the build tools an sdist pulls in.
pip_log="$ROOT/.venv/pip-install.log"
# Name the cause the pip log shows (network, hash, Python range) instead of one guess for every failure.
pip_failure() {
  printf '%s\n' "$1" >&2
  if grep -qE 'NewConnectionError|Failed to establish a new connection|Max retries exceeded|ConnectTimeout|Read timed out|ProxyError|Temporary failure in name resolution|nodename nor servname|Name or service not known|getaddrinfo failed|SSLError|CERTIFICATE_VERIFY_FAILED|Connection refused|Network is unreachable' "$pip_log" 2>/dev/null; then
    printf 'Causa: o pip não alcançou o índice de pacotes (rede, proxy, VPN, firewall ou PIP_INDEX_URL). Confira a conexão e rode o instalador de novo.\n' >&2
  elif grep -qE 'DO NOT MATCH THE HASHES|Hashes are required' "$pip_log" 2>/dev/null; then
    printf 'Causa: um arquivo baixado não confere com o sha256 fixado em %s. Não instale por fora: atualize o get-brolls (git pull ou /plugin update) e rode o instalador de novo; se repetir, algo na rede pode estar alterando os downloads.\n' "$2" >&2
  elif grep -qE 'No matching distribution|requires a different Python|Could not find a version' "$pip_log" 2>/dev/null; then
    printf 'Causa provável: seu Python é %s; o conjunto fixado foi validado em Python 3.11–3.13. Crie a venv com um interpretador dessa faixa e rode o instalador de novo.\n' "$python_version" >&2
  else
    printf 'Veja a mensagem do pip acima; o registro completo está em %s.\n' "$pip_log" >&2
  fi
  exit 1
}
: > "$pip_log"
if ! "$ROOT/.venv/bin/python" -m pip install --require-hashes --only-binary :all: -r "$ROOT/requirements-bootstrap.txt" --log "$pip_log"; then
  pip_failure 'Falha ao instalar o pip da venv (requirements-bootstrap.txt, com hash).' requirements-bootstrap.txt
fi
: > "$pip_log"
if ! "$ROOT/.venv/bin/python" -m pip install --require-hashes --only-binary :all: -r "$ROOT/requirements.txt" --log "$pip_log"; then
  pip_failure 'Falha ao instalar as dependências Python (requirements.txt, com hash).' requirements.txt
fi
"$ROOT/.venv/bin/python" -c 'import yt_dlp, yt_dlp_ejs; print("yt-dlp e EJS importados")'
# Install the reviewed dependency tree locally, without global npm changes.
mkdir -p "$ROOT/.tools"
cp "$ROOT/package.json" "$ROOT/package-lock.json" "$ROOT/.tools/"
npm --cache "$ROOT/.tools/npm-cache" ci --prefix "$ROOT/.tools" --ignore-scripts --no-audit --no-fund
bash "$ROOT/scripts/playwright.sh" --version
PATH="$ROOT/.venv/bin:$PATH" "$ROOT/.venv/bin/python" "$ROOT/scripts/gb.py" doctor
printf '\nDependências instaladas em .venv/ e .tools/, não fazem parte dos arquivos de distribuição.\n'
printf 'Navegador existente: siga docs/GUIDE.md para reutilizar a sessão autorizada.\n'
