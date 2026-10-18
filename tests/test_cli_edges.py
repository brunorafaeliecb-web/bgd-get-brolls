"""`getbrolls.cli` nas bordas do contrato: argumento posicional que sobra, `--port` que não é
número, `--project` com `~` sem pasta pessoal, erro interno sem onde gravar o diagnostics, um
`--project` sem valor no fim e o stdout que nem o `dup2` consegue calar.

Envelope, código de saída e `error_code` seguem `docs/CLI_CONTRACT.md`.
"""

import argparse
import contextlib
import errno
import getpass
import io
import json
import ntpath
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
import _paths as _test_paths  # noqa: F401  (efeito de import: põe scripts/ no sys.path)  # pylint: disable=unused-import

from getbrolls import cli, runtime
from getbrolls.errors import UsageError

# pylint: disable=protected-access


def entry(argv, *patches, stdout=None):
    """`cli.entrypoint()` com `sys.argv` = argv; devolve (código, stdout, stderr).

    Um `SystemExit` do argparse vira o código dele, como no processo de verdade."""
    out, err = stdout if stdout is not None else io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(cli.sys, "argv", ["getbrolls", *map(str, argv)]))
        stack.enter_context(mock.patch.object(cli.sys, "stdout", out))
        stack.enter_context(contextlib.redirect_stderr(err))
        for patcher in patches:
            stack.enter_context(patcher)
        try:
            code = cli.entrypoint()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="gb-cli-edges-")).resolve()
        self.addCleanup(_rmtree_quiet, self.home)
        env = mock.patch.dict(os.environ, {"GB_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)


def _rmtree_quiet(path):
    with contextlib.suppress(OSError):
        for child in sorted(Path(path).rglob("*"), reverse=True):
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        Path(path).rmdir()


class UsageEdgeTests(HomeCase):
    def test_a_leftover_positional_is_a_usage_error_without_a_suggestion(self):
        code, out, err = entry(["status", "--project", self.home / "proj", "sobrando"])
        self.assertEqual(cli.EXIT_USAGE_ERROR, code)
        self.assertEqual("", out)
        payload = json.loads(err)
        self.assertEqual("USAGE_ERROR", payload["error_code"])
        self.assertEqual("unrecognized arguments: sobrando", payload["error_en"])
        self.assertIsNone(payload["suggestion"])
        self.assertFalse((self.home / "proj").exists())

    def test_a_port_that_is_not_a_number_is_a_usage_error(self):
        code, out, err = entry(["serve", "--project", self.home / "proj", "--port", "abc"])
        self.assertEqual(cli.EXIT_USAGE_ERROR, code)
        self.assertEqual("", out)
        payload = json.loads(err)
        self.assertEqual("USAGE_ERROR", payload["error_code"])
        self.assertIn("porta inválida: 'abc'", payload["error"])
        self.assertIn("1 a 65535", payload["error"])
        self.assertFalse((self.home / "proj").exists())


class NoHomeFolderTests(unittest.TestCase):
    """Um `~` que não resolve (sem HOME, ou `~usuario` que não existe) nunca levanta aqui."""

    def no_home(self):
        return mock.patch.object(Path, "expanduser", side_effect=RuntimeError("Could not determine home directory."))

    def test_the_diagnostics_guess_keeps_an_unresolvable_tilde_as_typed(self):
        with self.no_home():
            self.assertEqual("~ninguem/proj", cli._expanded("~ninguem/proj"))

    def test_a_project_with_an_unresolvable_tilde_is_refused_as_usage(self):
        args = argparse.Namespace(command="status", project="~ninguem/proj")
        with self.no_home(), self.assertRaisesRegex(UsageError, "~ninguem/proj"):
            cli._expand_project(args)
        self.assertEqual("~ninguem/proj", args.project)

    def test_serve_with_an_unresolvable_tilde_does_not_write_the_project_log(self):
        for stop in (False, True):
            args = argparse.Namespace(command="serve", project="~ninguem/proj", stop=stop)
            with self.subTest(stop=stop), self.no_home():
                self.assertFalse(cli._serve_writes(args))


class UnknownUserTildeTests(HomeCase):
    def test_a_project_under_an_unknown_user_is_a_usage_error_not_a_bug(self):
        # `~gbnouser_zz` is no account here: on POSIX `expanduser` raises RuntimeError, which used
        # to reach the command and exit 3 (INTERNAL_ERROR) with a traceback in diagnostics.jsonl.
        for command in ("status", "serve", "brief", "review"):
            with self.subTest(command=command):
                code, out, err = entry([command, "--project", "~gbnouser_zz/p"])
                self.assertEqual(cli.EXIT_USAGE_ERROR, code)
                self.assertEqual("", out)
                payload = json.loads(err)
                self.assertEqual("USAGE_ERROR", payload["error_code"])
                self.assertIn("~gbnouser_zz/p", payload["error"])
                self.assertNotIn("Traceback", err)
        self.assertFalse((self.home / "diagnostics.jsonl").exists())
        self.assertFalse(Path("~gbnouser_zz").exists())


class SymlinkLoopProjectTests(HomeCase):
    def test_a_project_that_is_a_link_loop_is_a_usage_error_not_a_traceback(self):
        # 3.11 raised RuntimeError from `Path.resolve()` (a traceback, exit 3); 3.14 answered "not a folder".
        loop_a, loop_b = self.home / "loop-a", self.home / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        for command, project in (("status", loop_a), ("init", loop_a), ("status", loop_a / "child")):
            with self.subTest(command=command, project=project.name):
                code, out, err = entry([command, "--project", project])
                self.assertEqual(cli.EXIT_USAGE_ERROR, code)
                self.assertEqual("", out)
                payload = json.loads(err)
                self.assertEqual("USAGE_ERROR", payload["error_code"])
                self.assertIn("laço de links", payload["error"])
                self.assertIn(str(project), payload["error"])  # as typed
                self.assertNotIn("Traceback", err)


class WindowsTildeTests(HomeCase):
    """Windows' `ntpath.expanduser` never raises: `~nome` becomes `C:\\Users\\nome` without checking."""

    def windows_expanduser(self):
        env = {"USERPROFILE": "C:\\Users\\runneradmin", "USERNAME": "runneradmin"}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(mock.patch.object(os.path, "expanduser", ntpath.expanduser))
        stack.enter_context(mock.patch.object(Path, "expanduser", lambda self: Path(ntpath.expanduser(str(self)))))
        return stack

    def test_a_guessed_profile_that_does_not_exist_is_a_usage_error(self):
        with self.windows_expanduser():
            code, out, err = entry(["status", "--project", "~gbnouser_zz/p"])
        self.assertEqual((cli.EXIT_USAGE_ERROR, ""), (code, out))
        payload = json.loads(err)
        self.assertEqual("USAGE_ERROR", payload["error_code"])
        self.assertIn("--project ~gbnouser_zz/p: não consegui expandir o `~`", payload["error"])
        self.assertFalse((self.home / "diagnostics.jsonl").exists())


class ExistingTildeTests(unittest.TestCase):
    def test_plain_tilde_and_an_existing_user_expand_to_their_home(self):
        # `~` is HOME; `~<user>` is that account's home from the user database (`pwd` on POSIX),
        # which is not HOME when HOME points elsewhere (a sandbox, a patched test run).
        user = getpass.getuser()
        if os.name == "nt":
            user_home = Path(f"~{user}").expanduser()
        else:
            import pwd  # pylint: disable=import-outside-toplevel  # POSIX only

            user_home = Path(pwd.getpwnam(user).pw_dir)
        for typed, home in (("~/proj", Path("~").expanduser()), (f"~{user}/proj", user_home)):
            with self.subTest(typed=typed):
                args = argparse.Namespace(command="status", project=typed)
                cli._expand_project(args)
                self.assertEqual(str(home / "proj"), args.project)

    def test_a_path_without_tilde_is_left_alone(self):
        args = argparse.Namespace(command="status", project="relativo/~gbnouser_zz")
        cli._expand_project(args)
        self.assertEqual("relativo/~gbnouser_zz", args.project)


class InternalErrorEdgeTests(HomeCase):
    def test_without_a_home_folder_the_internal_error_says_nothing_was_written(self):
        environ = {key: value for key, value in os.environ.items() if key not in ("GB_HOME", "GETBROLLS_HOME")}
        code, out, err = entry(
            ["providers"],
            mock.patch.object(cli, "main", side_effect=RuntimeError("kaboom")),
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(runtime.Path, "home", side_effect=RuntimeError("sem HOME")),
        )
        self.assertEqual(cli.EXIT_INTERNAL_ERROR, code)
        self.assertEqual("", out)
        payload = json.loads(err)
        self.assertEqual("INTERNAL_ERROR", payload["error_code"])
        self.assertEqual("RuntimeError", payload["type"])
        self.assertIn("Não foi possível gravar diagnostics.jsonl.", payload["error"])
        self.assertIsNone(payload["log"])
        self.assertIsNone(payload["app_log"])
        self.assertNotIn("kaboom", err)
        self.assertEqual([], list(self.home.iterdir()))

    def test_a_project_flag_without_a_value_logs_the_internal_error_to_gb_home(self):
        code, _out, err = entry(["providers", "--project"], mock.patch.object(cli, "main", side_effect=KeyError("x")))
        self.assertEqual(cli.EXIT_INTERNAL_ERROR, code)
        payload = json.loads(err)
        self.assertEqual("INTERNAL_ERROR", payload["error_code"])
        log = self.home / "diagnostics.jsonl"
        self.assertEqual(str(log), payload["log"])
        self.assertIn("KeyError", json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["traceback"])
        # Only the diagnostics line: the traceback names files outside every known folder (the interpreter's),
        # hashed with the installation's key only when it is already there; it never creates `.log-salt`.
        self.assertEqual({"diagnostics.jsonl"}, {p.name for p in self.home.iterdir()})


class ClosedPipeTests(HomeCase):
    def test_a_closed_pipe_whose_stdout_cannot_be_redirected_keeps_the_exit(self):
        class PipeStdout(io.StringIO):
            def fileno(self):
                return 97

        def ran(argv=None):
            cli._RESULT_EXIT.set(cli.EXIT_PREREQUISITE)
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        dup2 = mock.patch.object(cli.os, "dup2", side_effect=OSError(errno.EBADF, "Bad file descriptor"))
        with dup2 as failing:
            code, _out, err = entry(["doctor"], mock.patch.object(cli, "main", side_effect=ran), stdout=PipeStdout())
        self.assertEqual(cli.EXIT_PREREQUISITE, code)
        self.assertEqual("", err)
        self.assertEqual(1, failing.call_count)
        self.assertEqual(97, failing.call_args.args[1])


if __name__ == "__main__":
    unittest.main()


class PortugueseHelpTests(unittest.TestCase):
    ENGLISH = ("usage:", "options:", "positional arguments:", "show this help message and exit", "show program")

    def test_help_of_every_command_has_no_english_argparse_chrome(self):
        parser = cli.build_parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
        self.assertGreaterEqual(len(commands), 40)
        for name, sub in (("", parser), *commands.items()):
            with self.subTest(command=name):
                text = sub.format_help()
                self.assertTrue(text.startswith("uso: "), text[:40])
                for chrome in self.ENGLISH:
                    self.assertNotIn(chrome, text)
                self.assertIn("mostra esta ajuda e sai", text)

    def test_the_continuation_lines_of_the_usage_follow_the_shorter_prefix(self):
        parser = cli.build_parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
        wrapped = 0
        for name, sub in (("", parser), *commands.items()):
            with self.subTest(command=name):
                usage = sub.format_help().split("\n\n", 1)[0].split("\n")
                if len(usage) > 1:
                    wrapped += 1
                    prefix = len("uso: ") + len(sub.prog) + 1
                    self.assertEqual(usage[0].index("["), prefix)
                    for line in usage[1:]:
                        self.assertEqual(prefix, len(line) - len(line.lstrip()), line)
        self.assertGreater(wrapped, 10)

    def test_the_usage_line_of_a_usage_error_stays_as_in_2_5(self):
        code, _, err = entry(["migrate"])
        self.assertEqual(cli.EXIT_USAGE_ERROR, code)
        self.assertTrue(json.loads(err)["usage"].startswith("usage: "))


class OutputEnvTests(HomeCase):
    def test_a_value_other_than_json_warns_and_changes_nothing(self):
        for value, warns in (("xml", True), ("JSON", True), ("json", False), ("", False), (" json ", False)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"GB_OUTPUT": value}):
                code, out, _ = entry(["capabilities"])
                self.assertEqual(0, code)
                codes = [w["code"] for w in json.loads(out).get("warnings", [])]
                self.assertEqual(warns, "ENV_VALUE_IGNORED" in codes, codes)
