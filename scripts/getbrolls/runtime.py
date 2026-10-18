"""Operational diagnostics. Never log command arguments or tokens; tracebacks are stored redacted."""

# pylint: disable=import-error,missing-function-docstring,broad-exception-caught,use-sequence-for-iteration,missing-class-docstring,cyclic-import,too-many-lines
# Legado: ocorrências pré-existentes (corpo idêntico ao código anterior à 2.6.0). `import-error`
# é o `fcntl`/`msvcrt` condicional por plataforma em `_acquire_lock`/`_release_lock`.
# Os ciclos (getbrolls.runtime <-> getbrolls.logs, getbrolls.runtime <-> getbrolls.http)
# já existiam antes da 2.6.0: os imports de `logs`/`http` aqui são tardios (dentro de
# função) de propósito, exatamente para quebrar esses ciclos em tempo de execução
# (ver os comentários "avoids a runtime<->logs/http import cycle" abaixo).
# `too-many-lines`: as tabelas de só leitura e de trava ficam ao lado da trava que elas decidem.

import contextlib
import contextvars
import errno
import json
import logging
import os
import re
import shutil
import stat
import sys
import time
import traceback
from pathlib import Path

from . import _paths, _tokens, labels
from .errors import LockedError, PrerequisiteError, Refusal, UsageError
from .models import now

ACTIVE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("getbrolls_operation", default=None)


def _acquire_lock(stream, platform=None, windows=None):
    """Acquire a non-blocking, one-byte lock on Windows or a flock elsewhere."""
    platform = platform or os.name
    if platform == "nt":
        if windows is None:
            import msvcrt as windows

        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write("\0")
            stream.flush()
        stream.seek(0)
        try:
            windows.locking(stream.fileno(), windows.LK_NBLCK, 1)  # pyright: ignore[reportAttributeAccessIssue]
        except OSError as exc:
            raise BlockingIOError from exc
        return
    import fcntl

    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock(stream, platform=None, windows=None):
    platform = platform or os.name
    if platform == "nt":
        if windows is None:
            import msvcrt as windows

        stream.seek(0)
        windows.locking(stream.fileno(), windows.LK_UNLCK, 1)  # pyright: ignore[reportAttributeAccessIssue]
        return
    import fcntl

    fcntl.flock(stream, fcntl.LOCK_UN)


def _ensure_private_file(path):
    """Create `path` (empty) if missing, then force 0600 regardless of umask.

    `touch()`'s own `mode=` is still masked by umask, so an explicit `chmod` is the
    only way to guarantee 0600 both on first creation and on a file left over from
    before this fix (a plain `diagnostics.jsonl` created 0644 by an older run).
    """
    # effect-exempt: creates the append-only journal; diagnostics.jsonl is the audit log, the rest log their op
    path.touch(exist_ok=True)
    path.chmod(0o600)


def record_warning(code, message):
    current = ACTIVE.get()
    if current is not None:
        current["warnings"].append({"code": code, "message": message})
    try:
        from . import logs  # local: avoids a runtime<->logs import cycle

        # Only the code: warning messages are prose written for the person and may
        # quote what they typed (an approver's name, a reason). The full message is
        # already in the command's JSON output; the log only needs to correlate.
        logs.event(logs.get("runtime"), logging.WARNING, "warning", code=code)
    except Exception:  # noqa: BLE001, S110 - logging must never break a command
        pass


def record_commit():
    current = ACTIVE.get()
    if current is not None:
        current["state_committed"] = True


def record_delivery_conflicts():
    """O `deliver` já entregou o que pôde e vai falhar nomeando os arquivos em conflito.

    É o que escolhe a dica do conflito (`_error_hint`): um `deliver` que gravou o estado
    (a troca de formato, que vem antes) e recusou por outro motivo leva a de `review`.
    Marca interna, só do evento: não vai para o JSON de erro."""
    current = ACTIVE.get()
    if current is not None:
        current["delivery_conflicts"] = True


def record_recovered():
    """O comando concluiu, antes de rodar, a gravação interrompida de um comando anterior.

    Não é gravação deste comando: não liga `state_committed` (nem a dica de `review`) se ele
    falhar depois; liga `recovered` no evento e no JSON de erro."""
    current = ACTIVE.get()
    if current is not None:
        current["recovered"] = True


SENSITIVE_HEADERS = ("Authorization", "Cookie", "Set-Cookie", "X-Api-Key")
# `Name: value` and a quoted/JSON form (`"Name": "value"`, `{'Name': 'value'}`, `=` too); the value stops at a quote
# or newline, so the braces/quotes of a dict repr survive. No nested quantifiers — linear on adversarial input.
_HEADER_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(re.escape(h) for h in SENSITIVE_HEADERS) + r")\s*[\"']?\s*[:=]\s*[\"']?[^\r\n\"']+"
)
# A Bearer or Basic credential with no header name in front of it: the whole value, whatever its characters,
# up to a blank, a quote or the line end (base64 `+/=`, a JWT, `%`-encoded text).
_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s\"'<>]+")
# Nome de parâmetro/chave com cara de segredo, casado com o nome INTEIRO
# (`fullmatch`): a palavra secreta fecha o nome — `auth_token`, `client_secret`,
# `X-Amz-Signature` casam; `oauth2_token_id`, `monkey`, `profile_id` não. Os nomes
# soltos do fim cobrem assinaturas de CDN (Akamai `__token__`/`hdnts`/`hdnea`,
# CloudFront `Key-Pair-Id`) e os prefixos de assinatura S3/GCS.
SECRET_NAME_RE = re.compile(
    r"(?i)(?:[a-z0-9_.-]{0,40}[_.-])?"
    r"(?:api_?key|apikey|access_?token|key|token|secret|signature|sig|password|passwd|pwd|senha|hmac|jwt"
    r"|credentials?|policy|authorization)"
    r"|__token__|hdnts|hdnea|key-pair-id|x-amz-[a-z0-9-]*|x-goog-[a-z0-9-]*"
)


