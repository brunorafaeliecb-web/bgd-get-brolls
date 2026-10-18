"""`labels`: a pasta da máquina vira a marca dela em toda mensagem, e nunca sobra o caminho inteiro."""

import errno
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
from _paths import ROOT  # noqa: F401  (efeito de import: insere scripts/ em sys.path)  # pylint: disable=unused-import

from getbrolls import labels, runtime


class LabelsTests(unittest.TestCase):
    def assert_no_machine_path(self, text):
        real = os.path.realpath(self.tmp)
        for spelling in (str(self.tmp), real, real.removeprefix("/private")):
            self.assertNotIn(spelling, text)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gb-labels-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = self.tmp / "video"
        (self.project / "brolls").mkdir(parents=True)
        self.gbhome = self.tmp / "gbhome"
        self.gbhome.mkdir()
        self.temp = self.tmp / "temp"
        self.temp.mkdir()
        self.person = self.tmp / "pessoa"
        self.person.mkdir()
        env = {
            "GB_HOME": str(self.gbhome),
            "GB_CACHE_DIR": str(self.gbhome / "cache"),
            "HOME": str(self.person),
            "USERPROFILE": str(self.person),
        }
        patched = patch.dict(os.environ, env)
        patched.start()
        self.addCleanup(patched.stop)
        temp = patch.object(tempfile, "tempdir", str(self.temp))
        temp.start()
        self.addCleanup(temp.stop)

    def test_each_known_folder_gets_its_tag(self):
        client = self.tmp / "clientes" / "acme"
        client.mkdir(parents=True)
        cases = {
            self.project / "brolls" / "review.html": "<projeto>/brolls/review.html",
            client / "templates": "<cliente>/templates",
            self.gbhome / "clients.json": "$GB_HOME/clients.json",
            self.gbhome / "cache" / "x": "$GB_CACHE_DIR/x",
            self.temp / "gb-digest-1": "$TMPDIR/gb-digest-1",
            self.person / "Videos": "~/Videos",
        }
        for path, expected in cases.items():
            with self.subTest(path=path.name):
                self.assertEqual(expected, labels.shown(path, project=self.project, extra=[(client, "<cliente>")]))

    def test_outside_every_folder_only_the_name_is_left(self):
        elsewhere = Path(self.tmp.anchor) / "gb-disco-externo" / "clip.mp4"
        # The name of a file outside every known folder can be a client's: a hash and the extension.
        shown = labels.shown(elsewhere, project=self.project)
        self.assertRegex(shown, r"^<pasta fora do projeto>/#[0-9a-f]{8}\.mp4$")
        self.assertEqual(shown, labels.shown(elsewhere, project=self.project))
        self.assertEqual("<pasta fora do projeto>", labels.shown(Path(self.tmp.anchor)))

    def test_what_is_not_a_path_comes_back_as_it_came(self):
        self.assertEqual("None", labels.shown(None))
        self.assertEqual("", labels.shown(""))
        self.assertIsNone(labels._paths.label("a\0b", [(self.project, "<projeto>")]))
        self.assertIsNone(labels._paths.label(None, [(self.project, "<projeto>")]))

    def test_a_folder_that_neither_stats_nor_resolves_is_compared_as_written(self):
        missing = self.tmp / "ainda-nao-existe"
        with patch.object(Path, "resolve", side_effect=RuntimeError("symlink loop")):
            self.assertEqual("<projeto>/x", labels._paths.label(missing / "x", [(missing, "<projeto>")]))

    def test_the_project_of_the_running_command_is_the_default(self):
        with patch("getbrolls.logs.project_root", return_value=self.project):
            self.assertEqual("<projeto>/brolls", labels.shown(self.project / "brolls"))

    def test_free_text_gets_every_folder_replaced_at_path_boundaries(self):
        real = Path(os.path.realpath(self.project))
        message = f"falhou em {real}/brolls/x.json e {self.gbhome}/plugins.json; {real}2 fica"
        out = labels.text(message, project=self.project)
        # `video2` is another folder, not `<projeto>2`: outside every anchor, so hidden to the end of the line.
        expected = r"^falhou em <projeto>/brolls/x\.json e \$GB_HOME/plugins\.json; "
        self.assertRegex(out, expected + r"<pasta fora do projeto>/#[0-9a-f]{8}$")

    def test_free_text_with_the_unresolved_spelling_of_a_resolved_folder(self):
        # macOS: the command holds the project resolved (`/private/var/…`), the person typed `/var/…`.
        real = Path(os.path.realpath(self.project))
        if not str(real).startswith(("/private/var/", "/private/tmp/")):
            self.skipTest("só no macOS a pasta temporária passa por /private")
        typed = str(real).removeprefix("/private")
        self.assertEqual("em <projeto>/x", labels.text(f"em {typed}/x", project=real))

    def test_free_text_names_a_folder_inside_another_by_the_deeper_one(self):
        # `$GB_HOME` inside the project: replacing the project first would leave `<projeto>/gbh/a`.
        nested = self.project / "gbh"
        nested.mkdir()
        with patch.dict(os.environ, {"GB_HOME": str(nested)}):
            self.assertEqual(
                "$GB_HOME/a e <projeto>/b", labels.text(f"{nested}/a e {self.project}/b", project=self.project)
            )

    def test_a_name_that_goes_on_past_the_folder_is_another_folder(self):
        gbh = self.gbhome
        with patch.dict(os.environ, {"GB_HOME": str(gbh)}):
            for other in (f"{gbh} 2026/outro", f"{gbh}(cópia)/x", f"{gbh},v2/x", f"{gbh}:x/y"):
                with self.subTest(other=other):
                    # Another folder, not `$GB_HOME 2026/outro`: outside every anchor, so by its hash.
                    out = labels.text(f"falhou em {other}")
                    self.assertNotIn("$GB_HOME", out)
                    self.assertTrue(out.startswith("falhou em <pasta fora do projeto>/#"), out)
                    self.assert_no_machine_path(out)
            for text, expected in (
                (f"{gbh}: Permission denied", "$GB_HOME: Permission denied"),
                (f"em {gbh} e depois", "em $GB_HOME e depois"),
                (f"({gbh}), x", "($GB_HOME), x"),
                (f"'{gbh}'", "'$GB_HOME'"),
            ):
                with self.subTest(text=text):
                    self.assertEqual(expected, labels.text(text))

    def test_an_existing_sibling_whose_name_starts_with_the_folder_is_left_alone(self):
        sibling = Path(f"{self.project} 2")
        sibling.mkdir()
        # Not `<projeto> 2`: the sibling is another folder, outside every anchor here, so only its hash shows.
        for text in (f"em {sibling}", f"em {sibling}, e"):
            with self.subTest(text=text):
                out = labels.text(text, project=self.project)
                self.assertNotIn("<projeto>", out)
                self.assertTrue(out.startswith("em <pasta fora do projeto>/#"), out)
                self.assert_no_machine_path(out)
        self.assertEqual("em <projeto>, e", labels.text(f"em {self.project}, e", project=self.project))

    def test_a_name_in_nfd_matches_the_same_name_in_nfc(self):
        import unicodedata  # pylint: disable=import-outside-toplevel

        nfc = self.tmp / unicodedata.normalize("NFC", "Vídeos-novos")  # not created: compared as text
        nfd = str(self.tmp / unicodedata.normalize("NFD", "Vídeos-novos"))
        self.assertEqual("<projeto>/x", labels._paths.label(nfd + "/x", [(nfc, "<projeto>")]))
        self.assertEqual("em <projeto>/x", labels.text(f"em {nfd}/x", project=nfc))

    def test_reason_of_an_os_error_is_the_system_text_only(self):
        error = PermissionError(errno.EACCES, "Permission denied", str(self.project / "x"))
        self.assertEqual("Permission denied", labels.reason(error))
        self.assertEqual("OSError", labels.reason(OSError()))
        self.assertEqual("<projeto>/x ruim", labels.reason(ValueError(f"{self.project}/x ruim"), project=self.project))

    def test_os_message_gives_the_reason_in_portuguese_and_the_advice_of_the_errno(self):
        cases = {
            errno.EACCES: ("sem permissão", "Confira as permissões"),
            errno.EPERM: ("sem permissão", "Confira as permissões"),
            errno.ENOSPC: ("o disco está cheio", "Libere espaço em disco"),
            errno.EROFS: ("o disco está somente leitura", "Use um disco que aceite gravação"),
            errno.ENAMETOOLONG: ("o nome é longo demais", "Use um nome mais curto"),
            errno.EEXIST: ("o arquivo já existe", "Escolha um nome que ainda não exista"),
            errno.EIO: ("Input/output error", "Confira o caminho e as permissões"),
        }
        for code, (reason, advice) in cases.items():
            with self.subTest(errno=code):
                strerror = os.strerror(code)
                named = labels.os_message(OSError(code, strerror, str(self.project / "x")), project=self.project)
                self.assertEqual(f"Não consegui acessar <projeto>/x ({reason or strerror}). {advice} e repita.", named)
                bare = labels.os_message(OSError(code, strerror))
                self.assertEqual(f"Erro de E/S ({reason or strerror}). {advice} e repita.", bare)

    def test_os_message_reasons_are_the_ones_the_roteiro_and_the_export_use(self):
        from getbrolls import export, roteiro_commands  # pylint: disable=import-outside-toplevel

        self.assertIs(labels.OS_REASONS, roteiro_commands.labels.OS_REASONS)
        self.assertEqual("sem permissão", export._OS_REASONS[errno.EACCES])  # pylint: disable=protected-access


