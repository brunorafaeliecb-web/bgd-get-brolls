"""Os instaladores instalam pip e runtime com `--require-hashes --only-binary :all:`: forma dos arquivos e efeito."""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import ClassVar

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)
from _paths import ROOT

LINE = re.compile(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.!+]+)(?: ; ([^\s].*?))?\s+((?:--hash=sha256:[0-9a-f]{64}\s*)+)")


def logical_lines(name):
    """As linhas de requisito do arquivo, com as continuações (`\\`) juntas e os comentários de fora."""
    text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = [line for line in text.replace("\\\n", " ").splitlines() if line.strip()]
    return [line for line in lines if not line.lstrip().startswith("#")]


def entries(name):
    """`{nome: (versão, marcador, [hashes])}`; uma linha fora do formato fixado com hashes derruba o teste."""
    found: dict[str, tuple[str, str, list[str]]] = {}
    for line in logical_lines(name):
        match = LINE.fullmatch(line.strip())
        assert match, f"{name}: linha fora do formato fixado com hashes: {line[:80]}"
        hashes = re.findall(r"[0-9a-f]{64}", match.group(4))
        found[match.group(1).lower()] = (match.group(2), match.group(3) or "", hashes)
    return found


class HashedRequirementsTests(unittest.TestCase):
    """O `requirements.txt` dos instaladores: cada linha com versão exata e hashes."""

    # Pacotes com extensão compilada: uma trava feita numa máquina só listaria a roda dela.
    COMPILED = ("brotli", "charset-normalizer", "pycryptodomex", "websockets")
    RUNTIME = (
        "yt-dlp",
        "yt-dlp-ejs",
        "brotli",
        "certifi",
        "charset-normalizer",
        "idna",
        "mutagen",
        "pycryptodomex",
        "requests",
        "urllib3",
        "websockets",
    )

    def test_every_requirement_is_pinned_and_carries_hashes(self):
        for name in ("requirements.txt", "requirements-bootstrap.txt"):
            found = entries(name)
            self.assertEqual(len(logical_lines(name)), len(found), name)
            for package, (version, _marker, hashes) in found.items():
                self.assertTrue(version, package)
                self.assertTrue(hashes, f"{package} sem hash: o pip recusa o arquivo inteiro com --require-hashes")
                self.assertEqual(len(hashes), len(set(hashes)), f"{package} repete hash")

    def test_the_files_hold_no_option_that_widens_the_install(self):
        for name in ("requirements.txt", "requirements-bootstrap.txt"):
            for line in logical_lines(name):
                self.assertFalse(line.lstrip().startswith("-"), f"{name}: opção no arquivo: {line[:60]}")
                self.assertNotIn("://", line)
                self.assertNotIn(" @ ", line, "pino por URL ou VCS fica fora")

    def test_the_runtime_set_is_all_there(self):
        found = entries("requirements.txt")
        for name in self.RUNTIME:
            self.assertIn(name, found)
        self.assertEqual("2026.10.19", found["certifi"][0], "certifi sem a raiz emSign (GHSA-6q48-6c7w-9gw2)")

    def test_requirements_in_is_the_source_of_the_top_level_pins(self):
        # `requirements.in` é o que o comando do cabeçalho compila: os pinos de lá são os do arquivo com hashes.
        text = (ROOT / "requirements.in").read_text(encoding="utf-8")
        pins = {}
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                match = re.fullmatch(r"([A-Za-z0-9_.\-]+)(?:\[[a-z,]+\])?==([A-Za-z0-9_.!+]+)", line.strip())
                assert match, f"linha fora do formato de pino exato: {line}"
                pins[match.group(1).lower()] = match.group(2)
        found = entries("requirements.txt")
        self.assertEqual(set(self.RUNTIME), set(pins))
        for name, version in pins.items():
            self.assertEqual(version, found[name][0], name)
        recipe = "uv pip compile --universal --generate-hashes --python-version 3.11 requirements.in"
        header = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn(f"{recipe} -o requirements.txt", header)
        self.assertIn("--only-binary :all:", header)
        # A receita do CONTRIBUTING guarda o cabeçalho escrito à mão, que o `uv` descarta ao regenerar.
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn(recipe, contributing)
        self.assertIn("header.tmp", contributing)

    def test_compiled_packages_list_the_wheels_of_every_platform(self):
        found = entries("requirements.txt")
        for name in self.COMPILED:
            self.assertGreaterEqual(len(found[name][2]), 10, f"{name}: poucas rodas para Linux, macOS e Windows")

    def test_only_pypy_dependencies_carry_a_marker(self):
        markers = {name: marker for name, (_version, marker, _hashes) in entries("requirements.txt").items() if marker}
        self.assertEqual({"brotlicffi", "cffi", "pycparser"}, set(markers))
        for marker in markers.values():
            self.assertIn("implementation_name", marker)

    def test_the_bootstrap_pins_the_pip_that_pypi_publishes(self):
        # sha256 de https://pypi.org/pypi/pip/26.2.2/json (roda e sdist), conferidos na adoção.
        self.assertEqual(
            {
                "pip": (
                    "26.2.2",
                    "",
                    [
                        "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70",
                        "3a8e5c800492787f33a4114ef54444546c340df428974ebba17e0fd47e11af2f",
                    ],
                )
            },
            entries("requirements-bootstrap.txt"),
        )


