"""`plugins --action install|update`: trazer um plugin de uma pasta ou de um repositório git.

Dois passos, como o `enable`: sem `--yes` só mostra o que chegaria (id, versão,
permissões, contribuições, origem, commit e o sha256 do conteúdo já materializado)
e nada fica em `plugins/`; com `--yes` **e** `--expect <sha256>` batendo com essa
prévia, move para `plugins/<id>`, habilita com pin de hash e grava origem/commit
em `plugins.json`. Nenhum código do plugin roda aqui — só o manifesto é lido.

Um repositório git nunca é `checkout`ado: cloná-lo popularia a árvore de trabalho
passando por filtros de conteúdo (`clean`/`smudge`, ex.: um `filter.lfs.smudge`
ou um filtro arbitrário citado em `.gitattributes`) — código de terceiro rodando
antes de qualquer `--yes`. Em vez disso, clonamos com `--no-checkout` e
materializamos nós mesmos, um blob por vez, direto de `git ls-tree`/`git
cat-file blob` (que nunca aplicam filtro), recusando qualquer entrada que não
seja arquivo regular (link simbólico, submódulo).
"""

# pylint: disable=too-many-lines
# `too-many-lines`: materializar, conferir, varrer as sobras e trocar a pasta de um plugin
# dividem a mesma pasta de staging e as mesmas recusas. Separar a árvore git e o staging em
# módulos próprios é uma mudança só de lugar, feita à parte de qualquer correção.

import contextlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from pathlib import Path
from typing import NamedTuple

from .. import effects, labels, logs
from .._paths import strict_exists, strict_is_dir
from ..runtime import force_rmtree
from . import git_source, loader, registry_state
from .contracts import NAME_RE
from .files import JUNK_FILENAMES, LFS_POINTER_MAX_BYTES, RESERVED_STEMS, counted_files, is_lfs_pointer
from .manifest import MANIFEST_MAX_BYTES, MANIFEST_NAME, compatibility_problem, read_manifest

_log = logs.get("sdk")


def _refused(reason, exc):
    """Registra a recusa do conteúdo (`plugin_content_refused`, com `reason` e o `error_code` do erro) e devolve `exc`.

    Só o motivo: o caminho, o nome do arquivo e o texto da mensagem nunca vão para o log.
    """
    return logs.refusal(_log, "plugin_content_refused", exc, reason=reason)


# Cópia de pasta local: sem metadado de VCS de topo (VCS aninhado é recusado antes,
# em `_refuse_nested_vcs`) e sem lixo de SO (`files.JUNK_FILENAMES`), que fica fora
# do hash. Bytecode e link simbólico não são ignorados: são recusados.
_COPY_IGNORED = (".git", ".hg", ".svn", *sorted(JUNK_FILENAMES))


def _copy_ignore(directory, names):
    """`ignore=` do `shutil.copytree`: os nomes de `_COPY_IGNORED` (VCS de topo e lixo de SO)."""
    return shutil.ignore_patterns(*_COPY_IGNORED)(directory, names)


# Quantos nomes de arquivo a prévia do install/update lista (o total vem sempre).
PREVIEW_FILES_MAX = 50

# Tamanho materializado que aceitamos sem confirmação extra: além de link
# simbólico/submódulo recusado à parte, um plugin gigantesco (histórico git
# inflado, pasta local errada) fica caro demais para copiar/ler às cegas.
# Módulo-nível (não default de parâmetro) de propósito: o teste troca o valor
# via `patch.object(install, "MAX_FILES", N)` e a função tem que enxergar isso
# na hora, não um default já resolvido na definição.
MAX_FILES = 2000
MAX_BYTES = 200 * 1024 * 1024

# Entrada de `git ls-tree` que não é arquivo regular: 120000 é link simbólico
# (o alvo pode apontar pra fora da pasta do plugin), 160000 é gitlink/submódulo
# (aponta pra outro repositório, não é conteúdo que materializamos aqui).
_REFUSED_TREE_MODES = {"120000": "link simbólico", "160000": "submódulo (gitlink)"}
_ALLOWED_TREE_MODES = {"100644", "100755"}

# `.old-*`/`.install-*` mais nova que isso pode ser um `install`/`update` concorrente
# ainda em andamento (não terminou de rodar o próprio `finally`); só mexemos em
# quem já passou desse prazo.
STALE_STAGING_MAX_AGE_S = 3600


def _run_git(args, cwd=None, ssh=False, binary=False):
    return git_source.run_git(args, cwd=cwd, ssh=ssh, binary=binary)


def _git(args, cwd=None, ssh=False):
    return git_source.git_text(args, cwd=cwd, ssh=ssh)


def _git_blob(dest, sha):
    """Conteúdo bruto do blob `sha`, direto do banco de objetos — `cat-file` nunca
    aplica filtro `clean`/`smudge` (ao contrário de um `checkout`)."""
    return _run_git(["cat-file", "blob", sha], cwd=dest, binary=True)


def _refuse_links_and_bytecode(folder, tolerated=frozenset()):
    """Link simbólico ou junction do NTFS (conteúdo fora do hash) e
    bytecode (roda no lugar da fonte revisada) nunca entram em `plugins/` — o loader
    marcaria a pasta `invalid`. `tolerated`: o bytecode do instalador num exemplo do pacote."""
    problem = loader.content_problem(folder, tolerated)
    if problem is not None:
        raise _refused(problem[0], ValueError(loader.content_reason(problem, "rode a prévia de novo")))


def _tree_entries(dest, treeish):
    """(modo, sha, caminho, tamanho) de cada entrada de `git ls-tree -r -l <treeish>` —
    inclui todo blob recursivamente e as entradas de submódulo (que `-r` não
    expande). `-l` traz o tamanho declarado do objeto (`-` para submódulo), o que
    deixa checar o tamanho ANTES de pedir o conteúdo (`cat-file blob`) — um blob
    gigante nunca chega a ser lido só para descobrirmos que ele estoura o teto."""
    raw = _git(["ls-tree", "-r", "-l", "-z", treeish], cwd=dest)
    for record in raw.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, _kind, sha, size_text = meta.split()
        try:
            size = int(size_text)
        except ValueError:
            size = None
        yield mode, sha, path, size


# Nomes curtos 8.3 que o NTFS pode resolver como alias de uma pasta de VCS.
_VCS_SHORT_ALIASES = frozenset({"git~1", "hg~1", "svn~1"})