def secret_name(name):
    """`True` quando `name` (chave de JSON ou de query string) parece guardar um segredo."""
    return isinstance(name, str) and SECRET_NAME_RE.fullmatch(name) is not None


# Quebra de linha ou caractere de controle: o que deixa um valor "fugir" da linha
# dele num Markdown gerado (ORIGEM.md, credits.md) e forjar outra linha.
_LINE_BREAKING = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


def one_line(value):
    """`value` como texto de uma linha só: controle/quebra de linha viram espaço.

    Texto normal (sem controle) volta idêntico — a saída dos built-ins não muda."""
    return _LINE_BREAKING.sub(" ", str(value))


def _writable(target):
    """Liga a escrita do dono em `target` (e leitura/entrada, se for pasta); link não é tocado."""
    with contextlib.suppress(OSError):
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode):
            return
        extra = stat.S_IWUSR | (stat.S_IRUSR | stat.S_IXUSR if stat.S_ISDIR(mode) else 0)
        target.chmod(stat.S_IMODE(mode) | extra)


# effect-exempt(3): only compared against the func that rmtree retries; called inside force_rmtree's logged op
_REMOVE_FUNCS = (os.unlink, os.remove, os.rmdir)


def _removable(target):
    """Libera leitura+execução+escrita do dono numa pasta (0o700) ou escrita numa
    entrada comum (0o600); link não é tocado. Só precisa ser permissivo o
    suficiente para apagar — chamada apenas na retentativa do force_rmtree."""
    with contextlib.suppress(OSError):
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode):
            return
        target.chmod(0o700 if stat.S_ISDIR(mode) else 0o600)


def force_rmtree(path, *, target=None):
    """Apaga `path` inteiro mesmo com entrada somente-leitura; pasta ausente não é erro.

    O Git para Windows grava objetos (`.git/objects/**`) como somente-leitura, e o
    Windows não apaga arquivo somente-leitura — `rmtree(..., ignore_errors=True)`
    deixava um `.git` dentro do plugin instalado e `.install-*` acumulando. Na
    falha, libera a escrita da entrada (e da pasta de cima, se ela ainda é parte da
    árvore — nunca a pasta que CONTÉM `path`) e tenta de novo.

    Numa pasta sem leitura/execução (uma pasta comum plantada pelo dono do plugin,
    não um objeto git), o walk por fd do `rmtree` chama a retentativa com
    `func=os.open`/`os.scandir`/`os.lstat` (não `os.unlink`/`os.rmdir`/`os.remove`)
    — chamar `func(failed)` sem os argumentos certos levantaria `TypeError`. Nesse
    caso, depois de liberar a permissão, a subárvore é apagada por conta própria
    (`shutil.rmtree` recursivo) em vez de repetir a chamada original.

    Nunca levanta (guarda `Exception`, não só `OSError`): o que sobrar fica para a
    varredura seguinte (`install._sweep_stale_staging`)."""
    from . import effects  # local: avoids a runtime<->effects import cycle

    root = Path(path)
    # lenient: force_rmtree never raises; the caller reports what is left
    if not os.path.lexists(root):
        return
    # `target`: what the `fs.rmtree` line names (a temporary folder is named by its parent).
    with effects.op("fs.rmtree", root if target is None else target):

        def retry(func, failed, _exc):
            failed_path = Path(failed)
            if failed_path != root:
                _writable(failed_path.parent)
            _removable(failed_path)
            if func in _REMOVE_FUNCS:
                with contextlib.suppress(OSError):
                    func(failed)
            else:
                # effect-exempt: onexc callback, run only by the shutil.rmtree inside this fs.rmtree op line
                shutil.rmtree(failed_path, ignore_errors=True)

        with contextlib.suppress(Exception):
            if sys.version_info >= (3, 12):
                # O pylint infere a assinatura de `shutil.rmtree` pela stdlib do
                # interpretador que roda o lint, não pelo `sys.version_info` deste
                # ramo: `onexc` existe a partir do 3.12, mas o CI faz lint em
                # 3.11, que não conhece o kwarg. `**kwargs` monta fora da chamada
                # engana o pylint mas confunde o pyright (perde a distinção entre
                # os overloads de `onexc`/`onerror`), então a chamada fica direta
                # com a supressão na linha.
                shutil.rmtree(root, onexc=retry)  # pylint: disable=unexpected-keyword-arg  # pragma: no cover - 3.12+
            else:
                shutil.rmtree(root, onerror=retry)  # pylint: disable=deprecated-argument


def scrub_home(text):
    """Replace the user's home folder with `~`, only as a whole path (never inside `/Users/anax`). Never raises.

    Only for text nobody acts on: the diagnostics file and the `repr`/`traceback`
    fields, which people paste into bug reports. It is deliberately NOT part of
    `redact()`: error messages name paths the person (or the agent driving the CLI)
    must open or pass back as an argument, and `~` inside quotes is not expanded by a
    shell nor understood by a file reader. Both the raw and the JSON-escaped form of
    the prefix are replaced, so it also works on an already serialized line on Windows —
    escaped both ways: `ensure_ascii=True` (`Jo\\u00e3o`) and `ensure_ascii=False` (the way
    the diagnostics line is written: only the backslashes escaped).
    """
    value = str(text)
    try:
        home = str(Path.home())
    except RuntimeError:
        # No HOME/USERPROFILE to resolve: nothing to scrub.
        return value
    if home in ("", "/", "\\"):
        return value
    # As written, resolved (a linked HOME) and without `/private`, in every encoding.
    homes = _paths.with_linked({home, os.path.realpath(home)})
    windows = re.match(r"[A-Za-z]:[\\/]", home) is not None  # a Windows home ignores case on any system
    return _paths.replace_spellings(value, homes, "~", ignore_case=windows)


_URL_PATTERN = re.compile(r'https?://[^\s"<>]*')
_HOST_CHAR = re.compile(r"[A-Za-z0-9]")