class InstallerHashFlagsTests(unittest.TestCase):
    """Os dois instaladores: pip com hash antes do runtime com hash, ambos depois da saída do `--check`."""

    def test_installers_install_the_hashed_pip_before_the_hashed_requirements(self):
        for name, check, bootstrap, install in (
            (
                "scripts/install.sh",
                '[ "${1:-}" = "--check" ]',
                '-m pip install --require-hashes --only-binary :all: -r "$ROOT/requirements-bootstrap.txt"',
                '-m pip install --require-hashes --only-binary :all: -r "$ROOT/requirements.txt"',
            ),
            (
                "scripts/install.ps1",
                "if ($Check)",
                (
                    "'pip', 'install', '--require-hashes', '--only-binary', ':all:', '-r', "
                    "(Join-Path $Root 'requirements-bootstrap.txt')"
                ),
                (
                    "'pip', 'install', '--require-hashes', '--only-binary', ':all:', '-r', "
                    "(Join-Path $Root 'requirements.txt')"
                ),
            ),
        ):
            script = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(bootstrap, script, name)
            self.assertIn(install, script, name)
            self.assertLess(script.index(check), script.index(bootstrap), name)
            self.assertLess(script.index(bootstrap), script.index(install), name)
            self.assertNotIn("--upgrade", script, f"{name}: pip sem hash voltou")

    def test_both_installers_name_the_cause_from_the_pip_log(self):
        # O `install.ps1` não roda fora do Windows: as mesmas causas, pelo mesmo log, ficam conferidas no texto.
        for name in ("scripts/install.sh", "scripts/install.ps1"):
            script = (ROOT / name).read_text(encoding="utf-8")
            self.assertEqual(2, script.count("--log"), name)
            for marker in (
                "pip-install.log",
                "Failed to establish a new connection",
                "o pip não alcançou o índice de pacotes",
                "DO NOT MATCH THE HASHES",
                "não confere com o sha256 fixado",
                "No matching distribution",
            ):
                self.assertIn(marker, script, f"{name}: {marker}")


def build_wheel(folder, project, modules):
    """Roda pura mínima `<project>-1.0-py3-none-any.whl` com um `__init__.py` por módulo; devolve (caminho, sha256)."""
    dist = f"{project}-1.0.dist-info"
    files = {f"{module}/__init__.py": b"" for module in modules}
    files[f"{dist}/METADATA"] = f"Metadata-Version: 2.1\nName: {project}\nVersion: 1.0\n".encode()
    files[f"{dist}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(data)}")
    record.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = ("\n".join(record) + "\n").encode()
    wheel = Path(folder) / f"{project}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


