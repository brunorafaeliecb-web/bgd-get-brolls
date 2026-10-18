"""Leitor único de arquivo de fora (`getbrolls.reader`): link, teto, permissão, `--project-dir`, cópia e texto."""

import hashlib
import os
import pickle
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
from _fake_secrets import joined
from _paths import ROOT  # noqa: F401  (efeito de import: insere scripts/ em sys.path)  # pylint: disable=unused-import

from getbrolls import reader, runtime
from getbrolls.errors import Refusal, UsageError

POSIX = os.name != "nt"
CAN_CHMOD = POSIX and os.geteuid() != 0
FD_DIR = Path("/dev/fd")
FIRST_PASS = 2  # `lstat` da raiz e de `project` na descida; o terceiro já é a reconferência


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gb-reader-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.outside = Path(tempfile.mkdtemp(prefix="gb-reader-out-")).resolve()
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)
        (self.outside / "segredo.txt").write_text("de fora", encoding="utf-8")

    def write(self, rel, data=b"conteudo"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def refused(self, reason, call, *args, **kwargs):
        with self.assertRaises(reader.Refused) as caught:
            call(*args, **kwargs)
        self.assertEqual(reason, caught.exception.reason)
        return caught.exception


class PathRulesTests(ReaderCase):
    def test_a_relative_posix_path_splits_and_the_root_is_empty(self):
        self.assertEqual(("a", "b c.json"), reader.parts_of("a/b c.json"))
        self.assertEqual((), reader.parts_of(""))

    def test_every_hostile_spelling_is_bad_name(self):
        for rel in ("/etc/x", "a/../b", "a\\b", "C:x", "a//b", "a/./b", "x\x00", "e\u0301", "a.", "a ", "CON.txt",
                    "a\u202eb", "/".join(["p"] * 17), "x" * 256):  # fmt: skip
            with self.subTest(rel=rel):
                self.refused("bad_name", reader.parts_of, rel)
        self.refused("bad_name", reader.parts_of, b"a")


class OpenUnderTests(ReaderCase):
    def test_reads_the_bytes_up_to_the_ceiling(self):
        self.write("project/hyperframes.json", b"12345")
        self.assertEqual(b"12345", reader.open_under(self.root, "project/hyperframes.json", max_bytes=5))
        self.refused("too_big", reader.open_under, self.root, "project/hyperframes.json", max_bytes=4)

    def test_a_huge_file_is_refused_before_any_read(self):
        with (self.root / "grande.mp4").open("wb") as stream:
            stream.truncate(2 * 1024**3)  # 2 GB esparso: nenhum byte é lido
        with mock.patch.object(reader.os, "read", side_effect=AssertionError("leu")):
            self.refused("too_big", reader.open_under, self.root, "grande.mp4", max_bytes=reader.MAX_COMPONENT_BYTES)

    def test_a_file_that_grows_after_the_check_is_cut_at_the_ceiling(self):
        self.write("cresce.txt", b"x" * 10)
        real = reader.os.fstat

        def small(fd):
            info = real(fd)
            return os.stat_result((*info[:6], 1, *info[7:]))

        with mock.patch.object(reader.os, "fstat", side_effect=small):
            self.refused("too_big", reader.open_under, self.root, "cresce.txt", max_bytes=4)

    def test_a_name_it_cannot_even_look_at_is_unreadable(self):
        with (
            mock.patch.object(reader.os, "stat", side_effect=PermissionError),
            self.assertRaises(reader.UnreadableError),
        ):
            reader.open_under(self.root, "nada.txt", max_bytes=10)

    def test_missing_is_missing_never_refused(self):
        with self.assertRaises(reader.MissingError) as caught:
            reader.open_under(self.root, "project/index.html", max_bytes=10)
        self.assertEqual("INVALID_DATA", runtime.error_code_for(caught.exception))
        self.assertNotIsInstance(caught.exception, Refusal)

    @unittest.skipUnless(POSIX, "link simbólico sem privilégio")
    def test_a_link_in_any_piece_is_refused(self):
        (self.root / "project").mkdir()
        (self.root / "project" / "fora.txt").symlink_to(self.outside / "segredo.txt")
        (self.root / "atalho").symlink_to(self.outside)
        (self.root / "dentro").symlink_to(self.root / "project")
        self.refused("link", reader.open_under, self.root, "project/fora.txt", max_bytes=100)
        self.refused("link", reader.open_under, self.root, "atalho/segredo.txt", max_bytes=100)
        self.refused("link", reader.open_under, self.root, "dentro/fora.txt", max_bytes=100)

    def test_a_file_in_the_middle_is_not_dir_and_the_root_is_not_regular(self):
        self.write("project", b"arquivo")
        self.refused("not_dir", reader.open_under, self.root, "project/index.html", max_bytes=100)
        self.refused("not_regular", reader.open_under, self.root, "", max_bytes=100)

    @unittest.skipUnless(POSIX and hasattr(os, "mkfifo"), "FIFO só no POSIX")
    def test_a_fifo_never_blocks_and_is_not_regular(self):
        os.mkfifo(self.root / "cano")
        self.refused("not_regular", reader.open_under, self.root, "cano", max_bytes=100)

    @unittest.skipUnless(POSIX, "hardlink no POSIX")
    def test_a_hardlinked_text_is_refused_and_media_may_have_two_names(self):
        target = self.write("a.txt", b"dois nomes")
        os.link(target, self.root / "b.txt")
        self.refused("hardlinked", reader.open_under, self.root, "a.txt", max_bytes=100)
        self.assertEqual(b"dois nomes", reader.open_under(self.root, "a.txt", max_bytes=100, single_link=False))

    @unittest.skipUnless(CAN_CHMOD, "permissões POSIX, sem root")
    def test_a_folder_without_permission_is_unreadable_never_missing(self):
        self.write("project/index.html", b"x")
        (self.root / "project").chmod(0)
        self.addCleanup((self.root / "project").chmod, 0o700)
        with self.assertRaises(reader.UnreadableError) as caught:
            reader.open_under(self.root, "project/index.html", max_bytes=10)
        self.assertEqual("IO_ERROR", runtime.error_code_for(caught.exception))
        with self.assertRaises(reader.UnreadableError):
            reader.list_under(self.root, "project")

    @unittest.skipUnless(CAN_CHMOD, "permissões POSIX, sem root")
    def test_a_file_without_permission_is_unreadable(self):
        self.write("x.txt", b"x").chmod(0)
        with self.assertRaises(reader.UnreadableError):
            reader.open_under(self.root, "x.txt", max_bytes=10)

    def test_the_refusal_carries_its_reason_code_and_survives_pickle(self):
        refusal = reader.Refused("link", "project/x")
        self.assertIsInstance(refusal, Refusal)
        self.assertEqual(("UNSAFE_INPUT", "INVALID_DATA"), (refusal.reason_code, runtime.error_code_for(refusal)))
        again = pickle.loads(pickle.dumps(refusal))  # noqa: S301 - round trip do nosso próprio objeto
        self.assertEqual((str(refusal), "link", "project/x"), (str(again), again.reason, again.rel))
        self.assertNotIn(str(self.root), str(refusal))


class TextAndJsonTests(ReaderCase):
    def test_text_drops_the_bom_and_refuses_bad_utf8_and_nul(self):
        self.write("a.md", "\ufeffolá".encode())
        self.assertEqual("olá", reader.text_under(self.root, "a.md"))
        self.write("b.md", b"\xff\xfe")
        self.refused("not_text", reader.text_under, self.root, "b.md")
        self.write("c.md", b"a\x00b")
        self.refused("not_text", reader.text_under, self.root, "c.md")

    def test_json_refuses_repeats_nan_depth_and_non_objects(self):
        self.write("ok.json", b'{"a": [1, {"b": null}]}')
        self.assertEqual({"a": [1, {"b": None}]}, reader.json_under(self.root, "ok.json"))
        for name, raw in (
            ("dup.json", b'{"a": 1, "a": 2}'),
            ("nan.json", b'{"a": NaN}'),
            ("inf.json", b'{"a": -Infinity}'),
            ("list.json", b"[1]"),
            ("deep.json", b'{"a": ' + b"[" * 32 + b"]" * 32 + b"}"),
            ("broken.json", b'{"a": '),
        ):
            with self.subTest(name=name):
                self.write(name, raw)
                self.refused("bad_json", reader.json_under, self.root, name)


class ListUnderTests(ReaderCase):
    @unittest.skipUnless(POSIX, "link simbólico sem privilégio")
    def test_lists_in_name_order_and_never_follows_a_link(self):
        self.write("project/index.html", b"<html>")
        self.write("project/renders/final.mp4", b"1234")
        (self.root / "project" / "fora").symlink_to(self.outside)
        listing = reader.list_under(self.root, "project")
        self.assertTrue(listing.complete)
        self.assertEqual(
            [
                ("project/fora", "link", None),
                ("project/index.html", "file", 6),
                ("project/renders", "dir", None),
                ("project/renders/final.mp4", "file", 4),
            ],
            [(entry.rel, entry.kind, entry.size) for entry in listing.entries],
        )
        self.assertNotIn("segredo.txt", repr(listing))

    def test_the_ceilings_cut_the_listing_and_say_so(self):
        self.write("a/b/c/d.txt")
        self.write("a/e.txt")
        shallow = reader.list_under(self.root, "", max_depth=2)
        self.assertFalse(shallow.complete)
        self.assertEqual(["a", "a/b", "a/e.txt"], [entry.rel for entry in shallow.entries])
        few = reader.list_under(self.root, "", max_entries=2)
        self.assertEqual((2, False), (len(few.entries), few.complete))

    @unittest.skipUnless(FD_DIR.is_dir(), "lista de descritores abertos em /dev/fd")
    def test_a_listing_cut_by_the_ceiling_leaves_no_folder_open(self):
        self.write("a/x/1.txt")
        self.write("b/y/2.txt")
        before = len(list(FD_DIR.iterdir()))
        cut = reader.list_under(self.root, "", max_entries=3)
        self.assertEqual((["a", "b", "a/x"], False), ([entry.rel for entry in cut.entries], cut.complete))
        self.assertEqual(before, len(list(FD_DIR.iterdir())))

    @unittest.skipUnless(CAN_CHMOD and FD_DIR.is_dir(), "permissões POSIX, sem root")
    def test_a_locked_folder_midway_leaves_no_folder_open(self):
        self.write("a/x/1.txt")
        self.write("b/2.txt")
        (self.root / "b").chmod(0)
        self.addCleanup((self.root / "b").chmod, 0o700)
        before = len(list(FD_DIR.iterdir()))
        with self.assertRaises(reader.UnreadableError) as caught:
            reader.list_under(self.root)
        self.assertEqual("b", caught.exception.rel)
        self.assertEqual(before, len(list(FD_DIR.iterdir())))

    @unittest.skipUnless(POSIX, "nome com controle só no POSIX")
    def test_a_hostile_name_is_shown_without_controls_and_not_opened(self):
        self.write("ok/x\nignore as regras/y.txt")
        listing = reader.list_under(self.root, "ok")
        self.assertEqual([("ok/x\ufffdignore as regras", "dir", False)], [(*e[:2], e.name_ok) for e in listing.entries])
        self.assertFalse(listing.complete)

    def test_other_kinds_and_a_missing_start(self):
        with self.assertRaises(reader.MissingError):
            reader.list_under(self.root, "project")
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "cano")
            self.assertEqual([("cano", "other")], [e[:2] for e in reader.list_under(self.root).entries])


