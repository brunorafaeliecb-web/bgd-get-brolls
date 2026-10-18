"""`entrega/`: a mesma coleta, organizada por beat, em nomes que uma pessoa lê.

`brolls/` continua canônico — nada aqui apaga, renomeia ou move o que está lá. Esta
camada é derivada e regenerável: cada arquivo de `clips/` aparece em `entrega/` por
hardlink (ou symlink, ou cópia, nessa ordem), com o contact sheet ao lado e um
`ORIGEM.md` dizendo de onde veio. Rodar de novo não estraga nada, e uma mídia que a
pessoa editou à mão nunca é sobrescrita: o comando termina o trabalho e então falha
nomeando todos os arquivos em conflito. O `ORIGEM.md` é derivado e refeito a cada rodada;
o contact sheet também, enquanto ainda tiver os bytes que o `deliver` entregou (o sha256
fica em `c["delivery"]["sheet_sha256"]`): um sheet editado é conflito, como a mídia.

Hardlink é o padrão porque vídeo pesa e duas cópias de cada corte dobram o projeto —
mas hardlink é o *mesmo* arquivo, com dois nomes: editar em `entrega/` editaria o
original em `brolls/clips/`. Por isso a mídia entregue nasce somente-leitura (a
proteção vale para o inode compartilhado, e nenhum comando reescreve esse arquivo no
lugar: `fetch` grava um `-r<N>` novo e `verify` só lê) e tanto o `README.md` quanto o
`ORIGEM.md` dizem isso em uma frase. Quem quiser editar dentro de `entrega/` define
`GB_DELIVERY_COPY=1` e recebe cópias independentes.

`c["delivery"]["path"]` é relativo à **raiz do projeto** (começa em `entrega/`), ao
contrário de `c["output"]["path"]`, que é relativo a `brolls/`.
"""

# pylint: disable=too-many-lines
# `too-many-lines`: a escada link/cópia e o caminho de conflito de cada item decidem juntos o que
# conta como "entregue"; separar a escada em módulo próprio fica para a divisão do delivery.py.

import contextlib
import errno
import functools
import logging
import os
import re
import stat
import unicodedata
from datetime import date
from pathlib import Path

from . import effects, labels, logs
from ._paths import strict_exists, strict_is_dir, strict_is_file
from .delivery_sweep import BEAT_DIR_RE, INDEX, Pristine, confined
from .delivery_sweep import sweep as _sweep
from .delivery_sweep import thaw_unlink as _thaw_unlink
from .delivery_sweep import unlink_keeping_mode as _unlink_keeping_mode
from .runtime import one_line, record_delivery_conflicts, record_warning

log = logs.get("delivery")

# Pasta derivada, na raiz do projeto — irmã de `brolls/`, nunca dentro dela.
DELIVERY_DIR = "entrega"
# Onde ficam os trechos coletados que nenhum beat reivindicou.
NO_BEAT = "00-sem-beat"
ORIGIN = "ORIGEM.md"
SHEET = "contact-sheet"
# Nome de pasta que ainda cabe num Finder/Explorer sem virar reticências.
MAX_NAME = 60
# Aviso do ORIGEM.md quando o arquivo é uma cópia independente.
COPY_NOTE = (
    "**Esta é uma cópia independente** (`GB_DELIVERY_COPY`, ou porque o sistema não "
    "deixou criar link): edite à vontade, o original em `brolls/` não é afetado. Rodar "
    "`deliver` de novo não mexe numa cópia que você editou — ele para e diz o nome dela."
)

# Aviso que vai no README.md e em cada ORIGEM.md: hardlink é o mesmo arquivo.
EDIT_WARNING = (
    "**Editar aqui é editar o original.** Estes arquivos são o mesmo arquivo de "
    "`brolls/` com outro nome (hardlink), e por isso vêm marcados como somente-leitura: "
    "copie antes de mexer. Se você quiser editar dentro de `entrega/`, rode o `deliver` "
    "com `GB_DELIVERY_COPY=1` e receba cópias independentes."
)


def slug(text):
    """Texto humano vira pedaço de caminho: ASCII, minúsculo, só letras/números/hífen."""
    normal = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = normal.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_only)).strip("-")


def _safe(value, field):
    """Recusa qualquer coisa que tentaria sair da pasta de entrega."""
    text = str(value or "")
    if ".." in text or "/" in text or "\\" in text or os.sep in text:
        raise ValueError(
            f'Valor inválido para o nome da pasta de entrega ({field}): "{text}". '
            "Ids de beat usam só letras minúsculas, números e hífen; corrija o BRIEF.md "
            "ou o --shot do candidato."
        )
    return text


def beat_dir_name(nn, beat_id, target):
    """`NN-<beat.id>-<slug(alvo)>`: estável entre execuções e seguro como caminho."""
    _safe(beat_id, "id do beat")
    _safe(target, "alvo do beat")
    prefix = f"{int(nn):02d}"
    parts = [prefix, slug(beat_id), slug(target)]
    name = "-".join(p for p in parts if p)
    name = re.sub(r"-+", "-", name).strip("-")[:MAX_NAME].rstrip("-")
    if not name or name in (".", "..") or not BEAT_DIR_RE.match(name):
        raise ValueError(
            f'Não consegui montar um nome de pasta seguro para o beat "{beat_id}". '
            "Use um id com letras minúsculas, números e hífen."
        )
    return name


class _CompareError(Exception):
    """Raised when a comparison could not complete because of an OSError.

    Kept distinct from a real content difference: `link_or_copy` must never report
    "parece edição sua" when it simply failed to read one of the two files.
    """


def _same_file(a, b):
    try:
        return os.path.samestat(Path(a).stat(), Path(b).stat())
    except OSError as exc:
        raise _CompareError(labels.reason(exc)) from exc


def _same_bytes(a, b, block=1024 * 1024):
    """Compara em blocos, nunca carregando um vídeo inteiro na memória."""
    try:
        if Path(a).stat().st_size != Path(b).stat().st_size:
            return False
        with Path(a).open("rb") as left, Path(b).open("rb") as right:
            while True:
                chunk = left.read(block)
                if chunk != right.read(block):
                    return False
                if not chunk:
                    return True
    except OSError as exc:
        raise _CompareError(labels.reason(exc)) from exc


def _compared(compare, dest, src):
    """Run one comparison; an I/O failure is reported as such, never as a user edit."""
    try:
        return compare(dest, src)
    except _CompareError as exc:
        raise ValueError(
            f"Não consegui comparar {labels.shown(dest)} com o arquivo coletado ({exc}): confira "
            "permissão ou disponibilidade do arquivo e rode `deliver` de novo."
        ) from exc


