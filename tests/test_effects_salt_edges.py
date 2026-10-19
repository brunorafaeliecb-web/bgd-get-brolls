"""`getbrolls.effects`: o sal do log nas bordas (arquivo que não abre, pasta no lugar dele, dono
errado, `$GB_HOME` sem pasta pessoal) e a redação de alvos fora do comum.

Cada caso confere o estado que o `doctor` vê (`salt_state`), o aviso único `log_salt_unavailable`
e que o arquivo no disco ficou como estava; nunca um hash sem sal.
"""

import errno
import os
import unittest
from pathlib import Path
from unittest import mock

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
import _paths as _test_paths  # noqa: F401  (efeito de import: põe scripts/ no sys.path)  # pylint: disable=unused-import
from test_effects import EffectsCase, target_of

from getbrolls import effects

# pylint: disable=protected-access

POSIX = os.name != "nt"
NOT_ROOT = POSIX and os.geteuid() != 0
HASH = r"#[0-9a-f]{8}"


class SaltCase(EffectsCase):
    """Um `$GB_HOME` próprio, nenhum sal em cache e nenhum aviso já dado neste processo."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "gbhome"
        env = mock.patch.dict(os.environ, {"GB_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        salts = mock.patch.dict(effects._salts, clear=True)
        salts.start()
        self.addCleanup(salts.stop)
        warned = mock.patch.object(effects, "_salt_warned", set())
        warned.start()
        self.addCleanup(warned.stop)

    def salt_file(self):
        return self.home / effects.SALT_FILE

    def warnings(self):
        return [fields["reason"] for _level, name, fields in self.recorder.calls if name == "log_salt_unavailable"]

    def write_salt(self, text):
        self.home.mkdir(exist_ok=True)
        self.salt_file().write_text(text, encoding="ascii")


class SaltFileStateTests(SaltCase):
    def test_a_salt_another_process_linked_first_is_the_one_used(self):
        # The race of two repairers: the name is already taken when this one links, so it keeps
        # the salt that won and drops its own temporary file (no coverage left to chance).
        good = "a" * 64
        self.write_salt(good + "\n")
        self.assertEqual(("ok", bytes.fromhex(good)), effects._link_new_salt(self.salt_file()))
        self.assertEqual([effects.SALT_FILE], [p.name for p in self.home.iterdir()])

    @unittest.skipUnless(NOT_ROOT, "permissões POSIX, sem root")
    def test_a_salt_this_user_cannot_read_is_unreadable_never_replaced_and_warns_once(self):
        good = "ab" * 32 + "\n"
        self.write_salt(good)
        self.salt_file().chmod(0)
        self.addCleanup(self.salt_file().chmod, 0o600)
        self.assertEqual("unreadable", effects.salt_state())
        first = target_of("texto livre do cliente")
        second = target_of("texto livre do cliente")
        self.assertRegex(first, f"^{HASH}$")
        self.assertEqual(first, second)  # o sal do processo vale até o fim dele
        self.assertEqual(["readonly"], self.warnings())
        self.assertEqual([30], [level for level, name, _ in self.recorder.calls if name == "log_salt_unavailable"])
        self.salt_file().chmod(0o600)
        self.assertEqual(good, self.salt_file().read_text(encoding="ascii"))  # nunca trocado
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))

    def test_a_link_swapped_in_after_the_check_is_refused_as_a_link(self):
        self.write_salt("ab" * 32 + "\n")
        real_open = os.open

        def raced(path, flags, *args):
            if Path(path) == self.salt_file():
                raise OSError(errno.ELOOP, "link no lugar do sal")
            return real_open(path, flags, *args)

        with mock.patch.object(effects.os, "open", side_effect=raced):
            self.assertEqual("link", effects.salt_state())
            self.assertEqual(("link", None), effects._inspect_salt(self.salt_file()))

    def test_a_salt_that_fails_to_open_for_another_reason_is_unreadable(self):
        self.write_salt("ab" * 32 + "\n")
        with mock.patch.object(effects.os, "open", side_effect=OSError(errno.EIO, "disco")):
            self.assertEqual(("unreadable", None), effects._inspect_salt(self.salt_file()))

    @unittest.skipUnless(POSIX, "abrir uma pasta com os.open é POSIX")
    def test_a_folder_in_the_place_of_the_salt_is_a_link_and_is_left_alone(self):
        self.salt_file().mkdir(parents=True)
        self.assertEqual("link", effects.salt_state())
        shown = target_of("texto livre do cliente")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual(["link"], self.warnings())
        self.assertTrue(self.salt_file().is_dir())
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))

    @unittest.skipUnless(POSIX, "dono do arquivo é POSIX")
    def test_a_salt_of_another_user_is_never_used(self):
        self.write_salt("cd" * 32 + "\n")
        with mock.patch.object(effects.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual("link", effects.salt_state())
            shown = target_of("texto livre do cliente")
        self.assertEqual(["link"], self.warnings())
        self.fresh_salts()
        self.assertNotEqual(shown, target_of("texto livre do cliente"))  # o do outro dono não era o usado
        self.assertEqual("cd" * 32 + "\n", self.salt_file().read_text(encoding="ascii"))

    def fresh_salts(self):
        effects._salts.clear()
        effects._salt_warned.clear()

    def test_a_read_that_fails_after_opening_is_unreadable(self):
        self.write_salt("ab" * 32 + "\n")
        with mock.patch.object(effects.os, "read", side_effect=OSError(errno.EIO, "disco")):
            self.assertEqual(("unreadable", None), effects._inspect_salt(self.salt_file()))
        self.assertEqual(("ok", bytes.fromhex("ab" * 32)), effects._inspect_salt(self.salt_file()))

    @unittest.skipUnless(NOT_ROOT, "permissões POSIX, sem root")
    def test_a_malformed_salt_in_a_read_only_home_warns_once_and_stays_on_disk(self):
        self.write_salt("nao-e-sal\n")
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        shown = target_of("texto livre do cliente")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual(["malformed"], self.warnings())  # um aviso só, mesmo com o conserto negado
        self.assertEqual("nao-e-sal\n", self.salt_file().read_text(encoding="ascii"))
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))


class SaltReadRetryTests(unittest.TestCase):
    """A salt still being written (short hex) is read again: exactly `tries` reads, then malformed."""

    def inspect(self, answers, tries):
        reads = iter(answers)
        with (
            mock.patch.object(effects, "_read_salt_once", side_effect=lambda _path: next(reads)) as read,
            mock.patch.object(effects.time, "sleep") as slept,
        ):
            result = effects._inspect_salt(Path("sal"), tries=tries)  # pylint: disable=protected-access
        return result, read.call_count, slept.call_count

    def test_a_salt_that_stays_partial_is_read_exactly_tries_times(self):
        for tries in (1, 2, 3, 25):
            with self.subTest(tries=tries):
                result, reads, sleeps = self.inspect([(None, "ab")] * (tries + 5), tries)
                self.assertEqual((("malformed", None), tries, tries - 1), (result, reads, sleeps))

    def test_a_salt_completed_on_the_last_read_is_used(self):
        salt = "c" * 64
        result, reads, sleeps = self.inspect([(None, "")] * 2 + [(None, salt)], 3)
        self.assertEqual((("ok", bytes.fromhex(salt)), 3, 2), (result, reads, sleeps))

    def test_text_that_is_not_hex_is_malformed_at_once(self):
        result, reads, sleeps = self.inspect([(None, "não é hex")], 25)
        self.assertEqual((("malformed", None), 1, 0), (result, reads, sleeps))


class NoHomeSaltTests(SaltCase):
    def test_without_a_home_folder_doctor_says_unreadable(self):
        with mock.patch.object(effects._paths, "gb_home", side_effect=RuntimeError("sem HOME")):
            self.assertEqual("unreadable", effects.salt_state())

    def test_without_a_home_folder_the_process_uses_its_own_salt_and_warns_once(self):
        with mock.patch.object(effects._paths, "gb_home", side_effect=RuntimeError("sem HOME")):
            first = target_of("texto livre do cliente")
            second = target_of("texto livre do cliente")
        self.assertRegex(first, f"^{HASH}$")
        self.assertEqual(first, second)
        self.assertEqual(["readonly"], self.warnings())
        self.assertFalse(self.home.exists())

    def test_a_home_with_no_existing_folder_up_to_the_root_is_readonly(self):
        # Um `$GB_HOME` num disco que não existe (Windows `Z:\`): nada até a raiz para criar o sal.
        with mock.patch.object(effects.os, "lstat", side_effect=FileNotFoundError(errno.ENOENT, "sem disco")):
            self.assertEqual("readonly", effects.salt_state())

    def test_a_parent_that_cannot_be_inspected_is_readonly(self):
        real_lstat = os.lstat
        blocked = self.home.parent

        def lstat(path, *args, **kwargs):
            path = Path(path)
            if path in (self.salt_file(), self.home):
                raise FileNotFoundError(errno.ENOENT, "ainda não existe")
            if path == blocked:
                raise PermissionError(errno.EACCES, "sem permissão")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(effects.os, "lstat", side_effect=lstat):
            self.assertEqual("readonly", effects.salt_state())
        self.assertFalse(self.home.exists())


class TargetRedactionEdgeTests(SaltCase):
    def test_a_path_under_an_explicit_runtime_dir_is_relative_to_it(self):
        runtime_dir = self.tmp / "runtime-cliente"
        target = runtime_dir / "pasta-cliente" / "estado.json"
        outside = target_of(target)
        self.assertRegex(outside, f"^{HASH}(\\.json)?$")  # sem a variável, só o hash do caminho inteiro
        with mock.patch.dict(os.environ, {"GB_RUNTIME_DIR": str(runtime_dir)}):
            shown = target_of(target)
        self.assertRegex(shown, f"^\\$GB_RUNTIME_DIR/{HASH}/{HASH}(\\.json)?$")
        self.assertNotIn("cliente", shown)

    def test_a_shell_text_with_an_unclosed_quote_keeps_only_its_first_word(self):
        shown = target_of([effects.ShellText('ffmpeg -i "/srv/cliente/segredo.mov')])
        self.assertEqual("ffmpeg", shown)
        self.assertEqual("ffmpeg", target_of((effects.ShellText("C:/tools/ffmpeg.EXE 'sem fim"),)))

    def test_a_command_given_as_bytes_is_never_spelled_out(self):
        self.assertEqual("-", effects.redact_target((b"/srv/cliente/bin/segredo",)))
        with effects.op("proc.run", [b"/srv/cliente/bin/segredo", b"--token"]):
            pass
        row = self.recorder.one()
        self.assertEqual(("proc.run", "-", "ok"), (row["operation"], row["target"], row["outcome"]))
        self.assertNotIn("cliente", repr(self.recorder.calls))

    def test_an_empty_command_logs_the_dash(self):
        self.assertEqual("-", effects.redact_target([""]))
        self.assertEqual("-", effects.redact_target([]))


if __name__ == "__main__":
    unittest.main()