def _omit_url(match):
    """A URL de verdade vira "[URL omitida]"; a forma citada num texto nosso fica.

    "https://…", "https://," ou "https://<host>" não têm host nenhum (sem letra nem
    dígito antes da primeira `/`): é exemplo de ajuda, não dado de quem chamou."""
    url = match.group(0)
    host = url.split("://", 1)[1].split("/", 1)[0]
    return url if _HOST_CHAR.search(host) is None else "[URL omitida]"


REDACT_LIMIT = 1200
# Quanto do fim de um traceback fica no diagnostics.jsonl: o frame que levantou e a linha
# do erro estão no fim, então o corte guarda a cauda, nunca o começo.
TRACEBACK_TAIL_CHARS = 8000


def redact(text, limit: int | None = REDACT_LIMIT):
    """Strip provider keys, secret-shaped headers/query values and URLs.

    Idempotent (running it twice yields the same string) and never raises — every
    step is a plain string replace/regex substitution over `str(text)`. Safe for
    user-facing messages: paths are left intact (see `scrub_home` for why). The result
    keeps the first `limit` characters; `limit=None` keeps everything.
    """
    value = _tokens.hide(text)  # keys by shape first: a URL around a key block must not hide its first line
    for key in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "YOUTUBE_API_KEY"):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = _HEADER_PATTERN.sub(lambda m: f"{m.group(1)}: [REDACTED]", value)
    value = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)} [REDACTED]", value)
    value = _tokens.hide_labelled(value)
    value = _URL_PATTERN.sub(_omit_url, value)  # last: every credential is out before the URLs go
    return value if limit is None else value[:limit]


# Par `'chave': 'valor'` (repr de dict) ou `"chave": "valor"` (JSON), com o valor entre aspas
# simples ou duplas (e `b'…'` de bytes). Cada lado da alternância do valor exclui o que o
# outro começa (barra invertida): sem retrocesso aninhado, linear em entrada hostil.
_QUOTED_PAIR_PATTERN = re.compile(
    r"""(?P<key>(?P<kq>["'])(?P<name>[A-Za-z0-9_.-]{1,80})(?P=kq))(?P<sep>\s*:\s*)b?"""
    r"""(?P<value>'(?:[^'\\\r\n]|\\.)*'|"(?:[^"\\\r\n]|\\.)*")"""
)


def _hide_quoted_secret(match):
    if not secret_name(match["name"]):
        return match.group(0)
    quote = match["value"][0]
    return f"{match['key']}{match['sep']}{quote}[REDACTED]{quote}"


def redact_diagnostic(text, limit: int | None = REDACT_LIMIT):
    """`redact` mais um passe para `repr` e `traceback` do diagnostics.jsonl.

    Um segredo dentro de um dict (`{'access_token': '…'}`, ou o mesmo em JSON) não tem `=`
    nem nome de cabeçalho na frente: aqui, o valor de toda chave entre aspas com cara de
    segredo (`secret_name`) vira `[REDACTED]`. Só para texto que ninguém lê como mensagem:
    a `message` da pessoa continua passando só por `redact`. Idempotente e nunca levanta."""
    value = _QUOTED_PAIR_PATTERN.sub(_hide_quoted_secret, redact(text, limit=None))
    return value if limit is None else value[:limit]


def _redacted_event(value):
    """`value` (the JSON of an event: dict, list, text) with every text through `redact_diagnostic`, whole and
    only then labelled: a `password=` in a warning or a message the person reads is as secret in the journal as
    one in a `repr`. The text is bounded first (`labels.bounded`): nothing hostile is searched at full size."""
    if isinstance(value, dict):
        return {key: _redacted_event(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_event(item) for item in value]
    if isinstance(value, str):
        bounded = labels.bounded(value)
        return bounded if bounded != value else redact_diagnostic(value, limit=None)
    return value


def diagnostic_line(event):
    """The `diagnostics.jsonl` line of `event`: every text redacted, known folders by label, then home as `~`
    (escaped forms too)."""
    return scrub_home(json.dumps(labels.labelled(_redacted_event(event)), ensure_ascii=False))


def redacted_traceback():
    """Traceback da exceção em tratamento, redigido e sem a pasta pessoal, para o diagnostics.jsonl.

    Longo demais, fica a cauda (`…` + os últimos `TRACEBACK_TAIL_CHARS` caracteres): é lá que
    estão o frame que levantou e a linha do erro."""
    text = labels.for_log(scrub_home(redact_diagnostic(traceback.format_exc(), limit=None)))  # whole, then cut
    return text if len(text) <= TRACEBACK_TAIL_CHARS else "…" + text[-TRACEBACK_TAIL_CHARS:]


STDERR_TAIL_MAX_CHARS = 600


def stderr_tail(stderr, limit=6):
    """Last `limit` non-empty stderr/detail lines, redacted, with folders by label, joined and capped.

    Shared helper for CLI stderr/detail text (subprocess stderr, HTTP error bodies), so callers cannot drift
    apart on secrets, URLs, machine paths or how much is surfaced; every cut comes after the redaction.
    """
    # Redacted whole, before the split: a private key spans lines, and only its first line says what it is.
    lines = [line.strip() for line in redact(stderr or "", limit=None).splitlines() if line.strip()]
    joined = " | ".join(labels.for_log(line, REDACT_LIMIT) for line in lines[-limit:])
    return joined[:STDERR_TAIL_MAX_CHARS]


_LOCK_POLL_S = 0.05


_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)


def _open_lock_file(path):
    refusal = ValueError(f"{path.name} em {labels.shown(path.parent)} é um link; apague o link e repita o comando.")
    # lenient: link refusal where O_NOFOLLOW is missing; the open below raises on a locked folder
    if not getattr(os, "O_NOFOLLOW", 0) and path.is_symlink():
        raise refusal
    try:
        # effect-exempt: lock file of exclusive_lock/try_lock; the log takes try_lock per line, logging here recurses
        fd = os.open(path, _LOCK_FLAGS, 0o644)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise refusal from None
        raise
    # effect-exempt: lock file of exclusive_lock/try_lock; the log takes try_lock per line, logging here recurses
    return os.fdopen(fd, "a+", encoding="utf-8")