def copies_forced():
    """`GB_DELIVERY_COPY=1`: cópias independentes para quem quer editar em `entrega/`."""
    return (os.environ.get("GB_DELIVERY_COPY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _freeze(path, method):
    """Tira a escrita só de quem compartilha bytes com o original.

    Hardlink e symlink são o mesmo arquivo com outro nome: editar ali editaria
    `brolls/`. Uma cópia é independente — congelá-la quebraria a promessa de
    `GB_DELIVERY_COPY=1`, que existe justamente para quem quer editar em `entrega/`.

    Uma falha aqui fica não fatal (o resto da entrega continua), mas nunca some: vai
    para `record_warning`, o mesmo canal de aviso que o resto do projeto usa, para que
    a promessa de "somente-leitura" não fique silenciosamente falsa.
    """
    if method not in ("hardlink", "symlink"):
        return
    try:
        mode = Path(path).stat().st_mode
        Path(path).chmod(mode & ~0o222)
    except OSError as exc:
        record_warning(
            "DELIVERY_FREEZE_FAILED",
            f"Não consegui deixar {labels.shown(path)} somente-leitura ({labels.reason(exc)}): o arquivo entregue "
            "compartilha o inode com o original em brolls/, mas ficou editável.",
        )


def _writable(path, shown=None):
    """Devolve a escrita do dono a uma cópia: `copy2` herda o modo do clipe congelado.

    Uma cópia é independente (é a promessa de `GB_DELIVERY_COPY=1`); nascer
    somente-leitura só porque o original em `brolls/` estava congelado a deixaria
    impossível de editar. A falha vira aviso, como em `_freeze`, nomeando `shown` (o
    nome final, quando `path` ainda é o `.part`).
    """
    try:
        mode = Path(path).stat().st_mode
        if not mode & stat.S_IWUSR:
            Path(path).chmod(mode | stat.S_IWUSR)
    except OSError as exc:
        record_warning(
            "DELIVERY_COPY_READ_ONLY",
            f"Não consegui deixar a cópia {labels.shown(shown or path)} editável ({labels.reason(exc)}): "
            "ela é independente do "
            "original em brolls/, mas ficou somente-leitura.",
        )


def _single_name(path):
    """`path` é o único nome do arquivo: uma cópia de verdade, não outro nome de um clipe de `brolls/`."""
    try:
        return Path(path).stat().st_nlink == 1
    # lenient: a file it cannot stat is not proven to be a single name, so it is not made editable
    except OSError:
        return False


def _copy_whole(src, dest, editable=True, linked=None):
    """Copia `src` para `dest` por um `<dest>.part` ao lado, e só o arquivo inteiro ganha o nome.

    Uma cópia cortada no meio (Ctrl+C, disco cheio) deixava um arquivo truncado com o
    nome final, e a rodada seguinte o tomava por edição da pessoa. Qualquer falha
    apaga o `.part`; uma sobra de rodada morta à força sai antes da cópia nova.
    `editable=False` (o `export`) deixa o modo que o `copy2` trouxe. `linked` é o clipe
    de que `dest` ainda é hardlink: o `.part` toma o lugar dele (`_take_name`).
    """
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    # lenient(2): leftover .part of a killed run; if its folder is locked, copy2 below raises the EACCES
    if part.is_symlink() or part.exists():
        _thaw_unlink(part)
    try:
        effects.copy_file(src, part, metadata=True)
        if editable:
            _writable(part, shown=dest)
        _take_name(part, dest, linked)
    except BaseException:
        with contextlib.suppress(OSError):
            # lenient(2): cleanup of our own .part; must not mask the copy error
            if part.is_symlink() or part.exists():
                _thaw_unlink(part)
        raise


def _take_name(part, dest, linked):
    """`part` vira `dest`; no POSIX a troca substitui de uma vez o hardlink que estava ali.

    No Windows a troca recusa um destino somente-leitura (o link congelado de `linked`):
    só então o nome sai, sem destravar o clipe, e a troca é refeita.
    """
    with effects.op("fs.replace", dest) as first:
        try:
            part.replace(dest)
            return
        except PermissionError:
            # lenient: inside except PermissionError; re-raises the original error
            if linked is None or not dest.exists():
                raise
            # Só agora se sabe que é o caso do Windows (o link congelado no lugar): a tentativa foi
            # recusada como esperado, e a troca é refeita depois de o nome sair.
            first.skip(fallback="eacces")
    _unlink_keeping_mode(dest, linked)
    effects.replace(part, dest)


def _copy_over_link(src, dest, editable):
    """Troca o hardlink `dest` de `src` por uma cópia, sem tirar o link antes de a cópia estar inteira.

    Apagar primeiro deixava o item sem arquivo em `entrega/` (e o manifesto dizendo
    "hardlink") quando a cópia falhava no meio. O disco cheio é conflito do item.
    """
    try:
        _copy_whole(src, dest, editable=editable, linked=src)
    except OSError as exc:
        raise ValueError(
            f"Não consegui trocar {labels.shown(dest)} por uma cópia independente ({labels.reason(exc)}): "
            "confira espaço e "
            "permissão de escrita na pasta do projeto e rode `deliver` de novo."
        ) from exc


def _settle_existing(src, dest, read_only, forced, editable):
    """O que fazer com um `dest` que já existe: o método que ele já é, ou `None` quando saiu.

    Link para `src` continua sendo o mesmo arquivo, a não ser com `forced` (cópia
    pedida): aí a cópia nasce no lugar dele (o hardlink só sai quando ela está inteira,
    e o método já é `"copy"`). Cópia com os mesmos bytes fica — e,
    com `editable`, volta a ser editável se uma versão antiga a deixou somente-leitura.
    Conteúdo diferente é obra da pessoa e o erro nomeia o arquivo.
    """
    # lenient: a link in place of the delivered file is replaced; strict_exists below sees the rest
    if dest.is_symlink():
        try:
            if dest.resolve() == src.resolve() and not forced:
                return "symlink"
        except OSError:
            pass
        _thaw_unlink(dest)
        return None
    if not strict_exists(dest):
        return None
    if _compared(_same_file, dest, src):
        if not forced:
            if read_only:
                _freeze(dest, "hardlink")
            return "hardlink"
        # Mesmo inode do clipe: só um nome a menos, o original não muda.
        _copy_over_link(src, dest, editable)
        return "copy"
    if _compared(_same_bytes, dest, src):
        # A versão antiga copiava o modo do clipe congelado (0444). Só um arquivo de nome
        # único é cópia de verdade: outro nome de um clipe congelado continua congelado.
        if editable and _single_name(dest):
            _writable(dest)
        return "copy"
    raise _edited(dest)


class _EditedError(ValueError):
    """`dest` já existe com conteúdo diferente do arquivo coletado (talvez edição da pessoa)."""


def _edited(dest):
    """O erro de um `dest` que a pessoa mudou: o mesmo texto na rodada real e no ensaio."""
    return _EditedError(
        f"{labels.shown(dest)} já existe com conteúdo diferente do arquivo coletado: parece edição "
        "sua e eu não sobrescrevo. Renomeie ou apague esse arquivo e rode `deliver` "
        "de novo."
    )


def _refuse_if_edited(src, dest):
    """O veredito de `link_or_copy` sobre um `dest` que já existe, só lendo (para o ensaio).

    Link ou cópia com os mesmos bytes passam; um symlink para outro lugar seria
    refeito e também passa. O resto levanta o mesmo erro da rodada real.
    """
    src, dest = Path(src), Path(dest)
    # lenient: link test; strict_exists on the same line raises for a locked folder
    if dest.is_symlink() or not strict_exists(dest):
        return
    if _compared(_same_file, dest, src) or _compared(_same_bytes, dest, src):
        return
    raise _edited(dest)


# Um link que não deu (outro volume, sistema sem link, sem privilégio, limite de links) cai no método
# seguinte: no `event=op` é `outcome=skipped` com o motivo (`fallback=exdev`). Outro erro (EIO, EROFS)
# sai como erro, mesmo que a cópia depois dê certo. A cópia é o último.
_LINK_FALLBACK = (
    NotImplementedError,
    AttributeError,
    errno.EXDEV,
    errno.EPERM,
    errno.EMLINK,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.ENOSYS,
    errno.EACCES,
)


def link_or_copy(src, dest, read_only=False, allow_symlink=True, *, editable_copy=True):
    """Liga `dest` a `src` pelo jeito mais barato que o sistema aceitar.

    Hardlink primeiro (não ocupa disco e não quebra ao mover a pasta de dentro),
    symlink depois (no Windows pode faltar privilégio) e cópia por último. Devolve o
    método usado. Se `dest` já é o mesmo arquivo, não faz nada; se é um arquivo comum
    com conteúdo diferente, é obra da pessoa e o erro nomeia o arquivo.
    `allow_symlink=False` pula o symlink (hardlink ou cópia): quem monta um projeto
    que vai para outra máquina não pode depender de link para fora da pasta.
    Com `GB_DELIVERY_COPY=1`, um `dest` que ainda é link para `src` (de uma entrega
    anterior) sai e vira cópia: editar ali editaria o original. Toda cópia de `entrega/`
    nasce editável; `editable_copy=False` (o `export`) deixa o modo como o `copy2` trouxe.
    """
    src, dest = Path(src), Path(dest)
    forced = copies_forced()
    copy = functools.partial(_copy_whole, editable=editable_copy)
    methods = (
        (("copy", copy),)
        if forced
        else (
            ("hardlink", functools.partial(effects.link, fallback=_LINK_FALLBACK)),
            *(
                (("symlink", functools.partial(effects.link, symbolic=True, fallback=_LINK_FALLBACK)),)
                if allow_symlink
                else ()
            ),
            ("copy", copy),
        )
    )
    settled = _settle_existing(src, dest, read_only, forced, editable_copy)
    if settled is not None:
        return settled
    effects.mkdir(dest.parent, parents=True, exist_ok=True)
    for method, make in methods:
        try:
            make(str(src), str(dest))
            if read_only:
                _freeze(dest, method)
            return method
        except (OSError, NotImplementedError, AttributeError):
            # lenient(2): cleanup after a failed link/copy; must not mask that error
            if dest.is_symlink() or dest.exists():
                _thaw_unlink(dest)
    raise OSError(
        f"Não consegui ligar nem copiar {labels.shown(src)} para {labels.shown(dest)}. Confira espaço e permissão "
        "de escrita na pasta do projeto."
    )


def _frontmatter(kind, created, tags):
    today = date.today().isoformat()  # noqa: DTZ011 - local date; tz-aware would shift day near midnight
    return [
        "---",
        f"type: {kind}",
        "status: current",
        f"created: {created or today}",
        f"updated: {today}",
        f"tags: [{', '.join(tags)}]",
        "---",
        "",
    ]


def _created_in(path):
    """Preserva o `created` de um arquivo já gerado: repetir não muda o histórico."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:8]:
            if line.startswith("created:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _segment_label(c):
    start = (c.get("segment") or {}).get("start_s")
    end = (c.get("segment") or {}).get("end_s")
    if start is None or end is None:
        return "não definido"
    return f"{start:g}s → {end:g}s"


def _author(c):
    """Quem assina a fonte: `creator.name` quando existe, senão o canal/uploader bruto.

    Provedores de vídeo costumam devolver só `channel`/`uploader`; dizer "não
    informado" com o nome do canal na mão esconde crédito que existe.
    """
    for value in (
        (c.get("creator") or {}).get("name"),
        c.get("channel"),
        c.get("uploader"),
        (c.get("media") or {}).get("channel"),
        (c.get("media") or {}).get("uploader"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "não informado"


def plugin_label(c):
    """Id do plugin dono da fonte do candidato, ou `None` para as fontes embutidas.

    Só lê manifesto/pin (`loader.declared_by`, nunca roda código de plugin); plugin
    que saiu da pasta desde a busca cai no nome da fonte gravado no candidato."""
    from .providers import BUILTIN_CAPABILITIES

    provider = c.get("provider")
    if not isinstance(provider, str) or provider in BUILTIN_CAPABILITIES:
        return None
    from .sdk import loader

    row = loader.declared_by(provider)
    return row["id"] if row else provider


# Só os caracteres que abrem marcação dentro de uma linha: `<img>`, `[link](url)`,
# `**ênfase**`, `` `código` ``, `|` de tabela, `%%comentário%%` e `==realce==` do
# Obsidian, `~~riscado~~`, `#tag`, `$matemática$`, `{atributo}`. Ponto, vírgula, hífen e
# dois-pontos ficam como estão: o texto continua legível colado numa descrição.
_MARKDOWN_SIGNIFICANT = re.compile(r"([\\`*_\[\]()<>!|%~=#${}])")
# Autolink sem `<>`: só o gatilho é quebrado (`https\://`, `www\.`).
_AUTOLINK_SCHEME = re.compile(r"://")
_AUTOLINK_WWW = re.compile(r"(?i)\b(www)\.")
# Prefixos que o core escreve na frente de um texto de plugin guardado como evidência.
_PLUGIN_EVIDENCE_PREFIXES = ("Licença registrada pelo plugin ", "Condições informadas pelo plugin ")


def inert(value, plugin):
    """`value` numa linha só e, se veio de um plugin (`plugin` não é `None`), com todo
    caractere que Markdown/HTML interpreta escapado por barra invertida: um título com
    `<img>`, `[link](url)` ou `**negrito**` sai como texto em ORIGEM.md/credits.md, nunca
    como imagem remota, link ou ênfase. Fonte embutida: só `one_line`, texto idêntico."""
    text = one_line(value)
    if plugin is None:
        return text
    text = _MARKDOWN_SIGNIFICANT.sub(r"\\\1", text)
    text = _AUTOLINK_SCHEME.sub(r"\\://", text)
    return _AUTOLINK_WWW.sub(r"\1\\.", text)


def evidence_line(evidence, plugin):
    """Itens de evidência juntos por `"; "`. Num candidato de plugin, o texto depois do
    prefixo que o core pôs num registro do plugin (licença, condições do preset) sai
    inerte; o que a pessoa disse sai como ela disse."""
    items = []
    for item in evidence or []:
        text = one_line(item)
        if plugin is not None and text.startswith(_PLUGIN_EVIDENCE_PREFIXES):
            head, sep, tail = text.partition(": ")
            text = head + sep + inert(tail, plugin) if sep else inert(text, plugin)
        items.append(text)
    return "; ".join(items)


def source_label(c, plugin):
    """Linha "Fonte:" de ORIGEM.md/credits.md. Fonte embutida: a URL ou "original
    local", como sempre; fonte de plugin: o plugin e a URL (ou "arquivo local") —
    antes saía "original local" sem dizer de onde o arquivo veio.
    `plugin` é o `plugin_label(c)` que quem chama já calculou."""
    url = c.get("source_url")
    if plugin is None:
        return url or "original local"
    return f"plugin {plugin} ({inert(url, plugin) if url else 'arquivo local'})"


def render_origin(c, media_name, created=None, method="hardlink", canonical=None):
    """`ORIGEM.md` do trecho: fonte, autor, intervalo, direitos e sha256 do arquivo.

    `canonical` é onde o original está, relativo à raiz do projeto (`broll/…` no
    layout 1); sem ele, `brolls/` + o caminho lógico do manifesto, como sempre.
    """
    rights = c.get("rights") or {}
    approval = c.get("approval") or {}
    plugin = plugin_label(c)
    lines = _frontmatter("delivery-origin", created, ["get-brolls", "entrega"])
    # pylint: disable-next=fixme
    # Todo valor vindo do candidato passa por `one_line`: um título/licença/evidência
    # com quebra de linha (plugin ou ledger editado à mão) nunca forja outra linha
    # "- Direitos:"/"- Aprovado por:" neste registro. Texto normal sai idêntico.
    lines += [
        f"# Origem de {one_line(media_name)}",
        "",
        f"- Arquivo: `{one_line(media_name)}`",
        f"- Candidato: `{one_line(c['id'])}`",
        f"- Título na fonte: {inert(c.get('title') or 'não informado', plugin)}",
        f"- Fonte: {one_line(source_label(c, plugin))}",
        f"- Autor: {inert(_author(c), plugin)}",
        f"- Trecho usado: {_segment_label(c)}",
        f"- Direitos: {one_line(rights.get('status') or 'unknown')}",
        f"- Licença: {inert(rights.get('license_name') or 'ver evidência', plugin)}",
        f"- Licença URL: {inert(rights.get('license_url') or 'não informada', plugin)}",
        "- Evidência: " + (evidence_line(rights.get("evidence"), plugin) or "não registrada"),
        f"- Aprovado por: {one_line(approval.get('by') or 'não registrado')}"
        + (f" ({one_line(approval.get('channel'))})" if approval.get("channel") else ""),
        f"- sha256 do arquivo coletado: `{one_line((c.get('output') or {}).get('sha256') or 'não calculado')}`",
        "- Original canônico: `"
        + (one_line(canonical) if canonical else f"brolls/{one_line((c.get('output') or {}).get('path'))}")
        + "`",
        "",
        EDIT_WARNING if method in ("hardlink", "symlink") else COPY_NOTE,
        "",
        (
            "Este arquivo é gerado por `deliver`. A pasta `entrega/` inteira pode ser apagada "
            "e refeita: o que vale é `brolls/`."
        ),
        "",
    ]
    return "\n".join(lines)


# Numa entrega misturada (parte link, parte cópia), nenhum aviso global é verdade: um
# diz "editar aqui é editar o original" sobre arquivos que são cópias independentes, o
# outro diz o contrário sobre hardlinks. A resposta vira coluna, linha a linha.
MIXED_NOTE = (
    "**Esta entrega tem os dois casos.** Parte dos arquivos é o mesmo arquivo de "
    "`brolls/` com outro nome (hardlink) e parte é cópia independente — o sistema não "
    "deixou criar link para todos. A coluna **Edição** diz qual é qual: "
    "`original compartilhado` vem somente-leitura e editar ali edita o original "
    "(copie antes de mexer); `editável` você muda à vontade. Para receber tudo como "
    "cópia, rode o `deliver` com `GB_DELIVERY_COPY=1`."
)
# Rótulo por linha quando a entrega é misturada.
EDIT_LABELS = {"copy": "editável", "hardlink": "original compartilhado", "symlink": "original compartilhado"}


def _mixed(rows):
    """A entrega tem cópia e link ao mesmo tempo? Aí nenhum aviso global serve."""
    kinds = {"copy" if row.get("method") == "copy" else "link" for row in rows if row.get("method")}
    return len(kinds) > 1


def render_index(rows, for_human=None, created=None, conflicts=(), copies=False):
    """`entrega/README.md`: a tabela que responde “onde estão meus arquivos”."""
    mixed = _mixed(rows)
    columns = ["beat", "narration", "target", "file", "state", "rights"]
    headers = ["Beat", "Narração", "Alvo", "Arquivo", "Estado", "Direitos"]
    if mixed:
        columns.append("edit")
        headers.append("Edição")
    lines = _frontmatter("delivery-index", created, ["get-brolls", "entrega"])
    lines += [
        "# Seus trechos",
        "",
        (
            "Cada pasta é um trecho do vídeo, na ordem do BRIEF.md. É só arrastar o `.mp4` "
            "para o seu editor; o `ORIGEM.md` ao lado diz de onde ele veio e o que você me "
            "disse sobre poder usar."
        ),
        "",
        MIXED_NOTE if mixed else (COPY_NOTE if copies else EDIT_WARNING),
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]
    for row in rows:
        cells = dict(row)
        if mixed:
            cells["edit"] = EDIT_LABELS.get(row.get("method") or "", "—")
        lines.append(
            "| " + " | ".join(str(cells.get(key) or "—").replace("|", "/").replace("\n", " ") for key in columns) + " |"
        )
    if not rows:
        # A linha vazia sai das mesmas colunas do cabeçalho: contar células à mão
        # deixava a tabela torta (5 células para 6 colunas) em todo projeto sem clipe.
        empty = dict.fromkeys(columns, "—")
        empty["file"] = "nenhum trecho coletado ainda"
        lines.append("| " + " | ".join(empty[key] for key in columns) + " |")
    if conflicts:
        # Texto puro é arquivo editado pela pessoa; `(arquivo, motivo)` é outro motivo.
        edited = [item for item in conflicts if isinstance(item, str)]
        other = [item for item in conflicts if not isinstance(item, str)]
        lines += ["", "## Conflitos", ""]
        if edited:
            lines += [
                (
                    "Estes trechos não foram refeitos porque o arquivo em `entrega/` tem conteúdo "
                    "diferente do que está em `brolls/` — parece edição sua e eu não sobrescrevo. "
                    "Renomeie ou apague o arquivo e rode `deliver` de novo:"
                ),
                "",
            ]
            lines += [f"- `{item}`" for item in edited]
        if other:
            lines += [*([""] if edited else []), "Estes trechos ficaram de fora pelo motivo dito em cada linha:", ""]
            lines += [f"- `{path}`: {note}" for path, note in other]
    lines += ["", "## Próximo passo", "", for_human or "Nada pendente por aqui.", ""]
    return "\n".join(lines)


def _brief_beats(project, rules=None):
    """Beats do BRIEF.md validado com o RULES.md (`rules`, lido do projeto quando falta).

    Sem BRIEF.md, lista vazia: a entrega segue a ordem do manifesto. Com um BRIEF.md
    inválido, recusa: a ordem e o nome das pastas sairiam do manifesto, e a varredura
    apagaria a entrega que o brief tinha organizado.
    """
    from .brief import brief_path, load_brief, validate_brief

    if not os.environ.get("GB_BRIEF_FILE") and not strict_exists(brief_path(project)):
        return []
    if rules is None:
        from .rules import load_rules

        rules = load_rules(project)
    try:
        data, _ = validate_brief(load_brief(project), rules, project=project)
    except ValueError as exc:
        raise ValueError(
            f"Não refiz entrega/ (nada foi mexido nela): o BRIEF.md é que diz a ordem e o nome "
            f"das pastas, e ele não está válido. {labels.reason(exc)}"
        ) from exc
    return [b["resolved"] for b in data["beats"]]


RETIRED_REASON = "beat aposentado pelo roteiro"


def _drop_retired(project, collected, retired=None, log_event="deliver_skipped"):
    """`collected` sem os clipes de beat aposentado; cada um vai para `retired`, se for lista.

    `log_event` nomeia o evento de log de cada clipe deixado de fora (o export usa o dele).
    """
    from .brief import retired_beat_ids

    gone = retired_beat_ids(project)
    if not gone:
        return collected
    kept = []
    for c in collected:
        if c.get("shot") not in gone:
            kept.append(c)
            continue
        if retired is not None:
            retired.append({"id": c["id"], "shot": c["shot"], "reason": RETIRED_REASON})
        logs.event(log, logging.INFO, log_event, candidate=c["id"], reason="retired_beat")
    return kept


def deliverable(project, items, retired=None, log_event="deliver_skipped"):
    """(clipes entregáveis na ordem do manifesto, pulados com o motivo): o predicado do `deliver`.

    Clipe cujo `shot` é um beat aposentado (`"retired": true`, gravado pelo sync do
    roteiro) sai e, quando `retired` é uma lista, entra nela com o motivo. O arquivo
    em `brolls/` fica como está.

    Fica de fora quem não tem `output.path`, quem tem a chave `output.verified`
    presente com um valor falso (`False`, `0`, `None` — verificação de sha256 que
    falhou ou nunca rodou) e quem foi rejeitado na aprovação — mesmo que `reject` já
    limpe `output` nesses casos, a checagem aqui é defesa extra. Um item sem a chave
    `verified` (manifesto antigo) segue endereçado normalmente; a mesma regra que
    `STAGE_TESTS["verified"]` usa em `commands.py`. `log_event` nomeia o evento de
    log de cada pulo (`deliver_skipped` no `deliver`, `export_skipped` no export).
    """
    collected, skipped = [], []
    for c in items:
        output = c.get("output") or {}
        if not output.get("path"):
            continue
        if "verified" in output and not output["verified"]:
            skipped.append(
                {
                    "id": c["id"],
                    "reason": (
                        "o arquivo em brolls/ não confere mais com o que foi coletado: restaure o "
                        "original, ou apague esse arquivo e rode `fetch` de novo; depois, `verify`."
                    ),
                }
            )
            logs.event(log, logging.INFO, log_event, candidate=c["id"], reason="unverified")
            continue
        if (c.get("approval") or {}).get("status") == "rejected":
            skipped.append({"id": c["id"], "reason": "candidato foi rejeitado: não entra na entrega."})
            logs.event(log, logging.INFO, log_event, candidate=c["id"], reason="rejected")
            continue
        collected.append(c)
    return _drop_retired(project, collected, retired, log_event), skipped


def _plan_group_order(project, collected, rules=None):
    """Ordem dos beats (do brief, mais os shots extras que não estão nele) e seus metadados."""
    beats = _brief_beats(project, rules)
    order = [b["id"] for b in beats]
    meta = {b["id"]: b for b in beats}
    for c in collected:
        shot = c.get("shot")
        if shot and shot not in order:
            order.append(shot)
    return order, meta


def _plan_beat_group(position, beat_id, beat, collected):
    """Grupo de um beat: membros, alvo (com plugin inerte quando vem do título) e pasta; None se vazio."""
    members = [c for c in collected if c.get("shot") == beat_id]
    if not members:
        return None
    target = beat.get("target") or members[0].get("title") or beat_id
    # Alvo que veio do título na fonte de um plugin: sai inerte na tabela do README
    # (a pasta continua saindo do texto original, por `slug`).
    from_title = not beat.get("target") and members[0].get("title")
    title_plugin = plugin_label(members[0]) if from_title else None
    return {
        "beat": beat_id,
        "narration": beat.get("narration"),
        "target": target,
        "target_label": inert(target, title_plugin) if title_plugin else target,
        # O alvo é texto humano (do brief, ou o título do candidato quando o beat
        # não tem alvo): "AC/DC ao vivo" tem barra e não é caminho. A pasta sai do
        # slug; o texto de verdade segue em `target` e no ORIGEM.md.
        "dir": beat_dir_name(position, beat_id, slug(target)),
        "items": members,
    }


def _plan(project, items, retired=None, rules=None):
    """Um grupo por beat, na ordem do brief; sem brief, na ordem do manifesto.

    Quem entra é decidido por `deliverable` (o mesmo predicado do export).
    """
    collected, skipped = deliverable(project, items, retired)
    order, meta = _plan_group_order(project, collected, rules)
    groups = []
    for position, beat_id in enumerate(order, start=1):
        group = _plan_beat_group(position, beat_id, meta.get(beat_id) or {}, collected)
        if group is not None:
            groups.append(group)
    orphans = [c for c in collected if not c.get("shot")]
    if orphans:
        groups.append(
            {
                "beat": None,
                "narration": None,
                "target": "trechos sem beat no brief",
                "dir": NO_BEAT,
                "items": orphans,
            }
        )
    return groups, skipped


def _names(group_dir, index, source_suffix, sheet_suffix):
    tail = "" if index == 1 else f"-{index}"
    return {
        "media": f"{group_dir}{tail}{source_suffix}",
        "sheet": f"{SHEET}{tail}{sheet_suffix}",
        "origin": ORIGIN if index == 1 else f"ORIGEM{tail}.md",
    }


def _deliver_refuse_symlink(root):
    """Recusa `deliver` se `entrega/` já existe como link simbólico (varrer seguiria o alvo)."""
    # lenient: refuses entrega/ as a link; a locked entrega/ fails in the sweep's strict_is_dir
    if not root.is_symlink():
        return
    logs.event(log, logging.WARNING, "deliver_refusal", reason="entrega_is_symlink")
    raise ValueError(
        f"{labels.shown(root)} é um link simbólico: apague o link antes de rodar `deliver`. A pasta "
        "de entrega precisa ser uma pasta real dentro do projeto, nunca um atalho para "
        "outro lugar."
    )


class _DeliverCtx:  # pylint: disable=too-few-public-methods
    # Contêiner simples de estado, não um objeto com comportamento.
    """Ledger, pasta de entrega e modo dry-run, compartilhados por todo `build_delivery`."""

    def __init__(self, ledger, root, dry_run, pristine=None):
        self.ledger = ledger
        self.root = root
        self.dry_run = dry_run
        # Os clipes de `brolls/` (todas as revisões) que um arquivo de `entrega/` pode ainda ser.
        self.pristine = pristine or Pristine(())


class _ClipMissingError(ValueError):
    """O manifesto diz que o clipe foi coletado e conferido, mas o arquivo não está no disco."""

    def __init__(self, label):
        # Linha curta para o README; a mensagem inteira vai para o erro do `deliver`.
        self.note = f"o clipe `{label}` sumiu; rode `verify` e depois `fetch` de novo"
        super().__init__(
            f"O clipe {label} sumiu: o manifesto diz que ele foi coletado e conferido, mas o "
            "arquivo não está lá. Restaure o arquivo, ou rode `verify` (ele marca o clipe "
            "como não conferido) e depois `fetch` de novo."
        )


def _clip_label(ctx, c, source):
    """Onde o clipe deveria estar, relativo ao projeto, para a mensagem."""
    try:
        return source.resolve().relative_to(ctx.ledger.root.parent.resolve()).as_posix()
    except (OSError, ValueError):
        return f"brolls/{c['output']['path']}"


def _deliver_materialize_item(ctx, group, c, names, sheet):
    """Liga/copia mídia e contact sheet deste item (no ensaio, só confere o destino).

    Devolve (método ou None, conflito ou None, motivo para o README ou None, sha256 do
    contact sheet entregue ou None): o motivo só existe quando o conflito não é um
    arquivo editado pela pessoa.
    """
    media_rel = f"{group['dir']}/{names['media']}"
    sheet_rel_out = f"{group['dir']}/{names['sheet']}" if sheet else None
    method = sheet_sha = None
    try:
        # O `ValueError` daqui vira conflito do item, e o `deliver` segue: a recusa que
        # chegar ao log não traz `error_code` (`logs.handled`).
        with logs.handled():
            # O mesmo nome em `broll/` e na antiga `brolls/clips/` sem desempate pelo
            # sha256 vira conflito nomeado, como o arquivo editado à mão.
            source = _clip_source(ctx, c)
            if not strict_is_file(source):
                # Pular calado deixava o item "entregue" com `method: None` e nada em
                # `entrega/`: vira conflito, e o registro de entrega fica como estava.
                raise _ClipMissingError(_clip_label(ctx, c, source))
            if ctx.dry_run:
                # O ensaio só lê, mas vê o mesmo conflito que a rodada real levantaria,
                # na mídia e no contact sheet (`_place_sheet` também só confere).
                try:
                    _refuse_if_edited(source, ctx.root / media_rel)
                except _EditedError:
                    if not _drop_stale_revision(ctx, ctx.root / media_rel):
                        raise
                method = "planned"
            else:
                # Só hardlink/symlink são congelados: cópia é independente.
                method = _place_media(ctx, source, ctx.root / media_rel)
                logs.event(
                    log,
                    logging.INFO,
                    "deliver_item",
                    beat=group["beat"] or "no_beat",
                    mode=method,
                    # `link_or_copy` doesn't report a per-attempt failure reason
                    # (its signature is shared with tests that stub it out with a
                    # 3-arg fake), so only the one fallback cause this call site
                    # can know for certain — a forced copy — is named here.
                    reason="env_copy" if copies_forced() else None,
                )
            if sheet and sheet_rel_out and strict_is_file(sheet):
                # O contact sheet não é congelado: `preview` regrava o arquivo
                # de origem no mesmo caminho quando a pessoa muda o intervalo.
                sheet_sha = _place_sheet(ctx, c, sheet, ctx.root / sheet_rel_out)
    except ValueError as exc:
        from .layout import ClipConflictError

        conflict = str(exc)
        if isinstance(exc, ClipConflictError):
            kind = "clip_conflict"
        elif isinstance(exc, _ClipMissingError):
            kind = "clip_missing"
        else:
            kind = "foreign_file" if "parece edição sua" in conflict else "io_error"
        logs.event(log, logging.WARNING, "deliver_conflict", beat=group["beat"] or "no_beat", kind=kind)
        return method, conflict, getattr(exc, "note", None), None
    return method, None, None, sheet_sha


def _drop_stale_revision(ctx, dest):
    """Tira de `entrega/` o arquivo planejado que ainda é outra revisão de um clipe; diz se tirou.

    O nome em `entrega/` não muda entre revisões do mesmo item: depois de um corte novo
    (ou de `reject` e outro `fetch`), o que está ali ainda é o link ou a cópia intocada do
    clipe antigo, que continua em `brolls/` — nosso, não edição da pessoa. No ensaio, só
    diz. Um hardlink sai sem destravar o clipe que compartilha o inode. Só dentro da
    `entrega/` real: atrás de uma pasta de beat virada link, o arquivo fica e é conflito.
    """
    if not _inside_delivery(ctx, dest):
        return False
    match = ctx.pristine.match(dest)
    if match is None:
        return False
    clip, same_inode = match
    if not ctx.dry_run:
        _make_room(dest, "a revisão nova", clip if same_inode else None)
    return True


def _make_room(dest, what, linked=None):
    """Tira `dest` de `entrega/` (sem destravar `linked`, o clipe do mesmo inode); a falha é conflito do item.

    No Windows, um arquivo aberto no editor não sai (e uma pasta de beat sem escrita, em
    qualquer sistema): o `OSError` passaria por cima do `except ValueError` do item e
    abortaria o `deliver` antes dos outros beats, do manifesto e do README.
    """
    try:
        if linked is None:
            _thaw_unlink(dest)
        else:
            _unlink_keeping_mode(dest, linked)
    except OSError as exc:
        raise ValueError(
            f"Não consegui tirar {labels.shown(dest)} de entrega/ para pôr {what} ({labels.reason(exc)}): "
            "se ele está aberto num "
            "editor, feche e rode `deliver` de novo."
        ) from exc


def _place_media(ctx, source, dest):
    """`link_or_copy` da mídia; o link ou a cópia intocada de outra revisão dá lugar à revisão nova.

    Uma linha `deliver.place` por mídia posta em `entrega/`, além das de cada link ou cópia."""
    with effects.op("deliver.place", dest):
        try:
            return link_or_copy(source, dest, read_only=True)
        except _EditedError:
            if not _drop_stale_revision(ctx, dest):
                raise
        return link_or_copy(source, dest, read_only=True)


def _inside_delivery(ctx, dest):
    """`dest` mora de fato dentro de `entrega/`: o mesmo confinamento da varredura antes de apagar."""
    return confined(dest, ctx.root.resolve())


def _place_sheet(ctx, c, sheet, dest):
    """`link_or_copy` do contact sheet; o sheet que o `deliver` deixou ali dá lugar ao novo.

    O `preview` regrava o sheet no mesmo caminho (inode novo) quando o intervalo muda; o
    que estava em `entrega/` só é a prévia velha quando ainda tem os bytes que o último
    `deliver` deste item entregou (`delivery.sheet_sha256`). Um sheet que a pessoa editou
    (a cópia do `GB_DELIVERY_COPY=1`, ou salvo por cima do link) não bate com isso e é
    conflito, como a mídia; atrás de uma pasta de beat virada link para fora de
    `entrega/`, também. No ensaio, só confere. Devolve o sha256 do sheet entregue.
    """
    try:
        if ctx.dry_run:
            _refuse_if_edited(sheet, dest)
            return None
        link_or_copy(sheet, dest)
    except _EditedError:
        if not (_inside_delivery(ctx, dest) and _sheet_we_left(c, dest)):
            raise
        if ctx.dry_run:
            return None
        _make_room(dest, "o contact sheet novo")
        link_or_copy(sheet, dest)
    from .ledger import digest

    try:
        return digest(sheet)
    except OSError:
        # Sem o sha256 no registro, a próxima troca do sheet vira conflito: o lado seguro.
        return None


def _sheet_we_left(c, dest):
    """`dest` ainda tem os bytes do contact sheet que o último `deliver` deste item entregou."""
    from .ledger import digest

    recorded = (c.get("delivery") or {}).get("sheet_sha256")
    if not recorded:
        # Entrega de uma versão anterior, sem o registro: nada prova que é a prévia velha.
        return False
    try:
        return digest(dest) == recorded
    except OSError as exc:
        raise ValueError(
            f"Não consegui comparar {labels.shown(dest)} com o contact sheet entregue antes "
            f"({labels.reason(exc)}): confira "
            "permissão ou disponibilidade do arquivo e rode `deliver` de novo."
        ) from exc


def _clip_source(ctx, c):
    """Arquivo coletado deste item, na pasta de clipes do layout do projeto."""
    from . import layout

    return layout.output_file(ctx.ledger.root.parent, c["output"]["path"], sha256=c["output"].get("sha256"))


def _canonical_label(ctx, c):
    """Onde o original está, relativo ao projeto, só quando não é `brolls/<caminho lógico>`."""
    source = _clip_source(ctx, c)
    project = ctx.ledger.root.parent
    if source == ctx.ledger.root / c["output"]["path"]:
        return None
    try:
        return source.relative_to(project).as_posix()
    except ValueError:
        return None


def _deliver_write_origin(ctx, origin_rel, c, names, method):
    """Grava ORIGEM.md deste item, com a data de criação preservada se o arquivo já existia."""
    from .ledger import atomic_write

    target = ctx.root / origin_rel
    atomic_write(
        target,
        render_origin(c, names["media"], _created_in(target), method or "hardlink", canonical=_canonical_label(ctx, c)),
    )


def _deliver_process_item(ctx, group, index, c, acc):
    """Processa um item do grupo: liga/copia arquivos, grava ORIGEM.md e atualiza os acumuladores."""
    sheet_rel = (c.get("preview") or {}).get("contact_sheet_path")
    sheet = ctx.ledger.root / sheet_rel if sheet_rel else None
    names = _names(
        group["dir"],
        index,
        Path(c["output"]["path"]).suffix or ".mp4",
        Path(sheet_rel).suffix if sheet_rel else ".jpg",
    )
    media_rel = f"{group['dir']}/{names['media']}"
    acc["expected"].add(media_rel)
    sheet_rel_out = f"{group['dir']}/{names['sheet']}" if sheet else None
    if sheet_rel_out:
        acc["expected"].add(sheet_rel_out)
    # No ensaio, o método sai `"planned"` e o conflito é o mesmo da rodada real.
    method, conflict, note, sheet_sha = _deliver_materialize_item(ctx, group, c, names, sheet)
    if conflict:
        acc["conflicts"].append(conflict)
        acc["conflicted"].append((media_rel, note) if note else media_rel)
        # O que já estava entregue deste item fica, mesmo que o nome planejado mude.
        acc["expected"].update(_delivered_files(ctx.root, c))
    origin_rel = f"{group['dir']}/{names['origin']}"
    acc["expected"].add(origin_rel)
    if not ctx.dry_run and not conflict:
        _deliver_write_origin(ctx, origin_rel, c, names, method)
    if conflict:
        # Não entregamos este: manter o registro antigo (ou nenhum) é o que
        # mantém o item fora de "entregue" no `status` e fora da tabela.
        return
    if not ctx.dry_run:
        _record_delivery(c, media_rel, method, sheet_sha, acc["changed"])
    acc["listed"].append(
        {
            "id": c["id"],
            "beat": group["beat"],
            "path": f"{DELIVERY_DIR}/{media_rel}",
            "method": method,
        }
    )
    acc["rows"].append(
        {
            "beat": group["beat"] or "sem beat",
            "narration": group["narration"],
            "target": group.get("target_label", group["target"]),
            "file": f"{group['dir']}/{names['media']}",
            "state": c.get("state"),
            "rights": (c.get("rights") or {}).get("status"),
            # Como este arquivo chegou aqui: é o que decide se editar nele
            # editaria o original.
            "method": method,
        }
    )


def _record_delivery(c, media_rel, method, sheet_sha, changed):
    """Grava em `c["delivery"]` o que foi entregue; o item entra em `changed` só quando o registro mudou."""
    record = {"path": f"{DELIVERY_DIR}/{media_rel}", "method": method}
    if sheet_sha:
        # O que prova, na próxima rodada, que o sheet em `entrega/` ainda é o que entregamos.
        record["sheet_sha256"] = sheet_sha
    if c.get("delivery") != record:
        c["delivery"] = record
        changed.append(c)


def _delivered_files(root, c):
    """Caminhos (relativos a `entrega/`) do que já foi entregue deste item: mídia, ORIGEM e contact sheet."""
    recorded = (c.get("delivery") or {}).get("path") or ""
    if not recorded.startswith(DELIVERY_DIR + "/"):
        return set()
    media = recorded[len(DELIVERY_DIR) + 1 :]
    folder, _, name = media.rpartition("/")
    stem = Path(name).stem
    tail = stem[len(folder) :] if folder and stem.startswith(folder) else ""
    found = {media, f"{folder}/ORIGEM{tail}.md"}
    # lenient: link test after strict_is_dir on the same line
    if strict_is_dir(root / folder) and not (root / folder).is_symlink():
        found.update(f"{folder}/{p.name}" for p in (root / folder).glob(f"{SHEET}{tail}.*"))
    return found


def _unresolved_conflicts(ctx, skipped, acc):
    """Itens pulados como não verificados cujo clipe está em conflito de pasta: viram conflito, não pulo.

    O `verify` derruba `verified` quando as duas cópias divergem; sem isto o `deliver`
    seguinte pularia o item e a varredura apagaria o link já entregue, calado.
    """
    from .layout import ClipConflictError

    by_id = {c["id"]: c for c in ctx.ledger.data["items"]}
    kept = []
    for entry in skipped:
        c = by_id.get(entry["id"])
        if c is None or not (c.get("output") or {}).get("path"):
            kept.append(entry)
            continue
        try:
            with logs.handled():  # o conflito vai para `conflicts`; o `deliver` decide no fim
                _clip_source(ctx, c)
        except ClipConflictError as exc:
            acc["conflicts"].append(f"{c['id']}: {labels.reason(exc)}")
            acc["expected"].update(_delivered_files(ctx.root, c))
            logs.event(log, logging.WARNING, "deliver_conflict", beat=c.get("shot") or "no_beat", kind="clip_conflict")
            continue
        except ValueError:
            pass
        kept.append(entry)
    return kept


def _deliver_write_index(root, for_human, rows, conflicted, listed):
    """Grava o índice (README) da entrega, com o próximo passo já refletindo o estado novo."""
    from .ledger import atomic_write

    index = root / INDEX
    # O "próximo passo" é lido agora, com `c["delivery"]` já preenchido: senão o
    # índice mandaria a pessoa rodar exatamente o comando que acabou de rodar.
    text = for_human() if callable(for_human) else for_human
    atomic_write(
        index,
        render_index(
            rows,
            text,
            _created_in(index),
            conflicted,
            # Quando tudo virou cópia, o aviso de "é o mesmo arquivo" seria mentira.
            copies=bool(listed) and all(i["method"] == "copy" for i in listed),
        ),
    )


def _deliver_forget_retired(items, retired, changed):
    """Apaga o registro de entrega dos itens de beats aposentados, para reativar pedir `deliver` de novo."""
    gone = {entry["id"] for entry in retired}
    for c in items:
        if c["id"] in gone and "delivery" in c:
            del c["delivery"]
            changed.append(c)


def _deliver_process_groups(ctx, groups, acc):
    """Processa todos os grupos do plano, item a item, preenchendo os acumuladores."""
    for group in groups:
        acc["expected"].add(group["dir"])
        for index, c in enumerate(group["items"], start=1):
            _deliver_process_item(ctx, group, index, c, acc)


def _deliver_owned_paths(items):
    """Caminhos relativos a `entrega/` que o próprio manifesto já diz que foram entregues."""
    return {
        (c.get("delivery") or {}).get("path", "")[len(DELIVERY_DIR) + 1 :]
        for c in items
        if (c.get("delivery") or {}).get("path", "").startswith(DELIVERY_DIR + "/")
    }


def _clip_link_roots(ledger):
    """Pastas para onde um symlink de `entrega/` gerado por nós pode apontar: `brolls/` e as de clipe."""
    from . import layout

    try:
        with logs.handled():
            return (ledger.root, *layout.clip_roots(ledger.root.parent))
    except ValueError:
        # Pasta de clipes recusada (link): só `brolls/` conta, e nenhum link para fora é nosso.
        return (ledger.root,)


def _mirrored_sources(ledger, items):
    """Clipes de `brolls/` que a mídia de `entrega/` pode espelhar: todo arquivo das pastas de clipe.

    Não só o `output.path` de agora: `reject`, um corte novo, a troca de formato e o
    `fetch` de outra revisão tiram o caminho do manifesto, mas o clipe nunca sai de
    `brolls/` — e o link (ou a cópia) que sobrou dele em `entrega/` continua sendo nosso.
    Também o caminho do manifesto, para um `output.path` fora de `clips/`; com `..` ou
    barra invertida, fica de fora. Gerador: só é percorrido se a varredura achar mídia
    fora do plano.
    """
    for folder in _clip_link_roots(ledger)[1:]:
        try:
            # lenient(2): best-effort mirror listing inside try/except OSError (see below)
            if folder.is_dir() and not folder.is_symlink():
                # Uma fonte que não dá para conferir não entra como intocada: o espelho dela fica.
                # lenient(2): the except OSError below catches it as on 3.11; its mirror is kept
                yield from (p for p in folder.iterdir() if p.is_file() and not p.is_symlink())
        except OSError:
            continue
    for c in items:
        value = (c.get("output") or {}).get("path")
        if isinstance(value, str) and value and "\\" not in value and ".." not in Path(value).parts:
            yield ledger.root / value


def _vanished_clip(ctx, c):
    """Onde estava o clipe de `c`, relativo ao projeto, quando ele sumiu de `brolls/`; senão `None`."""
    if c is None or not (c.get("output") or {}).get("path"):
        return None
    try:
        with logs.handled():
            source = _clip_source(ctx, c)
    except ValueError:
        return None
    return None if strict_exists(source) else _clip_label(ctx, c, source)


def _edited_orphan(ctx, rel, c):
    """(mensagem do erro, motivo para o README) de mídia nossa, fora do plano, que não bate com nenhum clipe.

    Ela fica e o `deliver` falha. Sem um clipe que confira, apagar poderia perder a
    edição da pessoa — ou a única cópia de um clipe que sumiu de `brolls/` —, então
    nenhuma das duas mensagens sugere apagar.
    """
    vanished = _vanished_clip(ctx, c)
    logs.event(
        log, logging.WARNING, "deliver_conflict", beat="orphan", kind="clip_missing" if vanished else "foreign_file"
    )
    if vanished:
        return (
            f"{ctx.root / rel} saiu do plano da entrega e o clipe dele sumiu de brolls/ ({vanished}): "
            "este arquivo pode ser a única cópia, e eu não apago. Copie esse arquivo de volta para "
            f"{vanished} e rode `verify`, ou guarde-o fora de `entrega/`; depois rode `deliver` de novo."
        ), (f"o clipe `{vanished}` sumiu e este pode ser a única cópia dele; não apaguei")
    return (
        f"{ctx.root / rel} saiu do plano da entrega e não bate com nenhum arquivo de brolls/ "
        "(pode ser edição sua): eu não apago. Guarde esse arquivo fora de `entrega/` e rode "
        "`deliver` de novo."
    ), "saiu do plano e não bate com nenhum arquivo de `brolls/` (pode ser edição sua); não apaguei"


def _deliver_sweep(ctx, items, acc):
    """Varre `entrega/`; cada cópia editada fora do plano vira conflito. Devolve (removidos, mantidos)."""
    edited = []
    removed, kept = _sweep(
        ctx.root,
        acc["expected"],
        ctx.dry_run,
        _deliver_owned_paths(items),
        _clip_link_roots(ctx.ledger),
        sources=_mirrored_sources(ctx.ledger, items),
        edited=edited,
    )
    by_path = {(c.get("delivery") or {}).get("path"): c for c in items if c.get("delivery")}
    for rel in edited:
        conflict, note = _edited_orphan(ctx, rel, by_path.get(f"{DELIVERY_DIR}/{rel}"))
        acc["conflicts"].append(conflict)
        acc["conflicted"].append((rel, note))
    return removed, kept


def build_delivery(project, dry_run=False, ledger=None, for_human=None, rules=None):
    """Refaz `entrega/` a partir do que já está coletado em `brolls/`.

    Não toca em `brolls/`, não decide nada e não inventa direito de uso: só reorganiza
    o que `fetch` já produziu. Com `dry_run`, devolve o mesmo relatório sem escrever
    um byte — nem na pasta, nem no manifesto, e o método de cada item sai como
    `"planned"`, porque só a escrita diz qual o sistema aceita.

    `for_human` pode ser um texto ou uma função sem argumentos: ela é chamada **depois**
    da materialização, para que o "próximo passo" do índice fale do estado novo e não do
    passo que acabou de ser executado.

    Um arquivo que a pessoa editou não interrompe o trabalho pela metade: o conflito é
    anotado, os demais itens são materializados, a varredura roda, o índice e o manifesto
    são gravados, e só então o comando falha nomeando todos os arquivos em conflito.

    `rules` é o RULES.md já carregado (o `deliver` e o `verify` passam o deles); sem ele,
    é lido do projeto. Com ele, o BRIEF.md é validado como o `brief` valida: um BRIEF.md
    inválido recusa antes de tocar em `entrega/`.

    `entrega/` precisa ser uma pasta real: se o caminho já existe como link simbólico,
    seguir ele poderia varrer e apagar arquivos de outro lugar (o alvo do link), então o
    comando recusa antes de tocar em qualquer arquivo, mesmo em `dry_run`.
    """
    from .ledger import Ledger

    ledger = ledger or Ledger(project, recover=False)
    items = ledger.data["items"]
    root = Path(project).expanduser().resolve() / DELIVERY_DIR
    _deliver_refuse_symlink(root)
    retired = []
    groups, skipped = _plan(project, items, retired, rules)
    ctx = _DeliverCtx(ledger, root, dry_run, Pristine(_mirrored_sources(ledger, items)))
    acc = {"expected": set(), "listed": [], "rows": [], "changed": [], "conflicts": [], "conflicted": []}
    _deliver_process_groups(ctx, groups, acc)
    skipped = _unresolved_conflicts(ctx, skipped, acc)
    acc["expected"].add(INDEX)
    # A varredura vem antes do índice: a cópia editada que ela preserva entra em "Conflitos".
    removed, kept = _deliver_sweep(ctx, items, acc)
    if not dry_run:
        effects.mkdir(root, parents=True, exist_ok=True)
        _deliver_write_index(root, for_human, acc["rows"], acc["conflicted"], acc["listed"])
    if not dry_run and retired:
        # A pasta do beat aposentado acabou de ser varrida: o registro de entrega dela
        # sai também, senão o beat reativado depois nunca pediria `deliver` de novo.
        _deliver_forget_retired(items, retired, acc["changed"])
    if acc["changed"]:
        ledger.save_many("deliver", acc["changed"])
    if acc["conflicts"] and not dry_run:
        # Só esta falha leva a dica do conflito; uma recusa anterior (brief, link) leva a de sempre.
        record_delivery_conflicts()
        raise ValueError(" ".join(acc["conflicts"]))
    return {
        "delivery": str(root),
        "readme": str(root / INDEX),
        "dry_run": bool(dry_run),
        "items": acc["listed"],
        "rows": acc["rows"],
        "removed": removed,
        "kept": kept,
        "skipped": skipped,
        # Só aparece com beat aposentado no brief: sem ele, o relatório é o de sempre.
        **({"retired": retired} if retired else {}),
        # Só no dry-run: fora dele, um conflito faz o comando falhar nomeando cada um.
        **({"conflicts": acc["conflicts"]} if acc["conflicts"] else {}),
    }
