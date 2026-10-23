"""Confere a `.venv` que o runtime usa contra os pinos de `requirements.txt` e `requirements-bootstrap.txt`.

Atualizar o código (git pull, `/plugin update`) sem rodar o instalador de novo, ou com um instalador que
falhou no meio, deixa a venv com as versões antigas: o `doctor` precisa dizer isso em vez de "tudo certo".
Só lê: roda o Python da venv para listar os pacotes instalados e não instala nem corrige nada.
"""

import json
import re
import subprocess
from pathlib import Path

from .config import executable_override, venv_override

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENT_FILES = ("requirements-bootstrap.txt", "requirements.txt")
PYTHON_LAYOUTS = ("Scripts/python.exe", "bin/python")
LIST_TIMEOUT_S = 30

# A versão que tira a raiz distrustada do pacote de certificados (GHSA-6q48-6c7w-9gw2).
CERTIFI_FLOOR = "2026.10.19"
CERTIFI_NOTE = (
    "o certifi desta venv ainda confia na raiz emSign ECC Root CA - G3 (GHSA-6q48-6c7w-9gw2); "
    "a correção só vale depois do instalador"
)

# Lista nome → versão de tudo que o `importlib.metadata` do Python da venv enxerga.
_LIST_SNIPPET = (
    "import importlib.metadata as m, json; "
    "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions() if d.metadata['Name']}))"
)
_PIN = re.compile(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;\\]+)\s*(;)?")


def normalize_name(name):
    """Nome canônico do PEP 503: `Charset_Normalizer` e `charset-normalizer` são o mesmo pacote."""
    return re.sub(r"[-_.]+", "-", name).lower()


def normalize_version(version):
    """`2026.07.22` e `2026.7.22` são a mesma versão: zeros à esquerda não contam."""
    return ".".join(str(int(part)) if part.isdigit() else part.lower() for part in version.split("."))


def _version_key(version):
    return tuple(int(part) if part.isdigit() else -1 for part in normalize_version(version).split("."))


def pinned_versions(root=None):
    """`{nome: versão}` dos dois arquivos de requisitos; linhas com marcador (só PyPy) ficam de fora."""
    root = Path(root) if root is not None else ROOT
    pins = {}
    for name in REQUIREMENT_FILES:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\\\n", " ")
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "-")):
                continue
            match = _PIN.match(line)
            if match and not match.group(3):
                pins[normalize_name(match.group(1))] = match.group(2)
    return pins


def venv_python(base):
    """Python da venv (layout do Windows ou do Unix), ou None quando não há nenhum."""
    for relative in PYTHON_LAYOUTS:
        candidate = Path(base) / relative
        if candidate.is_file():
            return candidate
    return None


def runtime_venv():
    """`(pasta, motivo)` da venv que o runtime usaria; pasta None quando o yt-dlp não vem de venv nenhuma."""
    if executable_override("GB_YTDLP_PATH"):
        return None, "pinned"
    try:
        base = venv_override()
    except ValueError:
        # Pin inválido já entra em `missing` pelo próprio GB_VENV_PATH.
        return None, "skipped"
    return (base if base is not None else ROOT / ".venv"), None


def installed_versions(python):
    """`{nome: versão}` instalados na venv; levanta OSError/ValueError quando o Python dela não roda."""
    done = subprocess.run(
        [str(python), "-I", "-c", _LIST_SNIPPET],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=LIST_TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0:
        raise ValueError(f"saiu com código {done.returncode}")
    return {normalize_name(name): version for name, version in json.loads(done.stdout).items()}


def venv_report(base=None, root=None):
    """Estado da venv: `ok`, `stale` (versão diferente ou pacote ausente), `absent`, `broken`, `pinned`."""
    reason = None
    if base is None:
        base, reason = runtime_venv()
    if base is None:
        return {"status": reason, "path": None, "python": None, "differences": []}
    report = {"status": "ok", "path": str(base), "python": None, "differences": []}
    python = venv_python(base)
    if python is None:
        report["status"] = "absent"
        return report
    report["python"] = str(python)
    try:
        installed = installed_versions(python)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["status"] = "broken"
        report["error"] = str(exc)
        return report
    for name, pinned in sorted(pinned_versions(root).items()):
        found = installed.get(name)
        if found is None:
            report["differences"].append(f"{name} ausente (fixado {pinned})")
        elif normalize_version(found) != normalize_version(pinned):
            report["differences"].append(f"{name} {found} (fixado {pinned})")
    certifi = installed.get("certifi")
    report["certifi_distrusted_root"] = certifi is None or _version_key(certifi) < _version_key(CERTIFI_FLOOR)
    if report["differences"]:
        report["status"] = "stale"
    return report


def missing_entries(report, fix):
    """Itens de `summary.missing` para a venv: vazio quando ela bate com os pinos ou não é usada."""
    status = report["status"]
    if status == "absent":
        note = f"A venv {report['path']} não existe: yt-dlp, EJS e o certifi corrigido não foram instalados"
    elif status == "broken":
        note = f"O Python da venv {report['python']} não roda ({report.get('error')}); refaça a venv pelo instalador"
    elif status == "stale":
        note = "A venv não bate com os pinos fixados: " + ", ".join(report["differences"])
        if report.get("certifi_distrusted_root"):
            note += f"; {CERTIFI_NOTE}"
    else:
        return []
    return [{"item": "venv", "fix": fix, "note": note}]