def lock_busy(name, started, error):
    """Registra `lock_busy` (WARNING, com o `error_code` do erro) e devolve `error` para levantar.

    Diz qual trava estava ocupada e quanto se esperou por ela: só o nome do arquivo de trava
    (`.plugins.lock`, `.command.lock`…), nunca o caminho. `started` é o `time.monotonic()` de
    quando a espera começou. O log nunca derruba o comando.
    """
    try:
        from . import logs  # local: avoids a runtime<->logs import cycle

        waited_ms = round((time.monotonic() - started) * 1000)
        logs.refusal(logs.get("runtime"), "lock_busy", error, lock=name, waited_ms=waited_ms)
    except Exception:  # noqa: BLE001, S110 - logging must never break a command
        pass
    return error


@contextlib.contextmanager
def exclusive_lock(path, busy_message, wait_s=0.0):
    """Trava exclusiva no arquivo `path` (criado se faltar); a pasta tem que existir.

    O arquivo é aberto sem seguir link (`O_NOFOLLOW`; onde não existe, um `lstat` antes):
    uma trava trocada por link nunca cria nem trava um arquivo fora do lugar.
    Tenta de novo por até `wait_s` segundos; depois, `LockedError(busy_message)` (código `LOCKED`).
    """
    with _open_lock_file(Path(path)) as lock:
        started = time.monotonic()
        deadline = started + wait_s
        while True:
            try:
                _acquire_lock(lock)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise lock_busy(Path(path).name, started, LockedError(busy_message)) from None
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            _release_lock(lock)


_QUIET_LOCK_POLL_S = 0.005


@contextlib.contextmanager
def try_lock(path, wait_s):
    """Trava exclusiva em `path`, como `exclusive_lock`, mas quieta: rende `False` em vez de levantar.

    `False` quando a trava segue ocupada depois de `wait_s` segundos ou não abre (link no
    lugar, pasta sem permissão); `True` com ela tomada. Nunca loga: o próprio log do app a
    toma em volta de cada linha do log da instalação, e logar aqui seria recursão.
    """
    try:
        stream = _open_lock_file(Path(path))
    except (OSError, ValueError):
        stream = None
    if stream is None:
        yield False
        return
    with stream:
        held = _acquire_within(stream, wait_s)
        try:
            yield held
        finally:
            if held:
                with contextlib.suppress(OSError):
                    _release_lock(stream)


def _acquire_within(stream, wait_s):
    deadline = time.monotonic() + wait_s
    while True:
        try:
            _acquire_lock(stream)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_QUIET_LOCK_POLL_S)
        except OSError:
            return False


@contextlib.contextmanager
def usage_errors():
    """Todo `ValueError` levantado dentro sai como `UsageError` (exit 2): formato de flag errado
    é erro de uso, não de conteúdo. A exceção é o `Refusal`: uma recusa de dados com `reason_code`
    segue como é (`INVALID_DATA`, exit 1), para o motivo chegar a quem automatiza.

    A recusa registrada dentro (`logs.refusal`) já sai com `USAGE_ERROR`, o código que a CLI
    vai dar ao erro convertido (`INVALID_DATA` para o `Refusal`): a linha do log e o JSON de erro
    nunca discordam.
    """
    from . import logs  # local: avoids a runtime<->logs import cycle

    with logs.refused_as("USAGE_ERROR"):
        try:
            yield
        except (UsageError, Refusal):
            raise
        except ValueError as exc:
            raise UsageError(str(exc)) from None


COMMAND_LOCK = ".command.lock"


@contextlib.contextmanager
def project_lock(project):
    if not project:
        yield
        return
    from . import effects  # local: avoids a runtime<->effects import cycle

    root = Path(project).resolve() / "brolls"
    effects.mkdir(root, parents=True, exist_ok=True)
    with exclusive_lock(
        root / COMMAND_LOCK, "Outro comando está usando este projeto. Aguarde terminar antes de repetir."
    ):
        yield


# Comandos que só leem o projeto: sem trava exclusiva e sem criar a árvore.
# `brief` entra aqui porque só lê BRIEF.md, RULES.md e o manifesto já existente.
# `doctor` aceita `--project` por uniformidade com o resto da CLI, mas diagnostica a
# instalação: não pode criar `brolls/` numa pasta que talvez nem seja um projeto.
# `capabilities` só descreve o parser e o manifesto dos plugins, sem projeto.
# `setup` nunca recebe nem toca projeto; instala só em `$GB_HOME/runtime` ou `GB_RUNTIME_DIR`.
# `x` roda comando de plugin, que só lê o projeto por cópias (CommandContext).
# `profile` só grava `$GB_HOME/trusted-profiles.json` (trust/untrust), nunca um projeto.
# `rules` só mostra as regras em vigor; `library --search` só consulta a biblioteca pessoal
# (`$GB_HOME/library`); `references` só lê as referências já decididas do projeto. `providers` só lista
# as fontes e o que está configurado, sem projeto.
READ_ONLY_COMMANDS = (
    "status",
    "serve",
    "brief",
    "doctor",
    "setup",
    "x",
    "capabilities",
    "profile",
    "rules",
    "library",
    "references",
    "providers",
    "environment",
)
# (comando, ação) somente leitura, além dos comandos inteiros acima: `queue --action status` só consulta
# queue.json (mesmo contrato de `status`), nunca deve tomar a trava exclusiva. `roteiro --action check|plan`
# e `assets` também só leem: plano de cena, sync simulado e inventário de componentes, sem trava nem árvore
# nova. `client --action list|show` só lê `$GB_HOME/clients.json` e o `client.json` de cada pasta;
# `recipe --action list|show` só lê as pastas de receita dos clientes registrados, e `template --action
# freeze|list|show` só recusa com o comando novo. `migrate --action plan` só mostra o `project.json` que o
# `apply` gravaria. `analysis --action list|check` só lê `analysis/`, sem criar a pasta. `plugins --action
# list` só lê o inventário. O `plugins --action check` fica de fora: ele executa o `register()` do plugin
# (código de terceiros, sem sandbox), e o core não garante que esse código não grave.
READ_ONLY_ACTIONS = {
    ("analysis", "check"),
    ("analysis", "list"),
    ("client", "list"),
    ("client", "show"),
    ("migrate", "plan"),
    ("recipe", "list"),
    ("recipe", "show"),
    ("template", "freeze"),
    ("template", "list"),
    ("template", "show"),
    ("queue", "status"),
    ("roteiro", "check"),
    ("roteiro", "plan"),
    ("assets", "list"),
    ("assets", "where"),
    ("plugins", "list"),
    ("plugins", "marketplace-list"),
    ("plugins", "search"),
}