def _refuse_non_nfc(path, where):
    """Nome de arquivo fora da forma NFC (ex.: acento decomposto, comum em pasta
    criada no HFS+) ou que não era UTF-8 (vira U+FFFD na leitura) é recusado: o
    mesmo nome viraria bytes diferentes em outro sistema, e o sha256 do pin
    deixaria de ser o mesmo entre macOS, Linux e Windows."""
    if "\ufffd" in path or not unicodedata.is_normalized("NFC", path):
        raise _refused(
            "non_nfc",
            ValueError(
                f"Nome de arquivo fora da forma Unicode NFC (ou que não é UTF-8) {where}: {path!r}; renomeie o arquivo."
            ),
        )


def _refuse_lfs_pointer(path, content):
    if is_lfs_pointer(content, len(content)):
        raise _refused(
            "lfs_pointer",
            ValueError(
                f"O plugin tem um ponteiro do Git LFS em {path!r}, não o arquivo de verdade; o install não roda o "
                "Git LFS. Commite o arquivo sem LFS (ou tire-o do plugin)."
            ),
        )


def _refuse_git_path_component(path):
    """Recusa qualquer entrada cujo caminho tenha um componente de VCS — `.git`,
    `.hg` ou `.svn` — em qualquer maiúsc./minúsc.; com ponto(s)/espaço(s) sobrando
    à direita (`.git.`, `.hg `, `.SVN. `), porque o Windows apaga ponto e espaço à
    direita do nome ao gravar em disco, então essas variantes viram a pasta de
    verdade sem bater numa comparação exata; ou o nome curto 8.3 (`GIT~1`, `HG~1`,
    `SVN~1`), que o NTFS pode resolver como alias. Também recusa `:` e `\\` em
    qualquer componente: `:` abre fluxo alternativo NTFS (`.git::$INDEX_ALLOCATION`)
    e `\\` vira separador de pasta no Windows. `.hg`/`.svn` materializados de um
    histórico git ficariam fora do hash do pin; nenhum deles é conteúdo de
    plugin — na melhor das hipóteses é lixo, na pior é metadado plantado que outra
    ferramenta trataria como especial mais adiante."""
    _refuse_non_nfc(path, "no histórico git")
    for part in path.split("/"):
        if ":" in part or "\\" in part:
            raise _refused(
                "path_chars", ValueError(f"Caminho com ':' ou '\\' no histórico git não é aceito: {path!r}.")
            )
        folded = part.casefold()
        if folded.rstrip(". ") in loader.VCS_DIRNAMES or folded in _VCS_SHORT_ALIASES:
            raise _refused(
                "vcs_path",
                ValueError(
                    "Caminho não pode ter um componente de controle de versão (.git, .hg, .svn) no histórico git: "
                    f"{path!r}."
                ),
            )


def _refuse_bytecode_path(path):
    parts = path.split("/")
    if any(loader.is_bytecode_name(part, True) for part in parts[:-1]) or loader.is_bytecode_name(parts[-1], False):
        raise _refused(
            "bytecode",
            ValueError(
                f"O plugin tem bytecode Python ({path!r}) no histórico git; o Python pode rodá-lo no lugar da "
                "fonte revisada. Tire __pycache__/.pyc/.pyo do repositório."
            ),
        )


def _is_junk(path):
    return path.rsplit("/", 1)[-1] in JUNK_FILENAMES


def _refuse_oversized_blob(path, size):
    if size is not None and size > MAX_BYTES:
        raise _refused(
            "oversized_blob",
            ValueError(f"O plugin tem um arquivo de mais de {MAX_BYTES // (1024 * 1024)} MB ({path!r}); recusado."),
        )


def _casefold_path(path):
    """A chave de colisão de `path`: cada componente sem diferença de caixa e sem o ponto
    ou espaço do fim, que o Windows apaga ao gravar (`plugin.py.` vira `plugin.py`)."""
    return "/".join(part.casefold().rstrip(". ") for part in path.split("/"))


def _refuse_windows_name(path):
    """Componente que o Windows não grava com o mesmo nome é recusado, em arquivo ou pasta:
    nome reservado (`CON`, `aux.txt`, `Nul.json`, `CONIN$`, `COM¹`: o radical antes do
    primeiro ponto, sem diferença de caixa) não vira arquivo lá, e ponto ou espaço no fim
    (`LEIAME.md.`) some ao gravar. O plugin chegaria diferente do que a prévia mostrou, e o
    sha256 do conteúdo mudaria só no Windows."""
    for part in path.split("/"):
        if part.split(".", 1)[0].rstrip(" ").upper() in RESERVED_STEMS:
            raise _refused(
                "reserved_name",
                ValueError(
                    f"O plugin tem um nome reservado do Windows ({path!r}: CON, PRN, AUX, NUL, CONIN$, CONOUT$, "
                    "COM0-9 ou LPT0-9, também com ¹ ² ³, com ou sem extensão); renomeie o arquivo ou a pasta."
                ),
            )
        if part.endswith((".", " ")):
            raise _refused(
                "trailing_dot",
                ValueError(
                    f"O plugin tem um nome com ponto ou espaço no fim ({path!r}), que o Windows apaga ao gravar: "
                    "lá o plugin chegaria com outro nome; renomeie o arquivo ou a pasta."
                ),
            )


def _refuse_tree_collisions(paths):
    """Detecta, só a partir da lista de caminhos (do `git ls-tree` ou da pasta comum) — nunca do
    disco de destino —, duas entradas que colidiriam num sistema de arquivos
    que não distingue maiúsc./minúsc. ou que apaga ponto/espaço do fim do nome
    (o Windows): arquivo × arquivo (`plugin.py` e `Plugin.py`, `plugin.py` e
    `plugin.py.`) ou arquivo × pasta (`Config` e `config/extra.py`). Depois da colisão
    (que nomeia os dois caminhos), recusa o nome que o Windows não grava igual
    (`_refuse_windows_name`). Roda ANTES
    de qualquer escrita e não depende do disco real distinguir caixa ou não —
    um disco case-sensitive (comum em CI Linux) materializaria as duas
    entradas sem erro nenhum, escondendo o problema até outra ferramenta
    (`git checkout` de verdade, Windows, um zip/tar que alguém abra depois)
    tratar os dois caminhos como o mesmo."""
    seen_files = {}
    seen_dir_prefixes = {}
    for path in paths:
        folded = _casefold_path(path)
        if folded in seen_files:
            raise _refused(
                "path_collision",
                ValueError(
                    f"Caminhos do plugin colidem (maiúsc./minúsc. ou ponto/espaço no fim): "
                    f"{seen_files[folded]!r} e {path!r}."
                ),
            )
        if folded in seen_dir_prefixes:
            raise _refused(
                "path_collision",
                ValueError(f"Caminhos do plugin colidem (arquivo x pasta): {seen_dir_prefixes[folded]!r} e {path!r}."),
            )
        parts = folded.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in seen_files:
                raise _refused(
                    "path_collision",
                    ValueError(f"Caminhos do plugin colidem (pasta x arquivo): {seen_files[prefix]!r} e {path!r}."),
                )
            seen_dir_prefixes.setdefault(prefix, path)
        seen_files[folded] = path
        _refuse_windows_name(path)