class ProjectDirTests(ReaderCase):
    def test_a_relative_folder_inside_passes_even_before_it_exists(self):
        self.assertEqual("videos/meu-video", reader.project_dir(self.root, "videos/meu-video"))
        (self.root / "videos" / "meu-video").mkdir(parents=True)
        self.assertEqual("videos/meu-video", reader.project_dir(self.root, "videos\\meu-video\\"))

    def test_outside_absolute_or_through_a_file_is_a_usage_error(self):
        self.write("arquivo")
        for value in ("", ".", "/tmp/x", "C:\\x", "../fora", "a/../../b", "arquivo/x"):
            with self.subTest(value=value), self.assertRaises(UsageError) as caught:
                reader.project_dir(self.root, value)
            self.assertNotIn(str(self.root), str(caught.exception))

    @unittest.skipUnless(POSIX, "link simbólico sem privilégio")
    def test_a_link_in_the_way_is_a_usage_error(self):
        (self.root / "videos").symlink_to(self.outside)
        with self.assertRaises(UsageError):
            reader.project_dir(self.root, "videos/x")


class WithoutDirFdTests(ReaderCase):
    """O caminho do Windows (sem `dir_fd`), exercitado aqui com `_BY_FD` desligado."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(reader, "_BY_FD", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_lists_and_checks_the_project_dir(self):
        self.write("project/renders/final.mp4", b"1234")
        self.assertEqual(b"1234", reader.open_under(self.root, "project/renders/final.mp4", max_bytes=10))
        self.assertEqual(
            ["project/renders", "project/renders/final.mp4"],
            [entry.rel for entry in reader.list_under(self.root, "project").entries],
        )
        self.assertEqual("project/renders", reader.project_dir(self.root, "project/renders"))
        self.assertEqual("project/novo", reader.project_dir(self.root, "project/novo"))
        with self.assertRaises(reader.MissingError):
            reader.open_under(self.root, "project/nada.txt", max_bytes=10)

    @unittest.skipUnless(POSIX, "link simbólico sem privilégio")
    def test_a_link_or_a_file_in_the_way_is_refused(self):
        self.write("project/index.html")
        (self.root / "project" / "fora.txt").symlink_to(self.outside / "segredo.txt")
        (self.root / "atalho").symlink_to(self.outside)
        self.refused("link", reader.open_under, self.root, "project/fora.txt", max_bytes=100)
        self.refused("link", reader.open_under, self.root, "atalho/segredo.txt", max_bytes=100)
        self.refused("not_dir", reader.open_under, self.root, "project/index.html/x", max_bytes=100)
        with self.assertRaises(UsageError):
            reader.project_dir(self.root, "atalho")

    def test_a_folder_swapped_during_the_read_is_changed(self):
        self.write("project/a.txt", b"x")
        real = reader._lstat
        calls = []

        def swapped(path, rel):
            calls.append(path)
            info = real(path, rel)
            if len(calls) > FIRST_PASS and path == self.root / "project":
                return os.stat_result((info.st_mode, info.st_ino + 1, *info[2:]))
            return info

        with mock.patch.object(reader, "_lstat", side_effect=swapped):
            self.refused("changed", reader.open_under, self.root, "project/a.txt", max_bytes=10)

    def test_a_leaf_swapped_between_lstat_and_open_is_changed(self):
        self.write("a.txt", b"x")
        real = reader._lstat

        def other(path, rel):
            info = real(path, rel)
            return os.stat_result((info.st_mode, info.st_ino + 1, *info[2:])) if path.name == "a.txt" else info

        with mock.patch.object(reader, "_lstat", side_effect=other):
            self.refused("changed", reader.open_under, self.root, "a.txt", max_bytes=10)

    @unittest.skipUnless(CAN_CHMOD, "permissões POSIX, sem root")
    def test_without_permission_is_unreadable(self):
        self.write("x.txt").chmod(0)
        with self.assertRaises(reader.UnreadableError):
            reader.open_under(self.root, "x.txt", max_bytes=10)
        (self.root / "p").mkdir()
        (self.root / "p").chmod(0)
        self.addCleanup((self.root / "p").chmod, 0o700)
        with self.assertRaises(reader.UnreadableError):
            reader.open_under(self.root, "p/x.txt", max_bytes=10)
        with self.assertRaises(reader.UnreadableError):
            reader.list_under(self.root, "p")


class CopyUnderTests(ReaderCase):
    def test_copies_with_the_sha_of_the_bytes_written_and_never_over_a_file(self):
        self.write("logo.png", b"\x89PNG-bytes")
        dest = self.outside / "copia.png"
        copied = reader.copy_under(self.root, "logo.png", dest, max_bytes=100)
        self.assertEqual((hashlib.sha256(b"\x89PNG-bytes").hexdigest(), 10), copied)
        self.assertEqual(b"\x89PNG-bytes", dest.read_bytes())
        if POSIX:
            self.assertEqual(0o644, dest.stat().st_mode & 0o777)
        with self.assertRaises(ValueError) as caught:
            reader.copy_under(self.root, "logo.png", dest, max_bytes=100)
        self.assertNotIsInstance(caught.exception, reader.Refused)

    def test_a_file_over_the_ceiling_leaves_no_copy(self):
        self.write("whoosh.wav", b"x" * 20)
        dest = self.outside / "whoosh.wav"
        self.refused("too_big", reader.copy_under, self.root, "whoosh.wav", dest, max_bytes=10)
        real = reader.os.fstat

        def small(fd):
            info = real(fd)
            return os.stat_result((*info[:6], 1, *info[7:]))

        with mock.patch.object(reader.os, "fstat", side_effect=small):
            self.refused("too_big", reader.copy_under, self.root, "whoosh.wav", dest, max_bytes=10)
        self.assertFalse(dest.exists())

    def test_a_destination_it_cannot_create_is_an_io_error(self):
        self.write("a.txt")
        with self.assertRaises(OSError) as caught:
            reader.copy_under(self.root, "a.txt", self.outside / "nao-existe" / "a.txt", max_bytes=100)
        self.assertEqual("IO_ERROR", runtime.error_code_for(caught.exception))

    @unittest.skipUnless(POSIX, "link simbólico sem privilégio")
    def test_a_link_is_never_copied(self):
        (self.root / "fora.txt").symlink_to(self.outside / "segredo.txt")
        dest = self.outside / "copia.txt"
        self.refused("link", reader.copy_under, self.root, "fora.txt", dest, max_bytes=100)
        self.assertFalse(dest.exists())

    def test_a_copy_without_dir_fd_and_a_folder_swapped_during_it(self):
        self.write("project/a.txt", b"x")
        with mock.patch.object(reader, "_BY_FD", False):
            self.assertEqual(
                1, reader.copy_under(self.root, "project/a.txt", self.outside / "c.txt", max_bytes=9).bytes
            )
            real = reader._lstat
            calls = []

            def swapped(path, rel):
                calls.append(path)
                info = real(path, rel)
                if len(calls) > FIRST_PASS and path == self.root / "project":
                    return os.stat_result((info.st_mode, info.st_ino + 1, *info[2:]))
                return info

            dest = self.outside / "a.txt"
            with mock.patch.object(reader, "_lstat", side_effect=swapped):
                self.refused("changed", reader.copy_under, self.root, "project/a.txt", dest, max_bytes=9)
        self.assertFalse(dest.exists())

    @staticmethod
    def vanishing_lstat(failure):
        real = reader._lstat
        calls = []

        def vanished(path, rel):
            calls.append(path)
            if len(calls) > FIRST_PASS:
                raise failure
            return real(path, rel)

        return vanished

    def test_a_folder_gone_or_locked_after_the_copy_leaves_no_dest(self):
        for failure in (reader.MissingError("project"), reader.UnreadableError("project", "PermissionError")):
            with self.subTest(failure=type(failure).__name__):
                self.write("project/a.txt", b"x")
                vanished = self.vanishing_lstat(failure)
                dest = self.outside / "gone.txt"
                with (
                    mock.patch.object(reader, "_BY_FD", False),
                    mock.patch.object(reader, "_lstat", side_effect=vanished),
                    self.assertRaises(type(failure)),
                ):
                    reader.copy_under(self.root, "project/a.txt", dest, max_bytes=9)
                self.assertFalse(dest.exists())


class UntrustedTextTests(unittest.TestCase):
    def test_one_clean_paragraph_with_its_origin(self):
        raw = "Título\n\u202eolleh\u202c\tfim\x07 e\u2028mais"
        self.assertEqual(
            {"origin": "hyperframes", "body": "Título olleh fim e mais"},
            reader.untrusted_text(raw, origin="hyperframes"),
        )

    def test_secrets_urls_and_absolute_paths_never_survive(self):
        body = reader.untrusted_text(
            joined("token=", "abc123def456ghi789 em https://exemplo.com/x?k=1 e /srv/cliente-exemplo/x.txt"),
            origin="hyperframes",
        )["body"]
        self.assertNotIn("abc123def456ghi789", body)
        self.assertNotIn("exemplo.com", body)
        self.assertNotIn("/srv/cliente-exemplo", body)

    def test_quoted_secrets_in_json_and_dict_text_never_survive(self):
        for text, secret in (
            ('{"password": "hunter2"}', "hunter2"),
            (joined('{"api_key": ', '"abcd1234efgh"}'), "abcd1234efgh"),
            ("{'secret': 'zzzz9999'}", "zzzz9999"),
        ):
            with self.subTest(text=text):
                self.assertNotIn(secret, reader.untrusted_text(text, origin="hyperframes")["body"])

    def test_the_body_fits_the_limit_in_utf8_bytes(self):
        body = reader.untrusted_text(("ação " * 3000).encode(), origin="hyperframes")["body"]
        self.assertLessEqual(len(body.encode("utf-8")), reader.UNTRUSTED_LIMIT)
        self.assertTrue(body.endswith("…"))
        self.assertEqual("\ufffd", reader.untrusted_text(b"\xff", origin="hyperframes")["body"])

    def test_the_origin_is_an_id(self):
        for origin in ("", "Hyper Frames", "../x", None):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                reader.untrusted_text("x", origin=cast("str", origin))


if __name__ == "__main__":
    unittest.main()