# (comando, ação) que grava só sob a própria trava, nunca sob a do projeto: `analysis
# --action register` calcula o sha256 de uma mídia grande sem segurar o projeto, e só a
# troca dos arquivos de `analysis/` fica dentro de `analysis/.lock`.
OWN_LOCK_ACTIONS = {("analysis", "register")}

# Comandos que criam o projeto (ou o `project.json`) e só tomam a trava dele depois de
# validar tudo: um `init` recusado (cliente desconhecido, template adulterado, flag errada)
# ou um `migrate` numa pasta que não existe não pode deixar para trás a pasta do projeto,
# `brolls/.command.lock` nem um log. O log de auditoria só é
# gravado quando `brolls/` já existe (ou seja, depois que o próprio comando a criou).
SELF_LOCKED_COMMANDS = ("init", "migrate")

# Comandos de etapa: trabalham sobre candidatos que `search`/`resolve` já registraram. Um typo
# no `--project` de um deles (uma pasta que não existe) é recusado antes da trava e do log:
# nunca cria um projeto vazio. Numa pasta que já existe, mesmo sem `brolls/`, eles rodam como
# sempre (`review` monta o Storyboard vazio). `search`, `resolve`, `init*`, `roteiro` e `queue`
# continuam criando o projeto, inclusive a pasta.
_DECISION_STAGES = ("preview", "approve", "permit", "reject", "review", "import-review", "remember")
_FILE_STAGES = ("fetch", "verify", "deliver")
STAGE_COMMANDS = _DECISION_STAGES + _FILE_STAGES


def is_folder(path):
    """`path` é uma pasta? Nunca levanta.

    No Python 3.11 e 3.12, `Path.is_dir()` relança o EACCES de uma pasta sem permissão; aqui a
    pasta que não dá para ver conta como ausente, e quem for usá-la de fato recebe o `OSError`
    dentro da auditoria (`IO_ERROR`), nunca um traceback no caminho do erro."""
    try:
        # lenient: documented never-raise view: the audit that uses the folder raises IO_ERROR
        return Path(path).is_dir()
    except OSError:
        return False


def _visible(path):
    """`path` existe? Nunca levanta: o que não dá para ver (EACCES) conta como ausente."""
    try:
        # lenient: documented never-raise view: what it cannot see counts as absent
        return Path(path).exists()
    except OSError:
        return False


def _absent(path):
    """`path` comprovadamente não existe? Nunca levanta.

    Só ENOENT e ENOTDIR contam: a pasta que não dá para ver (EACCES) não é um typo, e quem
    for usá-la recebe o `OSError` dentro da auditoria (`IO_ERROR`)."""
    try:
        Path(path).stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    # lenient: documented never-raise: what it cannot see is not proven absent (False)
    except OSError:
        return False
    return False


# Comandos que lidam com um `project.json` ilegível do seu jeito (`status` o mostra como problema do layout; `init`
# e `migrate` o recusam por nome); nos outros ele é um erro de E/S, nunca um projeto sem componentes.
LAYOUT_TOLERANT = frozenset({"status", "init", "migrate"})


def _surface_layout_shape(command, project, typed):
    """Dá ao comando em curso o aviso `OLD_SHAPE` de um `project.json` de forma antiga, qualquer que seja o comando.

    Ler o layout aqui, uma vez por comando, é o que impede um leitor que só olha o `project.json` de passagem
    (`assets`, `roteiro`, `verify`…) de degradar o projeto para o layout 0 calado. Um `--project` que existe e
    não é pasta, ou um `project.json` que o sistema não abre, recusa o comando (`status`, `init` e `migrate` têm
    a recusa deles).
    """
    if not project:
        return
    from . import layout  # local: layout importa renamed, que importa este módulo

    folder = Path(project).expanduser()
    # strict_*: a pasta que não dá para ver é erro de E/S do comando, nunca "não é pasta"
    if command not in LAYOUT_TOLERANT and _paths.strict_exists(folder) and not _paths.strict_is_dir(folder):
        raise ValueError(f"{typed or project} não é uma pasta; aponte --project para a pasta do projeto.")
    layout.surface(project, strict=command not in LAYOUT_TOLERANT)


def missing_project_tree(command, project):
    """O comando é de etapa e a pasta `project` não existe (um typo no `--project`)?"""
    return command in STAGE_COMMANDS and bool(project) and _absent(project)


def _refuse_missing_project(project):
    # A pasta como a pessoa a digitou: resolvida, um link no caminho mudaria a grafia.
    raise ValueError(
        f"Projeto não encontrado em {project} (a pasta não existe); nenhum arquivo foi criado. "
        "Confira o --project, ou comece o projeto com search ou resolve."
    )


