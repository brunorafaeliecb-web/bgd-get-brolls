"""Leitor único de arquivo de fora do estado do core (PRD §5, "Leitor único").

Tudo o que o getbrolls lê de uma pasta que outra pessoa ou outro programa escreveu (a volta de um
ambiente em `handoffs/<amb>/NNN/`, o arquivo que um comando copia para o projeto, a pasta de
`--project-dir`) passa por aqui, com as mesmas regras:

1. desce da raiz que o core escolheu, um nome por vez: link ou junção em qualquer pedaço é recusa;
2. abre sem seguir link, só arquivo regular; no POSIX, pasta por pasta pelo descritor
   (`dir_fd` + `O_NOFOLLOW`); no Windows, `lstat` de cada pedaço (junção conta como link), a
   identidade do arquivo conferida no descritor aberto e a das pastas conferida de novo no fim;
3. texto exige um nome só no disco (`st_nlink == 1`); mídia pode ter mais (o `deliver` faz hardlink);
4. tetos por arquivo, por listagem e de profundidade; nunca lê um byte além do teto;
5. texto só em UTF-8 sem NUL; JSON sem chave repetida, sem NaN e sem aninhamento fundo.

Três erros que quem chama nunca confunde: `Refused` (dado recusado: `INVALID_DATA`, `reason_code`
`UNSAFE_INPUT` e um `reason` curto), `UnreadableError` (E/S: pasta sem permissão, disco com defeito:
`IO_ERROR`) e `MissingError` (não está lá; quem chama decide se é recusa ou espera). Uma pasta sem
permissão de leitura nunca vira ausente. As mensagens só levam o caminho relativo à raiz.
"""

import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

from . import effects, labels, runtime
from .errors import Refusal, UsageError
from .sdk import safe_copy
from .sdk.files import RESERVED_STEMS
from .sdk.guard import plain_line
from .sdk.names import NAME_RE

MAX_TEXT_BYTES = 256 * 1024  # um .md ou texto que vai à IA (PRD §5)
MAX_JSON_BYTES = 64 * 1024  # um JSON pequeno de fora (hyperframes.json, um sidecar)
MAX_COMPONENT_BYTES = 5 * 1024 * 1024  # um componente copiado para a prateleira (logo, whoosh)
MAX_ENTRIES = 5000  # itens de uma listagem
MAX_DEPTH = 8  # níveis de pasta de uma listagem
MAX_PARTS = 16  # pedaços de um caminho relativo
MAX_NAME_BYTES = 255  # um nome de arquivo, em bytes UTF-8 (o teto comum dos sistemas de arquivos)
UNTRUSTED_LIMIT = 4096  # bytes UTF-8 de `untrusted_text`
JSON_MAX_DEPTH = 32
_CHUNK = 1024 * 1024

PHRASES = {
    "bad_name": "caminho recusado (absoluto, com '..', '\\', ':', caractere de controle, fora de NFC ou nome "
    "reservado do Windows)",
    "link": "é link ou junção, e o getbrolls não segue link em arquivo de fora",
    "not_dir": "um pedaço do caminho não é pasta",
    "not_regular": "não é um arquivo comum",
    "hardlinked": "tem mais de um nome no disco (hardlink); copie o arquivo em vez de ligá-lo",
    "too_big": "passa do teto de tamanho",
    "changed": "mudou enquanto era lido; tente de novo",
    "not_text": "não é texto UTF-8 (ou tem o caractere NUL)",
    "bad_json": "não é um objeto JSON válido (chave repetida, NaN e aninhamento fundo contam)",
}