class LinkedSpellingTests(unittest.TestCase):
    """macOS: `/var` and `/tmp` are links to `/private/…`, so one folder has two spellings. The folder a command
    holds (`GB_HOME`, `--project`) and the folder a message names can be spelled either way, in every pairing."""

    def setUp(self):
        real = Path(tempfile.mkdtemp(prefix="gb-linked-")).resolve()
        self.addCleanup(shutil.rmtree, real, ignore_errors=True)
        self.real = real
        if not str(real).startswith("/private/"):
            self.skipTest("só no macOS a pasta temporária passa por /private")
        self.linked = Path(str(real).removeprefix("/private"))
        self.assertTrue(self.linked.samefile(real))
        (real / "gbhome").mkdir()
        (real / "video").mkdir()
        patched = patch.dict(os.environ, {"GB_HOME": str(real / "gbhome")})
        patched.start()
        self.addCleanup(patched.stop)

    def pairings(self, folder):
        spellings = {"resolved": self.real / folder, "linked": self.linked / folder}
        for held_name, held in spellings.items():
            for named_name, named in spellings.items():
                yield f"held {held_name}, named {named_name}", held, named

    def test_gb_home_is_recognised_in_both_spellings(self):
        for label, held, named in self.pairings("gbhome"):
            with self.subTest(label), patch.dict(os.environ, {"GB_HOME": str(held)}):
                self.assertEqual("$GB_HOME/x.json", labels.shown(named / "x.json"))
                self.assertEqual("falhou em $GB_HOME/x.json: motivo", labels.text(f"falhou em {named}/x.json: motivo"))
                for shown in (labels.text(f"{named}/x"), labels.for_log(f"em {named}/x"), labels.outside(f"{named}/x")):
                    self.assertNotIn("gb-linked-", shown)

    def test_a_home_folder_is_scrubbed_in_both_spellings(self):
        for label, held, named in self.pairings("gbhome"):
            with self.subTest(label), patch.object(runtime.Path, "home", return_value=held):
                self.assertEqual("falhou em ~/x.json", runtime.scrub_home(f"falhou em {named}/x.json"))

    def test_the_project_is_recognised_in_both_spellings(self):
        for label, held, named in self.pairings("video"):
            with self.subTest(label):
                self.assertEqual("<projeto>/a.mp4", labels.shown(named / "a.mp4", project=held))
                self.assertEqual("em <projeto>/a.mp4", labels.text(f"em {named}/a.mp4", project=held))
                self.assertNotIn("gb-linked-", labels.for_log(f"em {named}/a.mp4", project=held))
                self.assertNotIn(
                    "gb-linked-", labels.message_of(FileNotFoundError(2, "x", str(named / "a.mp4")), project=held)
                )


