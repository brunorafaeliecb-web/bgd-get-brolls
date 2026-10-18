"""The one path every side effect goes through: one `event=op` line in getbrolls.log per operation.

A side effect is anything that changes the world outside this process: a file written,
replaced, renamed or deleted, a folder created, a subprocess run or killed, a network
request, a local HTTP server, plugin or toolchain code loaded. Each goes through a helper
here (`run`, `kill`, `replace`, `rename`, `unlink`, `rmdir`, `rmtree`, `mkdir`, `mkdtemp`, `temp_dir`,
`write_text`, `write_bytes`, `write_atomic`, `copy_file`, `copytree`, `link`, `open_url`,
`import_module`, `load_plugin`, `serve`) or, when one operation needs several calls
(temporary file + fsync + replace, a process and the file its output goes to), inside a
`with effects.op(...)` block. The guard `tests/_effects_guard.py` fails on a primitive
called anywhere else in the package.

Each operation logs ONE line (`logs.event`, logger `getbrolls.effects`, through the same
handler, `RedactingFilter` and rotation as every other line):

    event=op operation=fs.replace target=brolls/manifest.json outcome=ok duration_ms=3
    error_code=- error=- command=approve

- `operation`: low-cardinality `<area>.<verb>` from `OPERATIONS`;
- `target`: redacted (see `redact_target`): a path relative to the project or `$GB_HOME`,
  with every name the core does not give itself hashed (a title, a shot, a client folder
  never reach the log), else a hash of the path; a URL as its host only; a command as the
  name of its executable only (never its arguments); an id;
- `outcome`: `ok`, `error` (an `OSError` or a bug), `refused` (a `ValueError`: the
  operation said no) or `skipped` (the caller decided there was nothing to do);
- `duration_ms`; `error_code` (`logs.error_code`: the code the CLI envelope gives that
  exception) and `error` (the exception class only), both `-` on success;
- `command`: the CLI command running it (`runtime.ACTIVE`), to join with `command_start`;
- extras from `EXTRAS` only, small scalars: `exit`, `bytes`, `count`, `errors`, `fallback`, `sha12`,
  `pid`, `port`.

Never logged: file contents, tokens, environment values, arguments of a command, a query
string. An operation that finds nothing to do (`mkdir` of a folder that exists with
`exist_ok`, `unlink` of a missing file with `missing_ok`, `rmtree` of a missing tree)
changes nothing and logs nothing. Inside `batch`, the operations nested in it fold into
its `count` instead of logging one line each (a manifest save rewrites every candidate);
one that fails still logs its own WARNING line and counts in the batch's `errors`, even
when the caller suppresses the exception.

Logging never breaks the operation: `logs.event` never raises, a line the handler cannot
write is dropped (the command goes on, as with every other line), and the redaction and
the timing here are guarded too. An exception of the operation always goes up unchanged.

A few places never come here, on purpose, and carry the marker `# effect-exempt: <reason>`
the guard asks for. Logging there would recurse: `logs` itself (the handler's `_rotate` and
the private folder and file it opens for each line), the audit trail (`diagnostics.jsonl`)
and the lock files (`runtime.exclusive_lock`/`try_lock` and the other locks: the
installation log takes `try_lock` around each line, and a lock that stays busy already logs
`lock_busy`). The rest change nothing: an `os.open` that only reads, `os.devnull`, the
socket of the pinned HTTPS opener (whose request `net.get`/`net.download` already logs).
"""

# pylint: disable=broad-exception-caught,too-many-arguments,cyclic-import
# `broad-exception-caught`: logging must never break the operation (the exception of the
# operation itself is always re-raised). The cycle getbrolls.effects <-> getbrolls.runtime
# is broken at run time: `runtime` imports this module late (inside a function).

import contextlib
import contextvars
import errno
import hashlib
import hmac
import importlib
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import _paths, _tokens, logs, runtime

# The same logger `logs.get("effects")` gives, taken directly: `logs` -> `_paths` -> this module
# is an import cycle, so nothing of `logs` may be read while this module loads.
_logger = logging.getLogger("getbrolls.effects")