# (comando, ação) sem projeto que muda o estado da instalação (`$GB_HOME`): só eles gravam o log do app da
# instalação (`$GB_HOME/logs/getbrolls.log`), do mesmo jeito que só o comando que grava num projeto grava o
# `brolls/getbrolls.log`. `plugins` usa as ações que já tomam a trava dos plugins e `setup`/`profile` entram
# por flag e ação (ver `writes_installation`); `list`, `show`, `check`, `search`, `doctor` e o resto só leem.
INSTALLATION_WRITES = frozenset({("client", "add"), ("client", "remove")})


def writes_installation(args):
    """O comando muda a instalação (plugins, marketplaces, clientes, perfil confiável, `setup`)?

    Só ele abre o log do app da instalação; um comando que só lê não cria o arquivo. A
    prévia do `profile --action trust` (sem `--yes`) só lê o getbrolls.toml, e a forma
    posicional antiga (`legacy`) só recusa: o `trust --yes` e o `untrust` é que gravam `trusted-profiles.json`.
    """
    command = args.command
    if command == "plugins":
        from .sdk.cli import WRITING_ACTIONS  # tardio: o sdk importa este módulo

        return getattr(args, "action", None) in WRITING_ACTIONS
    if command == "setup":
        return not (getattr(args, "check", False) or getattr(args, "where", None))
    if command == "profile":
        action = getattr(args, "action", None)
        if getattr(args, "legacy", None):
            return False  # a forma antiga só recusa (`renamed`): nada grava, nem o log
        return action == "untrust" or (action == "trust" and bool(getattr(args, "yes", False)))
    return (command, getattr(args, "action", None)) in INSTALLATION_WRITES


# Erro que veio de um plugin: "Plugin <id>: …" (todo erro do core sobre código de
# plugin usa esse prefixo) ou "Fonte X é do plugin Y, …" (fonte de plugin fora do
# ar, que já traz a própria dica). RULES.md não tem nada a ver com nenhum dos dois.
_PLUGIN_ERROR_RE = re.compile(r"Plugin [A-Za-z0-9_-]+: |Fonte \S+ é do plugin ")
PLUGIN_ERROR_HINT = "Veja plugins --action list / doctor e docs/SDK.md."


# Comandos cujo erro sai sem a dica de recovery (nenhum erro mostra traceback nem
# repr à pessoa; esses ficam só em diagnostics.jsonl).
QUIET_ERROR_COMMANDS = ("plugins", "x", "export", "assets", "client", "analysis", "template", "recipe")

# `error_code` → código de saída da CLI; qualquer outro código (INVALID_DATA,
# IO_ERROR, ...) é erro de operação ou de dados: 1. A tabela completa, com o 0, fica
# em `cli.EXIT_CODES`; este é o único lugar que decide a saída de um erro. `LOCKED`
# (outro processo segura a trava do projeto ou do runtime) sai 1, como erro de operação,
# mas com código próprio para quem automatiza saber que basta repetir depois.
ERROR_EXIT = {"USAGE_ERROR": 2, "INTERNAL_ERROR": 3, "PREREQUISITE_MISSING": 4, "LOCKED": 1, "INTERRUPTED": 130}
EXIT_FOR_OTHER_ERRORS = 1


def exit_code_for(error_code):
    """Código de saída de um `error_code` do JSON de erro."""
    return ERROR_EXIT.get(error_code, EXIT_FOR_OTHER_ERRORS)


def error_code_for(exc):
    """`error_code` de uma exceção que a pessoa pode corrigir (não um bug).

    `ValueError` genérico depois do parse continua erro de dados (1), mesmo quando é,
    na prática, uso errado: só `UsageError` explícito vira `USAGE_ERROR`.
    """
    if isinstance(exc, UsageError):
        return "USAGE_ERROR"
    if isinstance(exc, PrerequisiteError):
        return "PREREQUISITE_MISSING"
    if isinstance(exc, LockedError):
        return "LOCKED"
    return "IO_ERROR" if isinstance(exc, OSError) else "INVALID_DATA"


_RECOVERY_HINT = "Há uma gravação pendente (recovery_pending=true): o próximo comando a retoma antes de rodar."
_REVIEW_HINT = "O estado já foi gravado (state_committed=true): execute review para regenerar a página."
# `deliver` que grava os itens bons e recusa os que estão em conflito (`delivery_conflicts`):
# regenerar o Storyboard não resolve nada; o que falta é resolver os arquivos que a mensagem nomeia.
_DELIVER_HINT = (
    "Os itens sem conflito já foram entregues (state_committed=true); resolva os arquivos em "
    "conflito que a mensagem nomeia e rode deliver de novo."
)
# Só sem `state_committed`: diz que o estado do projeto (o manifesto) não foi gravado, não que
# nada foi para o disco (prévias e GIFs são gravados antes do journal).
_RECOVERED_HINT = (
    "Uma gravação interrompida de um comando anterior foi concluída antes deste (recovered=true); "
    "este falhou sem gravar o estado do projeto (state_committed=false). Corrija e repita."
)


def _error_hint(command, event):
    """Dica do erro, só quando há o que fazer além de corrigir e repetir; senão `None`.

    `roteiro` nunca recebe a de `review`: ela fala do Storyboard do b-roll e se confunde
    com `roteiro --action review`. O `deliver` que falhou nomeando arquivos em conflito
    também não: o que ele já gravou são os itens sem conflito, e a dica dele fala do
    conflito. Um `deliver` recusado antes (BRIEF.md inválido) depois de gravar a troca de
    formato leva a de `review`, como os outros. O comando que gravou o próprio estado
    nunca recebe a de `recovered`, nem quando também concluiu a gravação de outro.
    """
    if event.get("recovery_pending"):
        return _RECOVERY_HINT
    if event.get("state_committed"):
        if command == "deliver" and event.get("delivery_conflicts"):
            return _DELIVER_HINT
        return None if command == "roteiro" else _REVIEW_HINT
    if event.get("recovered"):
        return _RECOVERED_HINT
    return None