class OutsideTests(unittest.TestCase):
    """`labels.outside` esconde só caminho de verdade: dois nomes ou mais, a partir da raiz de um disco."""

    def test_what_is_not_a_path_stays_as_it_is(self):
        for kept in (
            "progresso 3 /4 concluído",
            "rota=/review/save status=200",
            "GET /v1/search?q=gato",
            "use bv*+ba/b para o formato",
            "1080p/720p e 10/10/2026",
            "veja https://exemplo.com/a/b",
            "o caminho relativo aroll/c01.mp4 e ./assets/sfx",
            "e/ou isto e um / sozinho",
            "/tmp e /Users sozinhos",
        ):
            with self.subTest(kept=kept):
                self.assertEqual(kept, labels.outside(kept))

    def test_a_path_from_the_root_of_a_disk_is_hidden(self):
        for text, kept in (
            ("erro em /Volumes/Externo/video.mp4.", ("erro em ", ".mp4.")),
            ("C:/Dados/x/a e mais", ("", "")),  # the rest of the line goes with the path
            ("C:\\Dados\\x\\b.mov", ("", ".mov")),
            ("\\\\servidor\\pasta\\c.mp4", ("", ".mp4")),
            ("veja file:///x/y.mp4 agora", ("veja ", "")),
            ("veja file://localhost/x/y agora", ("veja ", "")),
        ):
            with self.subTest(text=text):
                shown = labels.outside(text)
                self.assertRegex(shown, "^" + re.escape(kept[0]) + re.escape(labels.OUTSIDE) + "/#[0-9a-f]{8}")
                self.assertTrue(shown.endswith(kept[1]), shown)
                self.assertNotIn("Externo", shown)
                self.assertNotIn("servidor", shown)

    def test_a_folder_with_a_parenthesis_after_a_label_is_one_name(self):
        self.assertEqual("falhou em $GB_HOME(copia)/x", labels.outside("falhou em $GB_HOME(copia)/x"))

    def test_the_output_never_grows_past_the_cap(self):
        dense = (" /Users/a" * (labels.INPUT_MAX // 9))[: labels.INPUT_MAX]
        self.assertLessEqual(len(labels.outside(dense)), labels.INPUT_MAX)
        self.assertLessEqual(len(labels.for_log(dense)), labels.INPUT_MAX)


if __name__ == "__main__":
    unittest.main()
