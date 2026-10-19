"""POSIX-only names that the serve tests simulate on every OS.

Windows' `os` has no `WNOHANG` and its `signal` has no `SIGKILL`: a test that simulates
the POSIX path reads them through `posix_names()`, which puts them in place for the block
(with the real values where they exist). `without_posix_names()` does the opposite, as on
Windows, to prove the Windows path never reaches them.
"""

import contextlib
import os
import signal
from unittest import mock

WNOHANG = getattr(os, "WNOHANG", 1)
SIGKILL = getattr(signal, "SIGKILL", 9)
_NAMES = ((os, "WNOHANG", WNOHANG), (signal, "SIGKILL", SIGKILL))


@contextlib.contextmanager
def posix_names():
    """`os.WNOHANG` and `signal.SIGKILL` exist for the block, on any OS."""
    with contextlib.ExitStack() as stack:
        for module, name, value in _NAMES:
            stack.enter_context(mock.patch.object(module, name, value, create=True))
        yield


@contextlib.contextmanager
def without_posix_names():
    """`os.WNOHANG` and `signal.SIGKILL` are gone for the block, as on Windows."""
    saved = [(module, name, getattr(module, name)) for module, name, _ in _NAMES if hasattr(module, name)]
    for module, name, _value in saved:
        delattr(module, name)
    try:
        yield
    finally:
        for module, name, value in saved:
            setattr(module, name, value)


@contextlib.contextmanager
def without_nofollow():
    """`os.O_NOFOLLOW` is gone for the block (it never existed on Windows); where it is missing already, nothing
    changes."""
    saved = getattr(os, "O_NOFOLLOW", None)
    if saved is not None:
        delattr(os, "O_NOFOLLOW")
    try:
        yield
    finally:
        if saved is not None:
            os.O_NOFOLLOW = saved