def provider_error_message(text):
    """Mensagem de `ProviderError` para a pessoa: built-in segue com o " Confira
    docs/RULES.md." de sempre; erro de plugin ganha dica de plugin (ou nenhuma,
    quando já traz a dele), com a frase fechada antes."""
    if not _PLUGIN_ERROR_RE.match(text):
        return text + " Confira docs/RULES.md."
    if text.startswith("Fonte "):
        return text
    closed = text if text.rstrip().endswith((".", "!", "?")) else text.rstrip() + "."
    return f"{closed} {PLUGIN_ERROR_HINT}"


def _classify_audited_error(event, exc, log, project=None):
    """Preenche o evento com a classificação da exceção capturada por `audited`.

    `log` é o diagnostics.jsonl que vai guardar o evento (o que a mensagem do erro interno
    cita); `project`, o projeto cuja gravação pendente conta para `recovery_pending`."""
    event["status"] = "error"
    project_log = diagnostics_path(project) if project else None
    pending = project_log.parent / ".pending-transaction.json" if project_log else None
    # Num `brolls/` sem permissão, o EACCES não pode escapar do caminho do erro.
    event["recovery_pending"] = bool(pending and _visible(pending))
    # Diagnostics survive regardless of classification, redacted like everything else here.
    event["type"] = type(exc).__name__
    event["repr"] = labels.for_log(scrub_home(redact_diagnostic(repr(exc), limit=None)), REDACT_LIMIT)
    event["traceback"] = redacted_traceback()
    if not isinstance(exc, (ValueError, OSError, OverflowError)):
        # Anything but a data/IO error (KeyError, IndexError, TypeError, AttributeError,
        # RuntimeError…) is a bug signature, not a user-fixable input problem; the
        # traceback is what a maintainer needs, not a RULES.md pointer.
        event["error_code"] = "INTERNAL_ERROR"
        event["message"] = (
            f"Erro interno inesperado (bug) [type: {type(exc).__name__}]. Reporte incluindo diagnostics.jsonl"
            + (f" ({labels.shown(log)})." if log else ".")
        )
        return
    from .http import ProviderError  # local: avoids a runtime<->http import cycle

    # `MissingToolError` (yt-dlp/ffmpeg ausente) é ao mesmo tempo `ProviderError` e
    # `PrerequisiteError`: sai 4, com a mensagem e o aviso de fonte de sempre.
    event["error_code"] = error_code_for(exc)
    if isinstance(exc, Refusal):
        event["reason_code"] = exc.reason_code
    message = labels.message_of(exc, REDACT_LIMIT, project=project)  # redact, label, the typed file, then cut
    if isinstance(exc, OSError) and exc.errno is not None and not isinstance(exc, ProviderError):
        # O erro do próprio sistema (EACCES, ENAMETOOLONG…): o texto cru traz `[Errno N]` e o 3.11 e o 3.14 o
        # levantavam de lugares diferentes. Sai sempre a mesma mensagem em português, com o arquivo pela marca.
        message = redact(labels.os_message(exc, project=project), limit=REDACT_LIMIT)
    if isinstance(exc, ProviderError):
        event["message"] = provider_error_message(message)
        current = ACTIVE.get()
        # `audited` (the only caller) sets ACTIVE before the command and resets it only after this.
        assert current is not None  # noqa: S101 - type narrowing only; see the line above
        current["warnings"].append({"code": "PROVIDER_ERROR", "message": message})
        event["warnings"] = current["warnings"]
    else:
        event["message"] = message


def _audited_error_failure(args, event, log, app_log):
    """Monta o `OperationError` da exceção já classificada por `_classify_audited_error`."""
    # Traceback e repr nunca vão para a pessoa, em nenhum comando: ficam só no
    # evento que `audited` grava em diagnostics.jsonl.
    payload = {k: v for k, v in event.items() if k not in ("traceback", "repr", "delivery_conflicts")}
    hint = _error_hint(args.command, event)
    if hint:
        payload["hint"] = hint
    payload["log"] = str(log) if log else None
    payload["app_log"] = app_log
    if args.command in QUIET_ERROR_COMMANDS and event["error_code"] != "INTERNAL_ERROR":
        # `plugins`/`x`/`assets`/`client` não gravam no projeto (ou, no `assets`, só leem):
        # erro de uso ali (flag faltando, plugin inexistente) é só a mensagem — a
        # dica de recovery/review era ruído. `export` grava só numa pasta nova em
        # exports/ e nunca no manifesto nem no journal (recusa journal pendente
        # antes de começar): a dica de recovery também não vale lá. `analysis` só grava
        # em analysis/, sob a trava própria, nunca no manifesto.
        payload.pop("hint", None)
    return OperationError(payload)


def _audited_interrupt_failure(event, log, app_log):
    """Monta o `OperationError` de uma interrupção (Ctrl-C) durante `audited`."""
    event["status"] = "interrupted"
    event["error_code"] = "INTERRUPTED"
    return OperationError(
        {
            **event,
            "message": "Operação interrompida. O próximo comando recuperará uma gravação pendente, se houver.",
            "log": str(log) if log else None,
            "app_log": app_log,
        }
    )


def _app_log(project):
    """`app_log` do erro: o `getbrolls.log` que `logs.configure` escolheu para o comando, se existir.

    A mesma escolha do log: o do projeto, ou o da instalação para quem muda a instalação
    (com `--project` ou não). Nunca levanta: arquivo que não dá para conferir é `None`.
    """
    from . import logs  # local: avoids a runtime<->logs import cycle

    try:
        path = logs.log_path(project)
    except Exception:  # noqa: BLE001 - the error envelope must come out even if this check fails
        return None
    return str(path) if path else None


def diagnostics_path(project):
    """`<projeto>/brolls/diagnostics.jsonl`; sem projeto, `$GB_HOME/diagnostics.jsonl`.

    `None` só quando nem a pasta pessoal se resolve (sem HOME no ambiente)."""
    if project:
        return Path(project).resolve() / "brolls/diagnostics.jsonl"
    try:
        return _paths.gb_home() / "diagnostics.jsonl"
    except RuntimeError:
        return None