OPERATIONS = frozenset(
    {
        "fs.write",
        "fs.replace",
        "fs.rename",
        "fs.unlink",
        "fs.rmdir",
        "fs.rmtree",
        "fs.mkdir",
        "fs.copy",
        "fs.link",
        "proc.run",
        "proc.spawn",
        "proc.kill",
        "net.get",
        "net.download",
        "plugin.load",
        "code.import",
        "plugin.install",
        "plugin.remove",
        "server.start",
        "server.stop",
        "server.save",
        "ledger.commit",
        "migrate.apply",
        "deliver.place",
        "export.place",
    }
)
OUTCOMES = ("ok", "error", "refused", "skipped")
# Required on every `event=op` line, in this order.
FIELDS = ("operation", "target", "outcome", "duration_ms", "error_code", "error", "command")
EXTRAS = frozenset({"exit", "bytes", "count", "errors", "fallback", "sha12", "pid", "port"})
_TARGET_MAX = 160
_HOME_TAG = "$GB_HOME"

_BATCH: contextvars.ContextVar["Op | None"] = contextvars.ContextVar("getbrolls_effects_batch", default=None)


class Op:
    """One running operation: `set` adds extras, `skip` sets the outcome by hand."""

    __slots__ = ("errors", "extra", "fallback", "folded", "operation", "outcome", "target")

    def __init__(self, operation, target, extra, fallback=()):
        self.operation = operation
        self.target = target
        self.fallback = tuple(fallback)
        self.extra = dict(extra)
        self.outcome = None
        self.folded = 0
        self.errors = 0

    def set(self, **extra):
        """Add extras (`exit`, `bytes`…); a key outside `EXTRAS` is dropped at emission."""
        self.extra.update(extra)

    def skip(self, fallback=None):
        """Nothing was done, by the caller's decision (`outcome=skipped`); `fallback` names the
        expected failure the caller now recovers from another way (`eacces`), once it knows."""
        self.outcome = "skipped"
        if fallback is not None:
            self.extra["fallback"] = fallback


# Names the core gives its own folders and files: they appear as they are. Any other
# component of a path (a beat folder made from a title, a clip named after a shot, a client
# folder, a recipe slug, a candidate id, a plugin folder) becomes `#<8 hex>` (salted HMAC of
# the name, see `_salt`), the last one with a known extension, so lines about the same file
# still match on this machine and nothing typed or titled reaches the log.
CORE_NAMES = frozenset(
    {
        # project
        "brolls", "entrega", "exports", "aroll", "assets", "broll", "analysis", "reviews",
        "candidates", "previews", "clips", "work", "components", "media", "refs", "projetos",
        "manifest.json", "project.json", "events.jsonl", ".pending-transaction.json", "review.html",
        "references.json", "queue.json", "RULES.md", "BRIEF.md", "ROTEIRO.md", "ORIGEM.md", "README.md",
        "credits.md", "LATEST", ".getbrolls-export.json", "getbrolls-plan.json", "markers.json",
        "roteiro-state.json", "roteiro-reviews.jsonl", ".serve.pid", ".serve.log", ".serve.log.1",
        ".command.lock", "getbrolls.log", "diagnostics.jsonl", ".getbrolls-sources", "index.json",
        # $GB_HOME
        "plugins", "plugin-data", "runtime", "library", "logs", "marketplaces", ".cache", "cache",
        "plugins.json", "clients.json", "marketplaces.json", "trusted-profiles.json", "settings.json",
        "client.json", "getbrolls-recipe.json", "getbrolls-recipe.sha256", "recipe.lock.json", "components.json",
        "getbrolls-plugin.json", "plugin.py", "getbrolls-marketplace.json", "requirements.txt", ".venv", ".tools",
        "recipes", "tests",
        "index.lock", "output", "playwright",
    }
)  # fmt: skip
_SUFFIX = re.compile(r"\.([A-Za-z0-9]{1,5})$")
# Extensions a hashed file name keeps (only the last name of a path, and only these): what kind
# of file it is, never a word someone typed (`joao.silva`, `Cliente.Acme`).
KEPT_SUFFIXES = frozenset(
    {
        "mp4", "mov", "m4v", "webm", "mkv", "mp3", "wav", "m4a", "aac", "jpg", "jpeg", "png", "webp",
        "gif", "json", "jsonl", "md", "txt", "html", "log", "srt", "vtt", "csv", "zip",
    }
)  # fmt: skip
# A plain-text target that is an id (a plugin id, `127.0.0.1:<port>`, a candidate id, a module):
# shown as it is. Anything else, or anything shaped like a token, is hashed.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,119}")
# Besides the key shapes (`_tokens`, the one source of them, shared with `runtime.redact`): a `=` (a query or an
# assignment) or a long run of letters and digits.
_LONG_RUN = re.compile(r"(?i)=|[a-z0-9_-]{32,}")