def _write_tree_entry(dest, root, path, mode, content):
    """Escreve `content` em `dest/path`. `_refuse_tree_collisions` já recusou
    qualquer colisão de maiúsc./minúsc. antes de chegar aqui; este
    `try/except` é só defesa em profundidade pra qualquer outro `OSError` de
    sistema de arquivos (disco cheio, permissão), convertido num `ValueError`
    com o caminho e o motivo em vez de deixar o traceback cru vazar."""
    target = (dest / path).resolve()
    if root not in target.parents:
        raise _refused("outside_folder", ValueError(f"Caminho fora da pasta do plugin no histórico git: {path!r}."))
    try:
        effects.mkdir(target.parent, parents=True, exist_ok=True)
        effects.write_bytes(target, content)
        if mode == "100755":
            target.chmod(target.stat().st_mode | 0o111)
    except OSError as exc:
        raise _refused(
            "write_failed", ValueError(f"Não consegui gravar {path!r} do histórico git ({type(exc).__name__}).")
        ) from exc


def _materialize_tree(clone, dest, treeish, paths_only=None):
    """Escreve o conteúdo de `treeish` de `clone` (só o banco de objetos, sem checkout) em `dest`
    — uma pasta de staging SEPARADA do clone, então nenhuma entrada da árvore
    consegue alcançar o `.git` do clone —, um blob por vez, sem jamais
    passar por um `checkout` — por isso sem filtro/smudge/hook. Recusa qualquer
    modo que não seja arquivo regular, qualquer caminho com componente de VCS ou com
    `:`/`\\`, e qualquer blob (mesmo na passada cedo) maior que `MAX_BYTES` — tudo
    isso ANTES de pedir o conteúdo do blob. Na passada completa (`paths_only=None`)
    também limita o total de arquivos/bytes a `MAX_FILES`/`MAX_BYTES`.

    `paths_only`, quando dado, materializa só esses caminhos exatos — usado pela
    validação cedo do manifesto, que não passa pelo teto de total (são no
    máximo dois arquivos pequenos: o manifesto e o `entry`), mas passa pelo teto
    por-blob do jeito que qualquer outra entrada passa."""
    effects.mkdir(dest, exist_ok=True)
    root = dest.resolve()
    # Lixo de SO (`.DS_Store`...) fica fora do hash e da lista da prévia: nunca é
    # gravado, senão o arquivo que roda poderia mudar sem mudar o sha256.
    entries = [
        entry
        for entry in _tree_entries(clone, treeish)
        if (paths_only is None or entry[2] in paths_only) and not _is_junk(entry[2])
    ]
    _refuse_tree_collisions(path for _mode, _sha, path, _size in entries)
    total_files = 0
    total_bytes = 0
    for mode, sha, path, size in entries:
        if mode not in _ALLOWED_TREE_MODES:
            reason = _REFUSED_TREE_MODES.get(mode, f"modo {mode}")
            raise _refused(
                "submodule" if mode == "160000" else "link" if mode == "120000" else "tree_mode",
                ValueError(f"O plugin tem {reason} em {path!r} no histórico git; isso não é aceito."),
            )
        _refuse_git_path_component(path)
        _refuse_bytecode_path(path)
        _refuse_oversized_blob(path, size)
        if paths_only is None:
            total_files += 1
            if total_files > MAX_FILES:
                raise _refused("too_many_files", ValueError(f"O plugin tem mais de {MAX_FILES} arquivos; recusado."))
            total_bytes += size or 0
            if total_bytes > MAX_BYTES:
                raise _refused("too_large", ValueError(f"O plugin passa de {MAX_BYTES // (1024 * 1024)} MB; recusado."))
        content = _git_blob(clone, sha)
        _refuse_lfs_pointer(path, content)
        _write_tree_entry(dest, root, path, mode, content)