def _failure_destination(project, fresh_init):
    """`(projeto, log)` de uma falha: o diagnostics.jsonl que de fato vai guardar o evento.

    O do projeto só quando a árvore `brolls/` já existe; senão (`init` numa pasta nova que
    falhou, comando somente leitura numa pasta sem `brolls/`, projeto que não existe), o de
    `$GB_HOME`, sem projeto: nenhuma árvore nasce só para guardar um erro, e a mensagem
    nunca cita um arquivo que ninguém gravou."""
    if project and not fresh_init:
        log = diagnostics_path(project)
        if log is not None and is_folder(log.parent):
            return project, log
    return None, diagnostics_path(None)


def _audit_log_wanted(event, log, read_only, in_project):
    """Se o evento vai para `log`.

    Um comando somente leitura nunca cria a árvore do projeto só para logar. Sem
    projeto, só um erro vai para `$GB_HOME/diagnostics.jsonl` (o sucesso não deixa
    rastro fora de um projeto)."""
    if not log:
        return False
    if in_project:
        return not read_only or is_folder(log.parent)
    return event.get("status") != "success"


def _write_audit_log(event, log, failure, result):
    """Grava a linha JSONL do evento; se a escrita falhar, anexa o aviso onde houver espaço."""
    try:
        # effect-exempt: audit log writes diagnostics.jsonl itself; an op line per audit line would log the logger
        log.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_file(log)
        # effect-exempt: audit log writes diagnostics.jsonl itself; an op line per audit line would log the logger
        with log.open("a", encoding="utf-8") as stream:
            stream.write(diagnostic_line(event) + "\n")
    except OSError:
        warning = {
            "code": "LOG_UNAVAILABLE",
            "message": "Não foi possível gravar diagnostics.jsonl. Confira espaço e permissões.",
        }
        if failure is not None:
            failure.payload.setdefault("warnings", []).append(warning)
        elif isinstance(result, dict):
            result.setdefault("warnings", []).append(warning)
        else:
            # `result` isn't a dict (e.g. None, or a non-mapping success value), so
            # the warning has nowhere to live in the response; it must not vanish.
            print(json.dumps(warning, ensure_ascii=False), file=sys.stderr)


def audited(args, execute):
    """Envolve a execução de um comando com trava, log de auditoria e trilha de erro/interrupção."""
    started = time.monotonic()
    event = {
        "at": now(),
        "operation": args.command,
        "status": "running",
        "state_committed": False,
        "recovered": False,
        "warnings": [],
    }
    token = ACTIVE.set(event)
    project = getattr(args, "project", None)
    read_only = (
        args.command in READ_ONLY_COMMANDS
        or (
            args.command,
            getattr(args, "action", None),
        )
        in READ_ONLY_ACTIONS
    )
    own_lock = (args.command, getattr(args, "action", None)) in OWN_LOCK_ACTIONS
    self_locked = args.command in SELF_LOCKED_COMMANDS
    log = diagnostics_path(project)
    # `init` numa pasta que ainda não existe: se falhar, a pasta some e o diagnóstico vai para
    # `$GB_HOME/diagnostics.jsonl` (o de quando não há projeto), não para uma pasta que a pessoa
    # nunca criou.
    fresh_init = args.command == "init" and bool(project) and not _visible(Path(project).expanduser())
    result = None
    failure = None
    try:
        if missing_project_tree(args.command, project):
            _refuse_missing_project(getattr(args, "project_typed", None) or project)
        with project_lock(None if read_only or own_lock or self_locked else project):
            if not getattr(args, "project_not_operated", False):
                _surface_layout_shape(args.command, project, getattr(args, "project_typed", None))
            result = execute(args)
            event["status"] = "success"
            # Comando que já devolve a própria lista de avisos (o `export`) marca o evento;
            # os avisos continuam no evento, e portanto no diagnostics.jsonl.
            if event["warnings"] and isinstance(result, dict) and not event.get("warnings_in_result"):
                result = {**result, "warnings": event["warnings"]}
            return result
    except Exception as exc:  # noqa: BLE001 - every failure leaves one classified event, never a "running" one
        app_log = None if fresh_init else _app_log(project)
        # Fora da árvore do projeto (destino em $GB_HOME) não há `brolls/`, nem gravação pendente.
        project, log = _failure_destination(project, fresh_init)
        _classify_audited_error(event, exc, log, project)
        failure = _audited_error_failure(args, event, log, app_log)
        raise failure from None
    except KeyboardInterrupt:
        app_log = None if fresh_init else _app_log(project)
        project, log = _failure_destination(project, fresh_init)
        failure = _audited_interrupt_failure(event, log, app_log)
        raise failure from None
    finally:
        event["duration_ms"] = round((time.monotonic() - started) * 1000)
        if _audit_log_wanted(event, log, read_only or self_locked, in_project=bool(project)):
            _write_audit_log(event, log, failure, result)
        ACTIVE.reset(token)


def write_diagnostics_log(project, event):
    """Append one diagnostics event to `diagnostics_path(project)`; best-effort.

    Used by cli.py's fallback handler for errors that happen outside audited() (e.g.
    before argument parsing finishes). Without a project, or in a folder with no `brolls/`
    yet (no tree is created just to keep an error), the event goes to
    `$GB_HOME/diagnostics.jsonl`. Returns the log path, or None when it could not write.
    """
    _project, log = _failure_destination(project, fresh_init=False)
    if log is None:
        return None
    event = {"at": now(), **event}
    try:
        # effect-exempt: audit log writes diagnostics.jsonl itself; an op line per audit line would log the logger
        log.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_file(log)
        # effect-exempt: audit log writes diagnostics.jsonl itself; an op line per audit line would log the logger
        with log.open("a", encoding="utf-8") as stream:
            stream.write(diagnostic_line(event) + "\n")
        return log
    except OSError:
        return None


class OperationError(Exception):
    def __init__(self, payload):
        self.payload = payload
        super().__init__(payload.get("message", "Falha na operação."))