def _secret_shaped(text) -> bool:
    """`text` may be a credential: a key shape of `_tokens`, a `=` or a long run (`_LONG_RUN`)."""
    return _tokens.secret_shaped(text) or _LONG_RUN.search(text) is not None


# Scheme of a URL target, in any case (`HTTPS://` too).
_URL_SCHEME = re.compile(r"(?i)^https?://")
# The key of the HMAC that hashes names: random, one per installation, 0600 in `$GB_HOME`,
# created the first time a line needs it and never logged. Without it a dictionary of client
# names or titles would reverse a plain sha256 of 32 bits.
SALT_FILE = ".log-salt"
_SALT_HEX = re.compile(r"[0-9a-f]{64}")
_SALT_PARTIAL = re.compile(r"[0-9a-f]{0,63}")
_SALT_READ_TRIES = 25
_SALT_READ_WAIT_S = 0.01
# Lock file of the salt repair where there is no `flock` (Windows); its name never matches `.log-salt*`.
SALT_REPAIR_LOCK = ".getbrolls-salt.lock"
# How long a repairer waits for another one that holds the folder (then repairs on its own).
_SALT_LOCK_WAIT_S = 2.0
_salts: dict[str, bytes] = {}
# Keys of this process only, for messages, where no salt was there yet (`message_key`): never written.
_message_keys: dict[str, bytes] = {}
# `$GB_HOME`s whose salt problem this process already warned about (one WARNING each).
_salt_warned: set[str] = set()


def _read_salt_once(path):
    """`(state, text)` of one read of `path`: `text` is what the file holds when `state` is None."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return "link", None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "missing", None
    except OSError as exc:
        return ("link" if exc.errno in (errno.ELOOP, errno.EMLINK) else "unreadable"), None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_uid != os.geteuid()):
            return "link", None
        return None, os.read(fd, 256).decode("ascii", "replace").strip()
    except OSError:
        return "unreadable", None
    finally:
        os.close(fd)


def _inspect_salt(path, tries=_SALT_READ_TRIES):
    """`(state, salt)` of the salt file `path`, never following a link; never raises.

    `state`: `ok`, `missing`, `link` (a link, not a regular file, or a file of another user),
    `unreadable` or `malformed`. An empty or short hex text is a salt still being written by
    another process (`_create_in_place`): read again a few times before calling it malformed."""
    attempt = 1
    while True:  # every pass returns or reads again: at least one read, at most `tries`
        state, text = _read_salt_once(path)
        if state is not None or text is None:
            return state or "unreadable", None
        if _SALT_HEX.fullmatch(text):
            return "ok", bytes.fromhex(text)
        if not _SALT_PARTIAL.fullmatch(text) or attempt >= tries:
            return "malformed", None
        attempt += 1
        time.sleep(_SALT_READ_WAIT_S)


def salt_state():
    """The state of this installation's salt for `doctor` (`ok`, `missing`, `readonly`, `link`,
    `malformed`, `unreadable`); only reads, never creates. `missing` is normal before the first log
    line; `readonly` is a missing salt that can never be created (the folder that would hold it, or
    the nearest one that exists, is not writable), so every process will use a salt of its own."""
    try:
        path = _paths.gb_home() / SALT_FILE
    except (RuntimeError, OSError, ValueError):
        return "unreadable"
    state = _inspect_salt(path, tries=1)[0]
    if state == "missing" and not _creatable(path.parent):
        return "readonly"
    return state


def _creatable(folder):
    """Could a file be created in `folder` (or, if it does not exist, in the nearest folder that does)?
    Only asks (`os.access`); never creates anything."""
    current = Path(folder)
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if current.parent == current:
                return False
            current = current.parent
            continue
        # lenient: doctor probe only: a folder it cannot even stat cannot hold the salt either
        except OSError:
            return False
        return stat.S_ISDIR(info.st_mode) and os.access(current, os.W_OK | os.X_OK)


def _new_salt_file(path):
    """A temporary file next to `path` with a fresh salt, 0600; returns its path."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(secrets.token_hex(32) + "\n")
    return temp