@unittest.skipIf(os.name == "nt", "O Windows usa scripts/install.ps1; a CI do Windows instala com os hashes reais.")
class InstallerHashEnforcementTests(unittest.TestCase):
    """`install.sh` de verdade, sem rede: um índice local com rodas mínimas no lugar do PyPI."""

    WRONG = "0" * 64

    def run_installer(self, bootstrap_hash=None, runtime_hash=None, runtime_line=None, **pip_env):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/install.sh", tmp / "scripts/install.sh")
        wheels = tmp / "wheels"
        wheels.mkdir()
        _, bootstrap = build_wheel(wheels, "gbbootstrap", ["gbbootstrap"])
        _, runtime = build_wheel(wheels, "gbruntime", ["yt_dlp", "yt_dlp_ejs"])
        (tmp / "requirements-bootstrap.txt").write_text(
            f"gbbootstrap==1.0 --hash=sha256:{bootstrap_hash or bootstrap}\n", encoding="utf-8"
        )
        (tmp / "requirements.txt").write_text(
            runtime_line or f"gbruntime==1.0 --hash=sha256:{runtime_hash or runtime}\n", encoding="utf-8"
        )
        # Só o Python é de verdade; o resto do sistema que o instalador confere é stub.
        bindir = tmp / "bin"
        bindir.mkdir()
        for name in ("bash", "dirname", "awk", "grep", "mkdir", "cp"):
            actual = shutil.which(name)
            if actual:
                (bindir / name).symlink_to(actual)
        (bindir / "python3").symlink_to(sys.executable)
        for name, body in (
            ("node", 'printf "v22.0.0\\n"'),
            *((tool, "exit 0") for tool in ("ffmpeg", "ffprobe", "curl", "npm", "npx")),
        ):
            stub = bindir / name
            stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            stub.chmod(0o755)
        env = {
            "PATH": str(bindir),
            "HOME": str(tmp),
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": str(wheels),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        # `PIP_NO_INDEX=None` tira a variável: o pip passa a procurar no índice de `PIP_INDEX_URL`.
        env.update({key: value for key, value in pip_env.items() if value is not None})
        for key in [key for key, value in pip_env.items() if value is None]:
            env.pop(key, None)
        result = subprocess.run(
            ["/bin/bash", str(tmp / "scripts/install.sh")],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=300,
        )
        return result, tmp / ".venv"

    def installed(self, venv):
        return sorted(path.name.split("-")[0] for path in venv.glob("lib/python*/site-packages/gb*.dist-info"))

    def test_matching_hashes_install_both_files(self):
        result, venv = self.run_installer()
        self.assertIn("yt-dlp e EJS importados", result.stdout, result.stderr)
        self.assertEqual(["gbbootstrap", "gbruntime"], self.installed(venv))

    def test_a_wrong_bootstrap_hash_stops_before_the_requirements(self):
        result, venv = self.run_installer(bootstrap_hash=self.WRONG)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("DO NOT MATCH THE HASHES", result.stdout + result.stderr)
        self.assertIn("Falha ao instalar o pip da venv", result.stderr)
        self.assertEqual([], self.installed(venv))

    def test_a_wrong_requirements_hash_installs_nothing_from_it(self):
        result, venv = self.run_installer(runtime_hash=self.WRONG)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("DO NOT MATCH THE HASHES", result.stdout + result.stderr)
        self.assertIn("Falha ao instalar as dependências Python", result.stderr)
        self.assertEqual(["gbbootstrap"], self.installed(venv))
        self.assertNotIn("yt-dlp e EJS importados", result.stdout)

    def test_a_requirement_without_a_hash_is_refused(self):
        # Sem nenhum `--hash` no arquivo o pip não liga o modo de hash sozinho: só a opção do instalador recusa.
        result, venv = self.run_installer(runtime_line="gbruntime==1.0\n")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("Hashes are required in --require-hashes mode", result.stdout + result.stderr)
        self.assertEqual(["gbbootstrap"], self.installed(venv))

    # F02 do QA da 2.5.1: a mensagem diz a causa que o log do pip mostra, não a faixa do Python por padrão.
    UNREACHABLE: ClassVar[dict] = {
        "PIP_NO_INDEX": None,
        "PIP_INDEX_URL": "http://127.0.0.1:9/simple",
        "PIP_RETRIES": "0",
        "PIP_TIMEOUT": "3",
    }
    NETWORK = "o pip não alcançou o índice de pacotes"
    HASH = "não confere com o sha256 fixado"
    PYTHON_RANGE = "validado em Python 3.11–3.13"

    def test_an_unreachable_index_at_the_bootstrap_says_network(self):
        result, _venv = self.run_installer(PIP_FIND_LINKS=None, **self.UNREACHABLE)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("Falha ao instalar o pip da venv", result.stderr)
        self.assertIn(self.NETWORK, result.stderr)
        self.assertNotIn(self.PYTHON_RANGE, result.stderr)

    def test_an_unreachable_index_at_the_requirements_says_network(self):
        # O bootstrap acha a roda local; o runtime pede um pacote que só existiria no índice inacessível.
        result, venv = self.run_installer(runtime_line=f"gbausente==1.0 --hash=sha256:{'1' * 64}\n", **self.UNREACHABLE)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertEqual(["gbbootstrap"], self.installed(venv))
        self.assertIn("Falha ao instalar as dependências Python", result.stderr)
        self.assertIn(self.NETWORK, result.stderr)
        self.assertNotIn(self.PYTHON_RANGE, result.stderr)

    def test_a_hash_mismatch_says_hash(self):
        result, _venv = self.run_installer(runtime_hash=self.WRONG)
        self.assertIn(self.HASH, result.stderr)
        self.assertNotIn(self.PYTHON_RANGE, result.stderr)
        self.assertNotIn(self.NETWORK, result.stderr)

    def test_no_distribution_without_network_errors_names_the_python_range(self):
        result, _venv = self.run_installer(runtime_line=f"gbausente==1.0 --hash=sha256:{'1' * 64}\n")
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(self.PYTHON_RANGE, result.stderr)
        self.assertNotIn(self.NETWORK, result.stderr)


if __name__ == "__main__":
    unittest.main()