def _peek_entry_name(dest):
    """Nome do arquivo `entry` do manifesto já materializado em `dest`, só pra
    saber qual outro arquivo trazer cedo junto — `read_manifest` exige o
    `entry` presente em disco, então validar o manifesto sozinho sempre falharia.
    Não é validação de verdade (isso é `_checked_manifest`/`read_manifest`
    logo a seguir): um manifesto ilegível aqui só significa que não dá pra
    adiantar o `entry`, e a validação de verdade falha do jeito certo depois."""
    try:
        path = dest / MANIFEST_NAME
        if path.stat().st_size > MANIFEST_MAX_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    # lenient: optional entry preview: the real manifest check right after raises
    except (OSError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        return None
    entry = raw.get("entry") if isinstance(raw, dict) else None
    return entry if isinstance(entry, str) else None


def _validate_manifest_early(clone, dest, treeish, compat=True):
    """Materializa e valida o manifesto (e o `entry` que ele declara) antes
    de trazer o resto — potencialmente grande — da árvore; falha cedo, sem gastar
    tempo/disco com um plugin incompatível ou inválido."""
    _materialize_tree(clone, dest, treeish, paths_only={MANIFEST_NAME})
    entry = _peek_entry_name(dest)
    if entry:
        _materialize_tree(clone, dest, treeish, paths_only={MANIFEST_NAME, entry})
    _checked_manifest(dest, compat)


def _from_git(spec, dest, compat=True):
    """Busca o commit de `spec` numa pasta de staging própria (sem checkout),
    materializa a árvore (de `spec.subdir`, quando há) em `dest` (outra pasta) e
    apaga o clone; devolve o commit."""
    clone = dest.with_name(_new_install_staging_name())
    try:
        commit = git_source.fetch(spec, clone)
        treeish = _subdir_tree(clone, commit, spec.subdir)
        _validate_manifest_early(clone, dest, treeish, compat)
        _materialize_tree(clone, dest, treeish)
    finally:
        force_rmtree(clone)
    _refuse_links_and_bytecode(dest)
    return commit


def _refuse_nested_vcs(folder):
    """Pasta de VCS (`.git`, `.hg`, `.svn`) fora do topo da pasta do plugin é
    recusada: o hash do pin só deixa de fora o `.git` de topo, e uma pasta
    aninhada dessas seria conteúdo que o plugin lê/executa sem ser metadado de nada."""
    nested = loader.nested_vcs(folder)
    if nested is not None:
        raise _refused(
            "nested_vcs",
            ValueError(f"O plugin tem uma pasta de controle de versão aninhada ({nested}); tire-a antes de instalar."),
        )


def _file_list(folder):
    """Arquivos que o pin vai cobrir, para a prévia: total e nomes (no máximo `PREVIEW_FILES_MAX`)."""
    names = [rel.as_posix() for rel, _path in counted_files(folder)]
    return {"count": len(names), "names": names[:PREVIEW_FILES_MAX], "truncated": len(names) > PREVIEW_FILES_MAX}


def _guard_folder_cap(folder):
    """Teto de árvore antes de copiar, no caminho de pasta comum: mesmo recorte de `folder_digest`
    (`files.counted_files`), então o que conta aqui é exatamente o que seria
    materializado por `shutil.copytree` com `_copy_ignore`. Recusa também, como no
    caminho git, nome fora de NFC e ponteiro do Git LFS."""
    total_bytes = 0
    for total_files, (rel, path) in enumerate(counted_files(folder), start=1):
        if total_files > MAX_FILES:
            raise _refused("too_many_files", ValueError(f"O plugin tem mais de {MAX_FILES} arquivos; recusado."))
        _refuse_non_nfc(rel.as_posix(), "na pasta")
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_BYTES:
            raise _refused("too_large", ValueError(f"O plugin passa de {MAX_BYTES // (1024 * 1024)} MB; recusado."))
        if size <= LFS_POINTER_MAX_BYTES:
            _refuse_lfs_pointer(rel.as_posix(), path.read_bytes())


def _subdir_tree(clone, commit, subdir):
    """`<commit>:<subdir>` conferido como pasta (`tree`) no commit; sem `subdir`, o próprio commit."""
    if not subdir:
        return commit
    entry = git_source.tree_entry(clone, commit, subdir)
    if entry is None:
        raise _refused("subdir_missing", ValueError(f"--subdir {subdir!r} não existe no commit {commit[:12]}."))
    if entry[1] != "tree" or entry[0] != "040000":
        raise _refused(
            "subdir_not_folder",
            ValueError(f"--subdir {subdir!r} não é uma pasta no commit {commit[:12]}; aponte para a pasta do plugin."),
        )
    return f"{commit}:{subdir}"


def local_repository_warning(commit, ref):
    """Aviso da prévia quando a origem é uma pasta local que é repositório git."""
    return (
        f"A pasta é um repositório git: entra o commit {commit[:12]} ({ref or 'HEAD'}), não a árvore de "
        "trabalho; o que não foi commitado fica de fora."
    )


def _copy_folder(folder, dest, compat=True):
    """Copia a pasta comum `folder` para `dest`, com as mesmas recusas do caminho git. O bytecode que o
    instalador compilou num exemplo do pacote (`loader.shipped_bytecode`) não é copiado."""
    _checked_manifest(folder, compat)
    _refuse_nested_vcs(folder)
    shipped = loader.shipped_bytecode(folder)
    _refuse_links_and_bytecode(folder, shipped)
    _guard_folder_cap(folder)
    _refuse_tree_collisions(rel.as_posix() for rel, _path in counted_files(folder) if rel.as_posix() not in shipped)
    ignored = _copy_ignore if not shipped else shutil.ignore_patterns(*_COPY_IGNORED, loader.BYTECODE_DIRNAME)
    try:
        effects.copytree(folder, dest, symlinks=True, ignore=ignored)
    except OSError as exc:
        raise _refused(
            "copy_failed", ValueError(f"Não consegui copiar a pasta do plugin ({type(exc).__name__}).")
        ) from exc
    _refuse_links_and_bytecode(dest)


def _materialize(spec, dest, compat=True):
    """Traz `spec` (`git_source.Source`) para `dest` (que não existe) e devolve
    `(origem, avisos)`; a origem é o que o `plugins.json` grava em `sources`.

    Repositório (pasta com `.git` ou URL) nunca é `checkout`ado (ver o docstring do
    módulo); pasta comum com link ou bytecode é recusada, e a cópia (links
    copiados como links, `symlinks=True`, sem `.git` nem lixo de SO) é
    conferida de novo — checar o resultado materializado, não só a origem,
    fecha a corrida entre "olhar" e "copiar"."""
    if isinstance(spec, git_source.FolderSource):
        _copy_folder(Path(spec.path), dest, compat)
        return {"source": spec.path, "commit": None}, []
    commit = _from_git(spec, dest, compat)
    # Só para a pasta local sem --commit: quem fixou o commit (ou o índice) já sabe o que entra.
    plain_local = not git_source.is_remote(spec.repo) and spec.commit is None
    warnings = [local_repository_warning(commit, spec.ref)] if plain_local else []
    # Commit fixado E ref dada: o commit tem que ser a ponta dela, senão pode vir de um fork.
    tip = git_source.tip_warning(spec, commit) if spec.commit is not None else None
    if tip:
        warnings.append(tip)
    return {"source": spec.repo, "commit": commit, "ref": spec.ref, "subdir": spec.subdir}, warnings


# O epoch de criação vai no NOME da pasta de staging, não é lido do `st_mtime`
# dela: `os.replace`/`shutil.copytree` preservam timestamp do conteúdo de
# origem (um plugin instalado há muito tempo, movido ou copiado agora,
# continua com o `st_mtime` antigo do próprio conteúdo) — uma pasta de staging
# recém-criada podia "parecer" velha o bastante pra a varredura mexer nela no
# meio de um `install`/`update` concorrente, apagando o staging de outro
# processo ainda rodando (`FileNotFoundError` cru quando esse processo, mais
# tarde, tentasse a própria troca). O nome é a única fonte de verdade da idade.
_INSTALL_STAGING_RE = re.compile(r"^\.install-(\d+)-[0-9a-f]{32}$")
_OLD_STAGING_RE = re.compile(r"^\.old-(\d+)-(.+)-[0-9a-f]{32}$")
# Pasta de um `plugins --action remove` (trocada de lugar antes de apagar). Nunca
# reusa o prefixo `.old-*`: esse a varredura devolve ao lugar quando o plugin sumiu,
# e um plugin removido de propósito voltaria sozinho. Resto `.removed-*` é sempre apagado.
_REMOVED_STAGING_RE = re.compile(r"^\.removed-(\d+)-[0-9a-f]{32}$")


# Só `.install-*`: o `.old-*` de uma troca que falhou no meio é citado de propósito
# (é onde o conteúdo anterior ficou).
_STAGING_NAME_RE = re.compile(r"\.install-\d+-[0-9a-f]{32}")


def hide_staging_names(text):
    """`text` sem o nome de uma pasta de staging (`.install-<epoch>-<hash>`…): "Plugin
    .install-…: x" vira "Plugin da origem: x"; o nome é detalhe interno, não algo que a
    pessoa encontre ou possa corrigir."""
    text = re.sub(r"Plugin " + _STAGING_NAME_RE.pattern + ":", "Plugin da origem:", str(text))
    return _STAGING_NAME_RE.sub("(pasta temporária)", text)


@contextlib.contextmanager
def _staging_names_hidden():
    """Reescreve o `ValueError` que sair do bloco sem o nome da pasta de staging."""
    try:
        yield
    except ValueError as exc:
        clean = hide_staging_names(exc)
        if clean == str(exc):
            raise
        try:
            replacement = type(exc)(clean)
        except TypeError:
            replacement = ValueError(clean)
        raise replacement from exc


def _new_install_staging_name():
    return f".install-{int(time.time())}-{uuid.uuid4().hex}"


def new_removed_staging_name():
    """Nome da pasta para onde o `remove` tira o plugin antes de apagá-lo."""
    return f".removed-{int(time.time())}-{uuid.uuid4().hex}"


def _new_old_staging_name(plugin_id):
    return f".old-{int(time.time())}-{plugin_id}-{uuid.uuid4().hex}"


def _sweep_stale_install(entry, name, now):
    match = _INSTALL_STAGING_RE.match(name)
    if not match or now - int(match.group(1)) < STALE_STAGING_MAX_AGE_S:
        return  # nome não reconhecido (versão anterior/lixo) ou jovem demais: não mexe
    force_rmtree(entry)


def _sweep_stale_removed(entry, name, now):
    match = _REMOVED_STAGING_RE.match(name)
    if not match or now - int(match.group(1)) < STALE_STAGING_MAX_AGE_S:
        return  # nome não reconhecido ou jovem demais (um `remove` ainda apagando): não mexe
    force_rmtree(entry)


def _known_ids():
    """Ids que o `plugins.json` ainda guarda (pin, último pin ou origem); `None` se ilegível."""
    try:
        state = loader.read_state()
    except (ValueError, OSError):
        return None
    return {*state.get("enabled", {}), *state.get("last_pins", {}), *state.get("sources", {})}


def _sweep_stale_old(root, entry, name, now, known):
    match = _OLD_STAGING_RE.match(name)
    if not match:
        return  # nome não reconhecido: sem como saber a idade com confiança, não mexe
    epoch, plugin_id = int(match.group(1)), match.group(2)
    if now - epoch < STALE_STAGING_MAX_AGE_S:
        return
    if not NAME_RE.fullmatch(plugin_id):
        return  # nome corrompido/inesperado: nunca usa como caminho sem validar antes
    if known is None:
        return  # plugins.json ilegível: sem saber se o plugin foi removido, não mexe
    target = root / plugin_id
    try:
        # Só volta ao lugar o plugin que o plugins.json ainda conhece: um `remove --yes`
        # depois de um update interrompido tirou o id de lá, e o `.old-*` é só resto.
        # lenient: sweep of stale .old-* folders; best effort
        if not target.exists() and plugin_id in known and _restorable(entry, plugin_id):
            effects.replace(entry, target)
        else:
            force_rmtree(entry)
    except OSError:
        # Uma corrida com outro processo (a pasta já não existe mais, ou o
        # destino apareceu entre o `exists()` e o `replace()`) nunca pode
        # abortar quem chamou `install`/`update`: na pior hipótese essa pasta
        # de resto fica pra próxima varredura.
        pass


def _restorable(entry, plugin_id):
    """Um `.old-*` só volta ao lugar se for mesmo aquele plugin: manifesto legível
    e com o mesmo id do nome. Pasta vazia, parcial ou de outro plugin é
    resto — restaurá-la travaria um `install` futuro com um plugin quebrado."""
    try:
        return read_manifest(entry, require_folder_match=False)["id"] == plugin_id
    except (ValueError, OSError):
        return False


def _sweep_one_stale_entry(root, entry, now, known):
    # lenient: stale-entry sweep; an entry it cannot stat is left for a later sweep
    if not entry.is_dir():
        return
    name = entry.name
    if name.startswith(".install-"):
        _sweep_stale_install(entry, name, now)
    elif name.startswith(".old-"):
        _sweep_stale_old(root, entry, name, now, known)
    elif name.startswith(".removed-"):
        _sweep_stale_removed(entry, name, now)


def _sweep_stale_staging():
    """Uma pasta `.install-*`/`.old-*`/`.removed-*` que sobrou de um processo anterior morto
    no meio (sem chance de rodar o próprio `finally`) não deve ficar acumulando
    disco nem confundir uma leitura futura de `plugins/`. Duas ressalvas, e uma
    regra de nunca-quebrar (ver `_sweep_one_stale_entry`):

    - Uma pasta mais nova que `STALE_STAGING_MAX_AGE_S` (pelo epoch no NOME,
      não pelo `st_mtime`) pode ser de um `install`/`update` concorrente ainda
      em andamento — mexer nela agora corromperia esse processo em vez de
      limpar um resto de verdade.
    - Se `plugins/<id>` já não existe e sobrou um `.old-<epoch>-<id>-<uuid>`
      antigo, é porque o processo morreu bem entre as duas trocas do `update`
      (depois de retirar a pasta original, antes de pôr a nova no lugar):
      esse `.old-*` é a ÚNICA cópia que resta do plugin, e apagá-lo destruiria
      o plugin inteiro. Devolvemos ele ao lugar em vez de apagar — mas só se o
      `plugins.json` ainda guarda o id: sem ele, o plugin foi removido de
      propósito (`remove --yes`) e o `.old-*` é resto, apagado.

    Nenhum erro de sistema de arquivos numa entrada individual pode abortar
    quem chamou `install`/`update`: o pior caso aceitável é deixar aquela
    pasta de resto pra próxima varredura."""
    root = loader.plugins_root()
    # lenient: sweep of stale staging; best effort
    if not root.is_dir():
        return
    now = time.time()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    known = _known_ids()
    for entry in entries:
        try:
            _sweep_one_stale_entry(root, entry, now, known)
        except OSError:
            continue


def _staging():
    root = loader.plugins_root()
    effects.mkdir(root, parents=True, exist_ok=True)
    # Começa com ponto: `loader.entries()` ignora a pasta enquanto ela existe.
    return root / _new_install_staging_name()


def _checked_manifest(folder, compat=True):
    """Manifesto de `folder`; com `compat`, também compatível com esta instalação."""
    manifest = read_manifest(folder, require_folder_match=False)
    problem = compatibility_problem(manifest) if compat else None
    if problem:
        raise ValueError(f"Plugin {manifest['id']}: {problem}")
    return manifest


def _summary(manifest, origin, content, files, warnings=()):
    """Prévia do install/update: manifesto, origem (com commit e ref), sha256, arquivos
    e, quando o plugin declara `permissions.paths`, as raízes resolvidas que o pin vai
    gravar. `warnings` junta os avisos da materialização aos das permissões."""
    warnings = [*warnings, *loader.permission_warnings(manifest)]
    summary = {
        **loader.manifest_summary(manifest),
        "source": origin["source"],
        "commit": origin["commit"],
        "ref": origin.get("ref"),
        "subdir": origin.get("subdir"),
        "sha256": content.sha,
        "files": files,
    }
    if manifest["permissions"]["paths"]:
        summary[loader.ROOTS_FIELD] = content.roots
    if warnings:
        summary["warnings"] = warnings
    return summary


def _as_spec(source):
    if isinstance(source, git_source.GitSource | git_source.FolderSource):
        return source
    return git_source.parse_source(source)


def content_digest(source):
    """`(sha256 do conteúdo, manifesto)` de `source` (`GitSource`, `FolderSource` ou
    `--source`), sem instalar nada: o mesmo sha256 que a prévia do `install` mostraria.

    Materializa numa pasta temporária do sistema (nunca em `plugins/`), sempre
    apagada no fim, e só lê o manifesto — nenhum código do plugin roda. Não confere
    compatibilidade com esta instalação (`platforms`, `requires_getbrolls`): quem
    monta um índice pode estar em outro sistema."""
    spec = _as_spec(source)
    tmp = Path(effects.mkdtemp(prefix="gb-digest-"))
    try:
        dest = tmp / "plugin"
        _materialize(spec, dest, compat=False)
        return loader.folder_digest(dest), read_manifest(dest, require_folder_match=False)
    finally:
        force_rmtree(tmp)


def fetch_file(source, path, max_bytes):
    """`(commit, bytes)` do arquivo `path` (relativo a `source.subdir`, quando há) no
    commit de `source`, sem checkout: recusa o que não é arquivo regular e o que
    passa de `max_bytes` antes de ler o conteúdo."""
    if not isinstance(source, git_source.GitSource):
        raise _refused("not_git", ValueError("fetch_file só lê de um repositório git."))
    git_source.validate_repo_path(path, "caminho")
    full = f"{source.subdir}/{path}" if source.subdir else path
    tmp = Path(effects.mkdtemp(prefix="gb-fetch-"))
    try:
        clone = tmp / "clone"
        commit = git_source.fetch(source, clone)
        entry = git_source.tree_entry(clone, commit, full)
        if entry is None or entry[0] not in _ALLOWED_TREE_MODES:
            raise _refused(
                "not_regular_file", ValueError(f"{full!r} não é um arquivo regular no commit {commit[:12]}.")
            )
        if entry[3] is None or entry[3] > max_bytes:
            raise _refused(
                "file_too_large",
                ValueError(f"{full!r} passa de {max_bytes} bytes no commit {commit[:12]}; recusado."),
            )
        return commit, _git_blob(clone, entry[2])
    finally:
        force_rmtree(tmp)


def check_expect(expect, sha, *, action, plugin_id, announce=True):
    """`loader.check_expect` (compartilhado com o `enable` de plugin suspenso), com o log da confirmação.

    A recusa vira `plugin_confirm_refused` (`expect_missing` ou `expect_mismatch`, com o `error_code`);
    passar vira `plugin_confirmed`, a menos que `announce=False` (o marketplace confere um valor
    derivado antes e o sha256 do conteúdo depois: a confirmação é uma só).
    """
    try:
        loader.check_expect(expect, sha)
    except ValueError as exc:
        reason = "expect_mismatch" if expect else "expect_missing"
        logs.refusal(_log, "plugin_confirm_refused", exc, action=action, plugin=plugin_id, reason=reason)
        raise
    if announce:
        logs.event(_log, logging.INFO, "plugin_confirmed", action=action, plugin=plugin_id, sha256=sha[:12])


# Chaves que um chamador (o marketplace) pode acrescentar à origem gravada.
ORIGIN_EXTRA_KEYS = ("marketplace", "tier", "index_commit")


def _with_extra(origin, origin_extra):
    extra = dict(origin_extra or {})
    unknown = set(extra) - set(ORIGIN_EXTRA_KEYS)
    if unknown:
        raise ValueError(f"origin_extra só aceita {', '.join(ORIGIN_EXTRA_KEYS)}: {', '.join(sorted(unknown))}.")
    if any(value is not None and not isinstance(value, str) for value in extra.values()):
        raise ValueError("origin_extra só aceita texto ou None.")
    return {**origin, **extra}


def _origin_kind(origin):
    """De onde veio o plugin, para o log: `marketplace`, `git` (repositório) ou `folder` (pasta comum)."""
    if origin.get("marketplace"):
        return "marketplace"
    return "git" if origin.get("commit") else "folder"


def _log_preview(action, manifest, origin, preview, current=None):
    """`plugin_preview` (INFO): o que a prévia mostrou, em ids e contagens (nunca caminho, URL nem nome de arquivo)."""
    added = loader.permissions_added(current["permissions"] if current else None, manifest["permissions"])
    logs.event(
        _log,
        logging.INFO,
        "plugin_preview",
        action=action,
        plugin=manifest["id"],
        version=manifest["version"],
        source=_origin_kind(origin),
        marketplace=origin.get("marketplace"),
        commit=(origin.get("commit") or "-")[:12],
        files=preview["files"]["count"],
        permissions_added=sum(len(items) for items in added.values()),
        warnings=len(preview.get("warnings", ())),
    )


def install(  # noqa: PLR0913 - keyword-only pin options, one per CLI flag
    source, confirm, expect=None, *, commit=None, ref=None, subdir=None
):  # pylint: disable=too-many-arguments  # keyword-only pin options, one per CLI flag
    """Prévia (sem `confirm`) ou instalação de um plugin a partir de pasta local ou URL git.

    `commit` fixa o commit (senão, `ref` — ausente vale `HEAD` — é resolvida na
    origem); `subdir` é a pasta do plugin dentro do repositório. Confirmar exige o
    sha256 da prévia em `expect`; o plugin sai instalado e habilitado."""
    spec = git_source.parse_source(source, commit=commit, ref=ref, subdir=subdir)
    return install_from(spec, confirm, expect)


def install_from(spec, confirm, expect=None, *, verify=None, origin_extra=None):
    """`install` a partir de um `git_source.Source` já montado.

    `verify(manifest, sha256)` roda depois de materializar e antes da prévia; um
    `ValueError` dele recusa o install e o staging é apagado. `origin_extra`
    acrescenta `marketplace`/`tier`/`index_commit` à origem gravada."""
    with _staging_names_hidden():
        return _install_from(spec, confirm, expect, (verify, origin_extra))


def _install_from(spec, confirm, expect, hooks):
    verify, origin_extra = hooks
    loader.read_state()  # plugins.json corrompido recusa antes de qualquer mutação.
    _sweep_stale_staging()
    staging = _staging()
    try:
        origin, warnings = _materialize(spec, staging)
        origin = _with_extra(origin, origin_extra)
        manifest = _checked_manifest(staging)
        target = loader.plugins_root() / manifest["id"]
        if strict_exists(target):
            raise logs.refusal(
                _log,
                "plugin_refused",
                ValueError(
                    f"Plugin {manifest['id']} já está instalado; use plugins --action update --id {manifest['id']}."
                ),
                action="install",
                plugin=manifest["id"],
                reason="already_installed",
            )
        content = loader.PinContent(*loader.pin_digests(staging), loader.pinned_roots(manifest))
        if verify is not None:
            verify(manifest, content.sha)
        preview = _summary(manifest, origin, content, _file_list(staging), warnings)
        if not confirm:
            _log_preview("install", manifest, origin, preview)
            return {"installed": False, "plugin": preview, "note": loader.EXPECT_NOTE}
        check_expect(expect, content.sha, action="install", plugin_id=manifest["id"])
        pinned_sha = _pin_then_move(manifest, origin, content, staging, target)
    finally:
        force_rmtree(staging)
    logs.event(
        _log,
        logging.INFO,
        "plugin_installed",
        plugin=manifest["id"],
        version=manifest["version"],
        source=_origin_kind(origin),
        marketplace=origin.get("marketplace"),
    )
    return {"installed": True, "plugin": {**preview, "sha256": pinned_sha}, "note": loader.DONE_NOTE}


def _pin_then_move(manifest, origin, content, staging, target):
    """Grava o pin (do conteúdo conferido no staging) e só depois põe a pasta em `plugins/<id>`.

    Nessa ordem, um `plugins.json` que não grava (disco cheio) não deixa pasta sem pin, que o
    `install` chamaria de "já instalado" e o `update` de "não foi instalado por install". Se a
    troca de pasta falhar, o `plugins.json` volta ao que era. Devolve o sha256 gravado."""
    with effects.op("plugin.install", manifest["id"]):
        previous = loader.read_state()
        pinned_sha = loader.pin(manifest, target, origin, content=content)
        try:
            effects.replace(staging, target)
        except BaseException:
            try:
                loader.write_state(previous)
            except OSError:
                logs.event(_log, logging.ERROR, "plugin_swap_failed", plugin=manifest["id"], rolled_back=False)
            else:
                logs.event(_log, logging.WARNING, "plugin_swap_failed", plugin=manifest["id"], rolled_back=True)
            registry_state.forget()
            raise
        return pinned_sha


def _diff(old_folder, old_manifest, new_folder, new_manifest):
    before = loader.file_digests(old_folder)
    after = loader.file_digests(new_folder)
    old_permissions = old_manifest["permissions"] if old_manifest else None
    added = loader.permissions_added(old_permissions, new_manifest["permissions"])
    return {
        "version": {"from": old_manifest["version"] if old_manifest else None, "to": new_manifest["version"]},
        "permissions": loader.permissions_diff(old_permissions, new_manifest["permissions"]),
        "permissions_added": added,
        "permissions_increased": any(added.values()),
        "files": loader.files_diff(before, after),
    }


class _Staged(NamedTuple):
    """O que o `update` materializou no staging e conferiu, antes de trocar a pasta."""

    origin: dict
    manifest: dict
    content: "loader.PinContent"
    preview: dict
    diff: dict


def _stage_update(plugin_id, spec, staging, installed, hooks):
    """Materializa `spec` em `staging`, confere o manifesto e monta a prévia com o diff.

    `installed` é `(pasta, manifesto)` do plugin instalado; `hooks`, `(verify, origin_extra)`."""
    folder, current = installed
    verify, origin_extra = hooks
    origin, warnings = _materialize(spec, staging)
    origin = _with_extra(origin, origin_extra)
    manifest = _checked_manifest(staging)
    if manifest["id"] != plugin_id:
        raise logs.refusal(
            _log,
            "plugin_refused",
            ValueError(f"A origem agora traz o plugin {manifest['id']}, não {plugin_id}; nada foi trocado."),
            action="update",
            plugin=plugin_id,
            reason="id_mismatch",
        )
    content = loader.PinContent(*loader.pin_digests(staging), loader.pinned_roots(manifest))
    if verify is not None:
        verify(manifest, content.sha)
    preview = _summary(manifest, origin, content, _file_list(staging), warnings)
    diff = _diff(folder, current, staging, manifest)
    return _Staged(origin, manifest, content, preview, diff)


def _swap_in(plugin_id, folder, staging):
    """Troca a pasta instalada `folder` pelo conteúdo de `staging`."""
    with effects.op("plugin.install", plugin_id):
        retired = folder.with_name(_new_old_staging_name(plugin_id))
        effects.replace(folder, retired)
        try:
            effects.replace(staging, folder)
        except OSError as exc:
            # A troca de verdade falhou no meio — devolve o conteúdo antigo
            # ao lugar em vez de deixar o plugin sem pasta nenhuma.
            try:
                effects.replace(retired, folder)
            except OSError as rollback_exc:
                logs.event(_log, logging.ERROR, "plugin_swap_failed", plugin=plugin_id, rolled_back=False)
                raise ValueError(
                    f"A troca de {plugin_id} falhou ({type(exc).__name__}) e desfazê-la também falhou "
                    f"({type(rollback_exc).__name__}); o conteúdo anterior pode estar em {labels.shown(retired)}, "
                    f"não em {labels.shown(folder)}. Confira as duas pastas manualmente antes de tentar de novo."
                ) from rollback_exc
            logs.event(_log, logging.WARNING, "plugin_swap_failed", plugin=plugin_id, rolled_back=True)
            raise
        force_rmtree(retired)


def update(plugin_id, confirm, expect=None, *, commit=None):
    """Prévia (com o diff) ou troca de um plugin instalado pela versão da origem gravada.

    Sem `commit`, a ref gravada (ausente vale `HEAD`) é resolvida de novo na origem;
    com `commit`, fixa aquele commit (inclusive um anterior, para voltar atrás).
    Confirmar exige o sha256 da prévia em `expect`; um plugin desabilitado continua
    desabilitado."""
    origin = _recorded_origin(plugin_id, loader.read_state())
    return update_from(plugin_id, recorded_spec(plugin_id, origin, commit), confirm, expect)


def _origin_refused(plugin_id, reason, message):
    return logs.refusal(_log, "plugin_refused", ValueError(message), action="update", plugin=plugin_id, reason=reason)


def _origin_gone(plugin_id, source, problem):
    """A origem gravada não serve mais: o plugin só volta de outra origem."""
    return _origin_refused(
        plugin_id,
        "origin_unavailable",
        f"A origem gravada do plugin {plugin_id} ({source}) não serve mais para o update: {problem}. Para trazer o "
        f"plugin de outra origem, rode plugins --action remove --id {plugin_id} --yes e depois "
        "plugins --action install --source <pasta ou URL git>.",
    )


def _check_recorded_folder(plugin_id, origin, commit):
    """A pasta gravada em `origin` tem que servir do jeito que a instalação a gravou.

    Cada recusa diz o conserto certo: só a pasta que sumiu (ou deixou de ser o repositório
    da instalação) pede outra origem. Um `.git` quebrado se conserta no lugar, e um
    `--commit` numa pasta comum é o erro da flag, não da origem."""
    source = origin["source"]
    folder = Path(source).expanduser()
    if not strict_is_dir(folder):
        raise _origin_gone(plugin_id, source, "a pasta não existe mais")
    folder = folder.resolve()
    if git_source.is_repository(folder):
        return
    # lenient: repository sniffing after is_repository; the copy raises on a locked folder
    if os.path.lexists(folder / ".git"):
        raise _origin_refused(
            plugin_id,
            "origin_invalid_worktree",
            f"A origem gravada do plugin {plugin_id} ({source}) tem um .git que não é a pasta de um repositório "
            "nem o gitfile de uma worktree ou submódulo desta pasta (link, gitfile copiado ou quebrado). Conserte "
            "o .git (numa worktree, git worktree repair) ou tire o .git para a pasta ser copiada como pasta comum, "
            "e rode o update de novo.",
        )
    if origin.get("ref") is not None or origin.get("subdir") is not None:
        raise _origin_gone(plugin_id, source, "a pasta não é mais o repositório git que era na instalação")
    if commit is not None:
        raise _origin_refused(
            plugin_id,
            "pin_flags_on_folder",
            f"--commit só vale para plugin instalado de um repositório git, e a origem gravada do plugin "
            f"{plugin_id} ({source}) é uma pasta comum: rode plugins --action update --id {plugin_id} sem --commit.",
        )


def recorded_spec(plugin_id, origin, commit=None):
    """A origem gravada de `plugin_id` (`sources.<id>` do plugins.json) como `git_source.Source`.

    Passa pela mesma validação de um `--source` novo, mas a recusa fala da origem gravada e do
    conserto (`_check_recorded_folder`): a pessoa não digitou `--source` nenhum no `update`. Só
    o `commit` (o `--commit` do update) é dela, e é conferido antes, com a própria mensagem.
    Uma URL (ou ref/subpasta) gravada que a validação de hoje não aceita mais deixa também a
    linha `git_source_refused` com a regra que falhou."""
    if commit is not None:
        git_source.validate_commit(commit)
    source = origin["source"]
    if not git_source.is_remote(source):
        _check_recorded_folder(plugin_id, origin, commit)
    try:
        return git_source.parse_source(source, commit=commit, ref=origin.get("ref"), subdir=origin.get("subdir"))
    except ValueError as exc:
        problem = (
            "a URL não é mais aceita"
            if git_source.is_remote(source)
            else "a ref ou a subpasta gravada não é mais aceita"
        )
        raise _origin_gone(plugin_id, source, problem) from exc


def _recorded_origin(plugin_id, state):
    origin = (state.get("sources") or {}).get(plugin_id)
    if origin is None:
        raise logs.refusal(
            _log,
            "plugin_refused",
            ValueError(
                f"Plugin {plugin_id} não foi instalado por plugins --action install; "
                "atualize a pasta à mão e rode enable."
            ),
            action="update",
            plugin=plugin_id,
            reason="no_recorded_origin",
        )
    return origin


def update_from(  # noqa: PLR0913 - keyword-only hooks, as in install_from
    plugin_id, spec, confirm, expect=None, *, verify=None, origin_extra=None
):  # pylint: disable=too-many-arguments  # keyword-only hooks, as in install_from
    """`update` de `plugin_id` a partir de um `git_source.Source` já montado.

    `verify` e `origin_extra` como em `install_from`. O plugin tem que ter sido
    instalado por `install` (origem gravada)."""
    with _staging_names_hidden():
        return _update_from(plugin_id, spec, confirm, expect, (verify, origin_extra))


def _update_from(plugin_id, spec, confirm, expect, hooks):
    state = loader.read_state()  # plugins.json corrompido recusa antes de qualquer mutação.
    _recorded_origin(plugin_id, state)
    was_enabled = plugin_id in state.get("enabled", {})
    _sweep_stale_staging()
    _, folder, current = loader.find(plugin_id)
    staging = _staging()
    try:
        staged = _stage_update(plugin_id, spec, staging, (folder, current), hooks)
        if not confirm:
            _log_preview("update", staged.manifest, staged.origin, staged.preview, current)
            return {"updated": False, "plugin": staged.preview, "diff": staged.diff, "note": loader.EXPECT_NOTE}
        check_expect(expect, staged.content.sha, action="update", plugin_id=plugin_id)
        _swap_in(plugin_id, folder, staging)
    finally:
        force_rmtree(staging)
    # Atualizar o conteúdo não liga de volta um plugin que estava desabilitado —
    # só quem já estava habilitado sai daqui com pin novo (senão o pin some).
    sha_after = loader.pin(staged.manifest, folder, staged.origin, enabled=was_enabled, content=staged.content)
    logs.event(
        _log,
        logging.INFO,
        "plugin_updated",
        plugin=plugin_id,
        version=staged.manifest["version"],
        commit=(staged.origin["commit"] or "-")[:12],
        source=_origin_kind(staged.origin),
        marketplace=staged.origin.get("marketplace"),
    )
    result = {
        "updated": True,
        "plugin": {**staged.preview, "sha256": sha_after},
        "diff": staged.diff,
        "enabled": was_enabled,
        "note": loader.DONE_NOTE,
    }
    if not was_enabled:
        result["note"] = (
            "Plugin atualizado, mas continua desabilitado (já estava antes do update); "
            f"rode `plugins --action enable --id {plugin_id} --yes` para habilitar."
        )
    return result