def _create_in_place(path):
    """Where there are no hardlinks (exFAT, some SMB shares): `O_CREAT|O_EXCL` on the final name,
    so only one process creates it; a reader that comes in the middle reads again."""
    with contextlib.suppress(FileExistsError):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(secrets.token_hex(32) + "\n")


def _create_salt(path, *, swap=False):
    """Write a new salt to `path` (0600) and return `(state, salt)` of what ended up there.

    A new file is a temporary one linked into place, so nobody reads half a salt, and the first
    process to link wins for everybody; with no hardlinks, `_create_in_place`. A malformed file
    (`swap`) is repaired under a `flock` on its folder (POSIX; `SALT_REPAIR_LOCK` without it): the
    first repairer moves it aside (`_set_aside`) and creates a new one by that same first-wins path,
    and every repairer then rereads the one file that persisted, never a salt of its own."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not swap:
        return _link_new_salt(path)
    # POSIX: `flock` on `$GB_HOME` itself; without it (Windows), a lock file of its own there, the way
    # the project log's rotation does it. A lock still busy after the wait: repair anyway.
    if logs.fcntl is not None:
        lock = logs.folder_lock(path.parent, _SALT_LOCK_WAIT_S)
    else:
        lock = runtime.try_lock(path.parent / SALT_REPAIR_LOCK, _SALT_LOCK_WAIT_S)
    with lock:
        state, found = _inspect_salt(path)
        if state != "malformed":
            # Another repairer already put a good salt in place (or it is gone): use that one.
            return (state, found) if state == "ok" else _link_new_salt(path)
        _set_aside(path)
        return _link_new_salt(path)


def _link_new_salt(path):
    temp = _new_salt_file(path)
    try:
        try:
            path.hardlink_to(temp)
        except FileExistsError:
            pass
        except OSError:
            _create_in_place(path)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
    return _inspect_salt(path)


def _set_aside(path):
    """Move the malformed salt out of the way under a name of its own, then drop it (and old leftovers).

    Another repairer that moved it first leaves nothing to move (`FileNotFoundError`, ignored). The
    file is removed with the raw `unlink`, not `effects.unlink`, on purpose: this runs while a line
    is being redacted, and logging it would recurse into the salt it is replacing."""
    aside = path.with_name(f"{path.name}.bad-{uuid.uuid4().hex}")
    with contextlib.suppress(FileNotFoundError):
        path.replace(aside)
    # This one and any left by a repairer that died midway (best effort).
    for leftover in path.parent.glob(f"{path.name}.bad-*"):
        with contextlib.suppress(OSError):
            leftover.unlink()  # effect-exempt: leftover of the salt repair; logging it would recurse into the salt


def _warn_salt(key, reason):
    """One WARNING per process and `$GB_HOME`: the hashes of this process will not match other runs."""
    if key in _salt_warned:
        return
    _salt_warned.add(key)
    logs.event(_logger, logging.WARNING, "log_salt_unavailable", reason=reason)


def _salt():
    """This installation's salt (cached per `$GB_HOME`).

    A missing salt is created; a malformed one is warned about and replaced. When the file is a
    link (never followed), the folder is read-only or nothing could be written, this process uses a
    salt of its own (a name is never hashed without a secret) and warns once: `link`, `readonly`,
    `nolink` or `malformed`."""
    try:
        path = _paths.gb_home() / SALT_FILE
    except (RuntimeError, OSError, ValueError):
        path = None
    key = str(path)
    if key in _salts:
        return _salts[key]
    found, reason = None, "readonly"
    if path is not None:
        state, found = _inspect_salt(path)
        reason = state
        if state == "malformed":
            _warn_salt(key, "malformed")
        if state in ("missing", "malformed"):
            try:
                state, found = _create_salt(path, swap=state == "malformed")
                reason = "nolink" if state == "missing" else state
            except OSError:
                reason = "readonly"
    if found is None:
        _warn_salt(key, "readonly" if reason in ("missing", "unreadable") else reason)
    _salts[key] = found or secrets.token_bytes(32)
    return _salts[key]


def message_key():
    """The key of the hashes in a message, a log line's outside path or the diagnostics: this installation's
    salt when it is already there (read, never created); without it, a key of this process only, never
    written and never warned about. Naming a file in a message writes nothing (`status` on a locked folder)."""
    try:
        path = _paths.gb_home() / SALT_FILE
    except (RuntimeError, OSError, ValueError):
        path = None
    key = str(path)
    if key not in _salts and key not in _message_keys:
        state, found = _inspect_salt(path) if path is not None else ("missing", None)
        if state == "ok" and found is not None:
            _salts[key] = found
        else:
            _message_keys[key] = secrets.token_bytes(32)
    return _salts.get(key) or _message_keys[key]


def _hashed(name, *, last=True, key=None):
    """`#<8 hex>` of `name` (HMAC-SHA256 with `key`, or this installation's salt); the last name of a path
    keeps a known extension (`#3fa2c1d9.mp4`), any other name keeps nothing."""
    found = _SUFFIX.search(name) if last else None
    kept = found.group(1).lower() if found and found.start() > 0 else ""
    suffix = f".{kept}" if kept in KEPT_SUFFIXES else ""
    digest = hmac.new(key or _salt(), name.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
    return "#" + digest[:8] + suffix


def _relative(path, base, tag):
    try:
        rel = path.relative_to(base)
    except ValueError:
        return None
    names = rel.parts
    parts = [
        name if name in CORE_NAMES else _hashed(name, last=index == len(names) - 1) for index, name in enumerate(names)
    ]
    return "/".join([tag, *parts] if tag else parts) or "."


def _forms(path):
    """`path` absolute as written and resolved (links followed): a project under `/var` is in
    `/private/var` on macOS, and either spelling must match."""
    absolute = Path(os.path.normpath(Path(path).absolute()))
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        resolved = absolute.resolve()
        if resolved != absolute:
            return (absolute, resolved)
    return (absolute,)


def _bases():
    bases = []
    project = logs.project_root()
    if project is not None:
        bases += [(form, "") for form in _forms(project)]
    with contextlib.suppress(RuntimeError, OSError, ValueError):
        bases += [(form, _HOME_TAG) for form in _forms(_paths.gb_home())]
    with contextlib.suppress(RuntimeError, OSError, ValueError):
        from .config import cache_root  # local: config imports runtime, which imports this module late

        bases += [(form, "$GB_CACHE_DIR") for form in _forms(cache_root())]
    explicit_runtime = os.environ.get("GB_RUNTIME_DIR")
    if explicit_runtime:
        with contextlib.suppress(RuntimeError, OSError, ValueError):
            bases += [(form, "$GB_RUNTIME_DIR") for form in _forms(explicit_runtime)]
    return bases


def _path_target(value):
    forms = _forms(os.fspath(value))
    for base, tag in _bases():
        for path in forms:
            found = _relative(path, base, tag)
            if found is not None:
                return found
    # Outside the project and `$GB_HOME` (the person's own media, a client folder, a
    # temporary folder): only a hash of the whole path, with its extension.
    return _hashed(str(forms[0])) if forms[0].name else "-"


def _url_target(value):
    """Only the host, lowercase: a URL path can be the credential (a webhook, a share link, a
    file id, a token), so the log keeps the rule it always had for URLs (only the host)."""
    return (urllib.parse.urlsplit(value).hostname or "-").lower()


def _executable(argv0):
    """The name of the executable alone, without `.exe` (`ffmpeg` on every system).

    A command given as one text (`shell=True`, `"echo --api-key=…"`) keeps only its first word."""
    text = os.fspath(argv0)
    if isinstance(text, str) and text.split():
        try:
            text = shlex.split(text)[0] if text.strip() else text
        except ValueError:
            text = text.split()[0]
    name = Path(text).name
    return name[:-4] if name.lower().endswith(".exe") else name


def redact_target(value):
    """`value` as it may appear in the log; never raises.

    A `Path` (or a text with a separator) inside the project is relative to it
    (`brolls/manifest.json`), inside `$GB_HOME` relative to it (`$GB_HOME/plugins.json`), and
    the same for the cache (`$GB_CACHE_DIR/…`) and an explicit `$GB_RUNTIME_DIR`;
    each component the core does not name itself (`CORE_NAMES`) becomes `#<8 hex>` plus its
    extension. Outside both, only `#<8 hex>` of the whole path. An `http(s)` URL is its host
    only. A list or tuple is a command: the name of its executable only (no `.exe`). Anything else
    (a plugin id, `127.0.0.1:<port>`) stays as it is. Capped at 160 characters.
    """
    try:
        if isinstance(value, (list, tuple)):
            text = _executable(value[0]) if value else "-"
        elif isinstance(value, os.PathLike):
            text = _path_target(value)
        else:
            text = str(value)
            if _URL_SCHEME.match(text):
                text = _url_target(text)
            elif "/" in text or os.sep in text:
                text = _path_target(text)
            elif not _ID.fullmatch(text) or _secret_shaped(text):
                # Not an id (`demo_export`, `127.0.0.1:8767`): free text or a token, only its hash.
                text = _hashed(text, last=False)
        return text[:_TARGET_MAX]
    except Exception:  # noqa: BLE001 - logging must never break the operation
        return "-"


def _command():
    """The command of `runtime.audited`, else the one the CLI set for the process (`serve`, whose
    server answers in threads that `audited` never covers)."""
    current = runtime.ACTIVE.get()
    if isinstance(current, dict):
        return current.get("operation")
    return logs.current_command()


def _fallback_reason(handle, exc):
    """The reason (`exdev`, `eexist`…) when `exc` is a failure the caller expects and falls back
    from (`fallback`: exception types or `errno` numbers), else None."""
    if exc is None or not handle.fallback:
        return None
    number = getattr(exc, "errno", None)
    expected = any(isinstance(item, type) and isinstance(exc, item) for item in handle.fallback) or (
        isinstance(number, int) and number in handle.fallback
    )
    if not expected:
        return None
    name = errno.errorcode.get(number) if isinstance(number, int) else None
    return (name or type(exc).__name__).lower()


def _outcome(handle, exc):
    """`refused` when the operation said no (a `ValueError`, or a refusal of the core that declares
    its own code); `error` for an I/O error, a failed external tool, Ctrl-C or a bug."""
    if exc is None:
        return handle.outcome or "ok"
    return logs.outcome_for(exc)


def _emit(handle, started, exc):
    try:
        reason = _fallback_reason(handle, exc)
        if reason is not None:
            # Tried, failed the way the caller expects, and the caller goes on another way.
            handle.extra["fallback"] = reason
            exc = None
            outcome = "skipped"
        else:
            outcome = _outcome(handle, exc)
        level = logging.INFO if outcome in ("ok", "skipped") else logging.WARNING
        if not _logger.isEnabledFor(level):
            return  # no handler wants the line: nothing to redact nor render
        if handle.folded:
            handle.extra["count"] = handle.folded
        if handle.errors:
            handle.extra["errors"] = handle.errors
        extra = {key: value for key, value in handle.extra.items() if key in EXTRAS}
        logs.event(
            _logger,
            level,
            "op",
            operation=handle.operation,
            target=redact_target(handle.target),
            outcome=outcome,
            duration_ms=round((time.monotonic() - started) * 1000),
            error_code=logs.error_code(exc) if exc is not None else None,
            error=type(exc).__name__ if exc is not None else None,
            command=_command(),
            **extra,
        )
    except Exception:  # noqa: BLE001, S110 - logging must never break the operation
        pass


@contextlib.contextmanager
def op(operation, target, *, fallback=(), **extra) -> Iterator[Op]:
    """Time the block and log one `event=op` line for it when it ends, ok or not.

    `fallback` names the failures the caller expects and recovers from by another way
    (exception types or `errno` numbers: a hardlink across volumes that becomes a copy, a
    name already taken that becomes the next number): such a failure is an INFO line with
    `outcome=skipped` and `fallback=<reason>`, never an error. Inside a `batch`, the block
    only adds to the batch's `count`. The exception of the block goes up unchanged, after
    the line.
    """
    handle = Op(operation, target, extra, fallback)
    outer = _BATCH.get()
    if outer is not None:
        started = time.monotonic()
        try:
            yield handle
        except BaseException as exc:
            # A failure inside a batch is never folded away: its own line, and `errors` on the batch.
            outer.errors += 1
            _emit(handle, started, exc)
            raise
        outer.folded += 1
        return
    started = time.monotonic()
    try:
        yield handle
    except BaseException as exc:
        _emit(handle, started, exc)
        raise
    _emit(handle, started, None)


@contextlib.contextmanager
def batch(operation, target, **extra) -> Iterator[Op]:
    """`op` whose nested operations fold into its `count`: one line for the whole batch.

    Batches are never nested (the two callers, `ledger` and `export_folder`, open none inside)."""
    handle = Op(operation, target, extra)
    started = time.monotonic()
    token = _BATCH.set(handle)
    try:
        yield handle
    except BaseException as exc:
        _emit(handle, started, exc)
        raise
    finally:
        _BATCH.reset(token)
    _emit(handle, started, None)


# --- processes ---------------------------------------------------------------------------


def run(argv, *, operation="proc.run", **kwargs):
    """`subprocess.run(argv, **kwargs)`, logged with `exit`; `outcome` says whether it ran.

    The target is the executable's name only; a command given as one text (`shell=True`)
    logs only its first word."""
    with op(operation, (argv,) if isinstance(argv, (str, bytes, os.PathLike)) else list(argv)) as handle:
        # The caller chooses `check` (and every other option): this only adds the line.
        try:
            completed = subprocess.run(argv, **kwargs)  # noqa: S603, PLW1510  # pylint: disable=subprocess-run-check
        except subprocess.CalledProcessError as exc:
            handle.set(exit=exc.returncode)  # `check=True`: the line still says how it exited
            raise
        handle.set(exit=getattr(completed, "returncode", None))
        return completed


def kill(pid, sig, *, target):
    """`os.kill(pid, sig)`, logged as `proc.kill` with the `pid`."""
    with op("proc.kill", target, pid=pid):
        os.kill(pid, sig)


# --- files and folders -------------------------------------------------------------------


def replace(src, dst, *, operation="fs.replace", target=None, fallback=()):
    """`Path(src).replace(dst)`; the target is `dst` unless the caller names another one."""
    with op(operation, dst if target is None else target, fallback=fallback):
        return Path(src).replace(dst)


def rename(src, dst, *, operation="fs.rename", target=None, fallback=()):
    """`Path(src).rename(dst)` (refuses an existing `dst` on Windows); the target is `dst`."""
    with op(operation, dst if target is None else target, fallback=fallback):
        return Path(src).rename(dst)


def unlink(path, *, missing_ok=False, operation="fs.unlink", target=None, fallback=()):
    """`Path(path).unlink(missing_ok=...)`; a file that is already missing logs nothing.

    Without `missing_ok` the call still raises `FileNotFoundError`, as it would, but with
    no line: nothing changed (callers often `suppress` it as "make sure it is gone").
    """
    if not os.path.lexists(path):  # lenient: absent file is a no-op cleanup, no line to log
        Path(path).unlink(missing_ok=missing_ok)
        return
    with op(operation, path if target is None else target, fallback=fallback):
        Path(path).unlink(missing_ok=missing_ok)


def rmdir(path, *, operation="fs.rmdir", target=None):
    """`Path(path).rmdir()`: an empty folder."""
    with op(operation, path if target is None else target):
        Path(path).rmdir()


def rmtree(path, *, ignore_errors=False, operation="fs.rmtree", target=None):
    """`shutil.rmtree`; a tree that is not there, with `ignore_errors`, changes nothing and logs nothing.

    For a tree with read-only entries (a git clone on Windows), `runtime.force_rmtree`,
    which logs the same `fs.rmtree` line and never raises.
    """
    if ignore_errors and not os.path.lexists(path):  # lenient: absent tree is a no-op cleanup, as in rmtree
        return
    with op(operation, path if target is None else target):
        shutil.rmtree(path, ignore_errors=ignore_errors)


def mkdir(path, *, mode=0o777, parents=False, exist_ok=False, target=None):
    """`Path(path).mkdir(...)`; a folder that already exists logs nothing."""
    path = Path(path)
    if path.is_dir():  # lenient: existing folder is a no-op mkdir; the real call raises
        # Nothing to create: `exist_ok` returns, and without it `FileExistsError` goes up as it
        # would, with no line (callers often `suppress` it as "make sure it exists").
        path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
        return path
    with op("fs.mkdir", path if target is None else target):
        path.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
    return path


def mkdtemp(*, prefix=None, suffix=None, folder=None, operation="fs.mkdir"):
    """`tempfile.mkdtemp`, logged with the folder it was made in (never its random name)."""
    with op(operation, folder if folder is not None else Path(tempfile.gettempdir())):
        return tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=folder)


@contextlib.contextmanager
def temp_dir(*, prefix=None, folder=None) -> Iterator[str]:
    """`tempfile.TemporaryDirectory` with its two lines: `fs.mkdir` when it is made and `fs.rmtree`
    when it goes, both with the folder it lives in as the target (never its random name).

    Yields the path as text, like `TemporaryDirectory`. The cleanup is `runtime.force_rmtree`
    (read-only entries too, never raises): a leftover is swept by the next run."""
    where = folder if folder is not None else Path(tempfile.gettempdir())
    path = mkdtemp(prefix=prefix, folder=folder)
    try:
        yield path
    finally:
        runtime.force_rmtree(path, target=where)


def write_text(path, text, *, encoding, newline=None, exclusive=False):
    """Write `text` to `path` (`x` mode with `exclusive`: never over an existing file).

    `encoding` is required, as for every text I/O of the package (always `"utf-8"`)."""
    with (
        op("fs.write", path),
        Path(path).open("x" if exclusive else "w", encoding=encoding, newline=newline) as stream,
    ):
        stream.write(text)
    return Path(path)


def write_bytes(path, data, *, exclusive=False, target=None):
    """Write `data` to `path` (`x` mode with `exclusive`)."""
    with (
        op("fs.write", path if target is None else target),
        Path(path).open("xb" if exclusive else "wb") as stream,
    ):
        stream.write(data)
    return Path(path)


def write_atomic(path, text, *, unique=False, operation="fs.write", target=None):
    """Write `text` whole at once (temporary + fsync + replace), UTF-8 with `\\n` kept as it is.

    Without `newline="\\n"`, Windows would turn every `\\n` into `\\r\\n`: the file would come
    out different from macOS and Linux. `unique=True` gives the temporary a name of this
    process only (`<name>.<uuid>.tmp`), so two writers never share one. One line, for `path`.
    """
    path = Path(path)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp" if unique else path.name + ".tmp")
    with op(operation, path if target is None else target):
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return path


def copy_file(src, dst, *, metadata=False, operation="fs.copy", target=None):
    """`shutil.copy2` (`metadata=True`) or `shutil.copyfile`; the target is `dst`."""
    with op(operation, dst if target is None else target):
        return Path(shutil.copy2(src, dst) if metadata else shutil.copyfile(src, dst))


def link(src, dst, *, symbolic=False, target=None, fallback=()):
    """`os.link(src, dst)` (`os.symlink` with `symbolic`); the target is `dst`."""
    with op("fs.link", dst if target is None else target, fallback=fallback):
        if symbolic:
            Path(dst).symlink_to(src)
        else:
            Path(dst).hardlink_to(src)


def copytree(src, dst, *, operation="fs.copy", target=None, **kwargs):
    """`shutil.copytree(src, dst, **kwargs)`; the target is `dst`."""
    with op(operation, dst if target is None else target):
        return Path(shutil.copytree(src, dst, **kwargs))


def open_url(request, *, timeout, operation="net.get"):
    """`urllib.request.urlopen(request)`, logged when the response arrives.

    The target is the request's host. The caller reads and closes the
    response; what it reads after this is its own business (`net.download` wraps it all).
    """
    url = request if isinstance(request, str) else getattr(request, "full_url", None)
    with op(operation, url):
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - the caller checks the scheme


def import_module(name, *, operation="plugin.load", target=None):
    """`importlib.import_module(name)`, logged with the module name (or the caller's target)."""
    with op(operation, name if target is None else target):
        # lenient: dynamic import of a module the caller names (a toolchain runner), never os or os.path
        return importlib.import_module(name)


def load_plugin(plugin_id):
    """`with effects.load_plugin(id):` around the code of plugin `id` being executed: `plugin.load`.

    A block, not a call: the loader compiles the plugin's source and runs it with `exec` right
    there, so no bytecode is read or written (see `sdk.loader`)."""
    return op("plugin.load", plugin_id)


def serve(server, *, address):
    """`server.serve_forever()`, logged as `server.stop` when it returns.

    `address` is what the person sees (`127.0.0.1:<port>`); the caller logs `server.start`
    once the port is bound. `server.stop` carries how long the server ran (`duration_ms`).
    Ctrl-C is the normal way to stop a server in the foreground: `outcome=ok`, and the
    `KeyboardInterrupt` still goes up to the caller.
    """
    interrupted = False
    with op("server.stop", address):
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            interrupted = True
    if interrupted:
        raise KeyboardInterrupt