# Reparse points que apontam para outro caminho (junção e link simbólico do NTFS), como em
# `sdk.files.is_link`: os outros (arquivo sob demanda do OneDrive) guardam o próprio conteúdo.
_LINK_TAGS = frozenset(
    {
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    }
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
_DIR_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
# Descer pelo descritor pede `dir_fd` em `open`/`stat`, `O_DIRECTORY`/`O_NOFOLLOW` e `scandir` de
# descritor (POSIX). Sem isso (Windows), vale a descida por `lstat` com a identidade conferida.
_BY_FD = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


class Refused(Refusal):
    """Arquivo ou pasta de fora recusado: `reason` curto (`link`, `too_big`…), `rel` relativo à raiz."""

    def __init__(self, reason: str, rel: str):
        super().__init__(f"{rel or '.'}: {PHRASES[reason]}.", reason_code="UNSAFE_INPUT")
        self.reason = reason
        self.rel = rel

    def __reduce__(self) -> tuple[Any, ...]:
        # `errors.Refusal.__reduce__` refaz pela mensagem; aqui o estado é `reason` e `rel` (pickle, copy).
        return (type(self), (self.reason, self.rel))


class UnreadableError(OSError):
    """O arquivo ou a pasta existe e não deu para ler (permissão, disco): `IO_ERROR`, nunca "ausente"."""

    def __init__(self, rel: str, type_name: str):
        super().__init__(f"{rel or '.'}: não consegui ler ({type_name}); confira a permissão da pasta.")
        self.rel = rel
        self.type_name = type_name


class MissingError(ValueError):
    """Não há nada em `rel` (ou falta uma pasta do caminho)."""

    def __init__(self, rel: str):
        super().__init__(f"{rel or '.'}: não está lá.")
        self.rel = rel


class Entry(NamedTuple):
    """Um item de `list_under`: `kind` é `file`, `dir`, `link` ou `other`; `size` só em `file`.

    `name_ok` é falso quando o nome no disco não passa nas regras de `parts_of`: `rel` então traz o
    nome com cada caractere de controle trocado por U+FFFD, e uma pasta assim não é aberta."""

    rel: str
    kind: str
    size: int | None
    name_ok: bool


class Listing(NamedTuple):
    """`entries` em ordem de nome, pasta por pasta; `complete` é falso quando um teto cortou a lista."""

    entries: tuple[Entry, ...]
    complete: bool


class Copied(NamedTuple):
    """O que `copy_under` gravou: sha256 dos bytes lidos (os mesmos gravados) e quantos foram."""

    sha256: str
    bytes: int


def name_problem(name: str) -> bool:
    """`True` quando `name` não serve como um pedaço de caminho de fora."""
    return (
        name in ("", ".", "..")
        or len(name.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES
        or "\\" in name
        or ":" in name
        or name != unicodedata.normalize("NFC", name)
        or any(unicodedata.category(char)[0] == "C" for char in name)
        or name.endswith((".", " "))
        or name.split(".", 1)[0].upper() in RESERVED_STEMS
    )


def parts_of(rel: str) -> tuple[str, ...]:
    """Pedaços de `rel` (POSIX, relativo à raiz; `""` é a própria raiz) ou `Refused("bad_name")`."""
    if type(rel) is not str:
        raise Refused("bad_name", "")
    if rel == "":
        return ()
    parts = tuple(rel.split("/"))
    if len(parts) > MAX_PARTS or any(name_problem(part) for part in parts):
        raise Refused("bad_name", _shown_name(rel))
    return parts


def _shown_name(name: str) -> str:
    return "".join("�" if unicodedata.category(char)[0] == "C" else char for char in name[:240])


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in _LINK_TAGS


def _classify(exc: OSError, dir_fd, name, rel: str, *, want_dir: bool) -> Exception:
    """A recusa certa para uma abertura que falhou: olha o item sem seguir link."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return MissingError(rel)
    except OSError as again:
        return UnreadableError(rel, type(again).__name__)
    if _is_link(info):
        return Refused("link", rel)
    if want_dir and not stat.S_ISDIR(info.st_mode):
        return Refused("not_dir", rel)
    return UnreadableError(rel, type(exc).__name__)


def _open_at(name, flags, dir_fd, rel, *, want_dir):
    try:
        # effect-exempt: read-only opens of the walk (folder and read flags only, never a write flag)
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise _classify(exc, dir_fd, name, rel, want_dir=want_dir) from None


def _walk_fd(root: Path, parts: tuple[str, ...]) -> int:
    """Descritor da pasta `root/parts...`, aberta pedaço por pedaço sem seguir link."""
    fd = _open_at(str(root), _DIR_FLAGS, None, "", want_dir=True)
    try:
        for index, name in enumerate(parts):
            child = _open_at(name, _DIR_FLAGS, fd, "/".join(parts[: index + 1]), want_dir=True)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lstat(path: Path, rel: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise MissingError(rel) from None
    except OSError as exc:
        raise UnreadableError(rel, type(exc).__name__) from None


def _walk_path(root: Path, parts: tuple[str, ...]) -> list[tuple[Path, tuple[int, int]]]:
    """Sem `dir_fd` (Windows): `lstat` da raiz e de cada pasta; devolve (pasta, identidade) de cada uma."""
    seen = []
    for index in range(len(parts) + 1):
        current = root.joinpath(*parts[:index])
        rel = "/".join(parts[:index])
        info = _lstat(current, rel)
        if _is_link(info):
            raise Refused("link", rel)
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("not_dir", rel)
        seen.append((current, (info.st_dev, info.st_ino)))
    return seen


def _recheck(seen, rel: str) -> None:
    """As pastas do caminho são as mesmas do começo (nenhuma foi trocada por link no meio)."""
    for path, identity in seen:
        info = _lstat(path, rel)
        if _is_link(info) or (info.st_dev, info.st_ino) != identity:
            raise Refused("changed", rel)


def _open_file(root: Path, rel: str):
    """`(fd, pastas a reconferir)` do arquivo `root/rel`, sem seguir link em nenhum pedaço."""
    parts = parts_of(rel)
    if not parts:
        raise Refused("not_regular", rel)
    if _BY_FD:
        folder = _walk_fd(root, parts[:-1])
        try:
            return _open_at(parts[-1], _FILE_FLAGS, folder, rel, want_dir=False), []
        finally:
            os.close(folder)
    seen = _walk_path(root, parts[:-1])
    leaf = seen[-1][0] / parts[-1]
    info = _lstat(leaf, rel)
    if _is_link(info):
        raise Refused("link", rel)
    try:
        fd = os.open(leaf, _FILE_FLAGS)
    except OSError as exc:
        raise UnreadableError(rel, type(exc).__name__) from None
    held = os.fstat(fd)
    if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise Refused("changed", rel)
    return fd, seen


def _checked(fd: int, rel: str, max_bytes: int, single_link: bool) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise Refused("not_regular", rel)
    if single_link and info.st_nlink != 1:
        raise Refused("hardlinked", rel)
    if info.st_size > max_bytes:
        raise Refused("too_big", rel)
    return info


def _read_capped(fd: int, rel: str, max_bytes: int) -> bytes:
    data = bytearray()
    while len(data) <= max_bytes:
        chunk = os.read(fd, min(_CHUNK, max_bytes + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data += chunk
    raise Refused("too_big", rel)


def open_under(root: Path, rel: str, *, max_bytes: int, single_link: bool = True) -> bytes:
    """Os bytes do arquivo `root/rel`, lidos sem seguir link, até `max_bytes`.

    `root` é uma pasta absoluta e resolvida que o core escolheu (o projeto, a pasta onde a pessoa
    guardou o arquivo); `rel` é POSIX, relativo a ela. `single_link=False` só para mídia."""
    fd, seen = _open_file(Path(root), rel)
    try:
        _checked(fd, rel, max_bytes, single_link)
        data = _read_capped(fd, rel, max_bytes)
    finally:
        os.close(fd)
    _recheck(seen, rel)
    return data


def text_under(root: Path, rel: str, *, max_bytes: int = MAX_TEXT_BYTES) -> str:
    """Texto UTF-8 (BOM tirado) de `root/rel`, sem NUL; um nome só no disco."""
    raw = open_under(root, rel, max_bytes=max_bytes)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Refused("not_text", rel) from None
    if "\x00" in text:
        raise Refused("not_text", rel)
    return text


def _no_repeats(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise ValueError(key)
        found[key] = value
    return found


def _no_constant(name):
    raise ValueError(name)


def _depth(value) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        current, level = stack.pop()
        if isinstance(current, (dict, list)):
            deepest = max(deepest, level)
            stack.extend((item, level + 1) for item in (current.values() if isinstance(current, dict) else current))
    return deepest


def json_under(root: Path, rel: str, *, max_bytes: int = MAX_JSON_BYTES) -> dict:
    """O objeto JSON de `root/rel`: sem chave repetida, NaN/Infinity nem mais de 32 níveis."""
    text = text_under(root, rel, max_bytes=max_bytes)
    try:
        value = json.loads(text, object_pairs_hook=_no_repeats, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise Refused("bad_json", rel) from None
    if not isinstance(value, dict) or _depth(value) > JSON_MAX_DEPTH:
        raise Refused("bad_json", rel)
    return value


def _entry(rel: str, info: os.stat_result, name_ok: bool) -> Entry:
    if _is_link(info):
        return Entry(rel, "link", None, name_ok)
    if stat.S_ISDIR(info.st_mode):
        return Entry(rel, "dir", None, name_ok)
    if stat.S_ISREG(info.st_mode):
        return Entry(rel, "file", info.st_size, name_ok)
    return Entry(rel, "other", None, name_ok)


def _scan(where: int | Path, rel: str):
    """`[(nome, stat sem seguir link)]` de uma pasta (descritor ou caminho), em ordem de nome."""
    try:
        with os.scandir(where) as found:
            items = [(item.name, item.stat(follow_symlinks=False)) for item in found]
    except OSError as exc:
        raise UnreadableError(rel, type(exc).__name__) from None
    return sorted(items, key=lambda pair: pair[0])


class _Walk(NamedTuple):
    """O que uma listagem acumula: os itens, as pastas a abrir e os dois tetos."""

    found: list[Entry]
    queue: list
    max_entries: int
    max_depth: int


def _visit(where: int | Path, prefix: str, level: int, walk: _Walk) -> bool:
    """Põe os itens de uma pasta em `walk.found` e as pastas filhas, já abertas, em `walk.queue`.

    A filha abre aqui, pelo descritor da mãe ainda aberto. Devolve se nada foi cortado; com a lista
    cheia, para na hora e devolve `False`."""
    whole = True
    for name, info in _scan(where, prefix):
        if len(walk.found) == walk.max_entries:
            return False
        name_ok = not name_problem(name)
        child = f"{prefix}/{_shown_name(name)}" if prefix else _shown_name(name)
        entry = _entry(child, info, name_ok)
        walk.found.append(entry)
        if entry.kind != "dir":
            continue
        if level >= walk.max_depth or not name_ok:
            whole = False
        elif isinstance(where, int):
            walk.queue.append((_open_at(name, _DIR_FLAGS, where, child, want_dir=True), child, level + 1))
        else:
            walk.queue.append((Path(where) / name, child, level + 1))
    return whole


def list_under(root: Path, rel: str = "", *, max_entries: int = MAX_ENTRIES, max_depth: int = MAX_DEPTH) -> Listing:
    """O que há em `root/rel`, sem seguir link: links são listados (`kind: link`), nunca abertos.

    Uma pasta abaixo de `max_depth` níveis não é aberta e a lista para em `max_entries`; nos dois
    casos `complete` sai falso. Uma pasta sem permissão de leitura é `UnreadableError`."""
    root = Path(root)
    parts = parts_of(rel)
    seen = [] if _BY_FD else _walk_path(root, parts)
    walk = _Walk([], [(_walk_fd(root, parts) if _BY_FD else seen[-1][0], rel, 1)], max_entries, max_depth)
    complete = True
    try:
        while walk.queue:
            where, prefix, level = walk.queue.pop(0)
            try:
                whole = _visit(where, prefix, level, walk)
            finally:
                if isinstance(where, int):
                    os.close(where)
            complete = complete and whole
    finally:
        for pending in walk.queue:
            if isinstance(pending[0], int):
                os.close(pending[0])
    _recheck(seen, rel)
    return Listing(tuple(walk.found), complete)


def copy_under(src_root: Path, rel: str, dest: Path, *, max_bytes: int, single_link: bool = False) -> Copied:
    """Copia `src_root/rel` para `dest` (novo, nunca por cima) pelo descritor, com o sha256 no mesmo passe.

    Os bytes gravados são os bytes lidos e somados: não há segunda abertura pelo caminho que um link
    trocado no meio pudesse desviar. Num erro, `dest` não fica."""
    fd, seen = _open_file(Path(src_root), rel)
    digest = hashlib.sha256()
    try:
        _checked(fd, rel, max_bytes, single_link)
        try:
            copied = safe_copy.copy_from_fd(fd, dest, max_bytes, mode=0o644, hasher=digest)
        except safe_copy.UnsafeFileError as exc:
            if exc.reason == safe_copy.TOO_BIG:
                raise Refused("too_big", rel) from None
            if exc.reason == safe_copy.TARGET_EXISTS:
                raise ValueError(f"{Path(dest).name} já existe: o getbrolls não grava por cima.") from None
            raise OSError(f"Não consegui criar {Path(dest).name} ({exc.type_name}).") from None
    finally:
        os.close(fd)
    try:
        _recheck(seen, rel)
    except (Refused, MissingError, UnreadableError):
        effects.unlink(dest, missing_ok=True)
        raise
    return Copied(digest.hexdigest(), copied)


def project_dir(project: Path, value: str) -> str:
    """`--project-dir`: uma pasta DENTRO do projeto, relativa, sem link em nenhum pedaço (spec §3.1).

    Devolve o caminho POSIX normalizado. Absoluto, com `..`, fora das regras de nome ou com um link
    ou arquivo no caminho é erro de uso (`UsageError`, saída 2). Uma pasta que ainda não existe
    passa: quem lê decide (a volta fica esperando)."""
    typed = plain_line(str(value), limit=120)
    normalized = str(value).strip().replace("\\", "/").rstrip("/")
    if not normalized or PurePosixPath(normalized).is_absolute() or PureWindowsPath(str(value)).drive:
        raise UsageError(f"--project-dir {typed!r}: use uma pasta relativa, dentro do projeto.")
    try:
        parts = parts_of(normalized)
    except Refused:
        raise UsageError(f"--project-dir {typed!r}: {PHRASES['bad_name']}.") from None
    try:
        if _BY_FD:
            os.close(_walk_fd(Path(project), parts))
        else:
            _walk_path(Path(project), parts)
    except MissingError:
        return "/".join(parts)
    except Refused as exc:
        raise UsageError(f"--project-dir {typed!r}: {exc.rel} {PHRASES[exc.reason]}.") from None
    return "/".join(parts)


def _cut(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: limit - 3].decode("utf-8", "ignore").rstrip() + "…"


def untrusted_text(raw: bytes | str, *, origin: str, limit: int = UNTRUSTED_LIMIT) -> dict:
    """Texto de fora como DADO: `{"origin", "body"}`, um parágrafo de até `limit` bytes UTF-8.

    Bytes inválidos viram U+FFFD; caractere de controle, de formatação (bidi, U+FEFF) e quebra de
    linha viram espaço; segredo (também o de chave entre aspas, JSON ou dict) e URL saem por
    `runtime.redact_diagnostic`; caminho absoluto sai por `labels.outside`.
    Nunca é ordem: quem mostra diz de onde veio (`origin`, um id)."""
    if type(origin) is not str or not NAME_RE.fullmatch(origin):
        raise ValueError("untrusted_text: origin tem que ser um id (a-z, 0-9, _).")
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = plain_line(text[: limit * 4], limit=limit)
    text = labels.outside(runtime.redact_diagnostic(text, limit=None))
    return {"origin": origin, "body": _cut(text, limit)}
