"""Biblioteca global: recusas de entrada, índice inválido, biblioteca desligada e trava velha."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401  # pylint: disable=unused-import  # isola GB_HOME antes dos imports do getbrolls
import _logs
from test_library import LibraryBase, approved_candidate

from getbrolls import library
from getbrolls.errors import LockedError
from getbrolls.memory import remember


class LibraryInputRefusalTests(LibraryBase):
    def test_learn_query_refuses_empty_query_bad_outcome_and_empty_provider(self):
        cases = (
            (("  ", "youtube", "hit"), "--query"),
            (("foguete", "youtube", "talvez"), "hit ou miss"),
            (("foguete", "   ", "hit"), "--provider"),
        )
        for args, fragment in cases:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, fragment):
                library.learn_query(*args)
        self.assertFalse(library.index_path().exists())

    def test_learn_preference_refuses_text_shorter_than_five_characters(self):
        with self.assertRaisesRegex(ValueError, "5 caracteres"):
            library.learn_preference(" abc ")
        self.assertFalse(library.index_path().exists())

    def test_search_refuses_an_empty_term(self):
        with self.assertRaisesRegex(ValueError, "--search"):
            library.search("   ")


class LibraryOffTests(LibraryBase):
    def setUp(self):
        super().setUp()
        os.environ["GB_LIBRARY"] = "off"

    def test_learn_preference_when_off_writes_nothing_and_says_so(self):
        with self.assertLogs("getbrolls.library", level="INFO") as cm:
            answer = library.learn_preference("Prefiro plano aberto de foguete.", by="Bruno")
        self.assertFalse(answer["enabled"])
        self.assertIsNone(answer["entry"])
        self.assertEqual(library.OFF_NOTE, answer["note"])
        self.assertTrue(answer["rights_not_transferable"])
        self.assertFalse(library.index_path().exists())
        event = _logs.events(cm.output, "library")[0]
        self.assertEqual(("learn", "0", "True"), (event["action"], event["entries"], event["disabled"]))

    def test_learn_from_candidate_when_off_validates_first_then_writes_nothing(self):
        ledger, c = approved_candidate(self.project)
        # Sem decisão registrada a recusa vem antes de a biblioteca desligada importar.
        with self.assertRaisesRegex(ValueError, "remember"):
            library.learn_from_candidate(self.project, c["id"])
        remember(ledger, c, "approved", "Plano aberto exato da fala.", "Bruno")
        with self.assertLogs("getbrolls.library", level="INFO") as cm:
            answer = library.learn_from_candidate(self.project, c["id"])
        self.assertIsNone(answer["entry"])
        self.assertFalse(answer["enabled"])
        self.assertFalse(library.index_path().exists())
        self.assertEqual("True", _logs.events(cm.output, "library")[0]["disabled"])

    def test_hints_when_off_is_empty_and_logged_as_disabled(self):
        with self.assertLogs("getbrolls.library", level="INFO") as cm:
            self.assertEqual([], library.hints("foguete"))
        event = _logs.events(cm.output, "library")[0]
        self.assertEqual(("hints", "0", "True"), (event["action"], event["entries"], event["disabled"]))


class LibraryIndexValidityTests(LibraryBase):
    def _write_index(self, text):
        path = library.index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_an_index_that_is_not_an_object_is_refused_and_left_untouched(self):
        path = self._write_index("[1, 2]")
        with self.assertRaisesRegex(ValueError, "não é uma biblioteca da versão 1"):
            library.learn_query("foguete", "youtube", "hit")
        self.assertEqual("[1, 2]", path.read_text(encoding="utf-8"))

    def test_an_index_from_a_newer_version_is_refused_and_left_untouched(self):
        text = json.dumps({"schema_version": 9})
        path = self._write_index(text)
        with self.assertRaisesRegex(ValueError, "mais nova"):
            library.load_index()
        self.assertEqual(text, path.read_text(encoding="utf-8"))

    def test_a_second_distinct_asset_is_appended_instead_of_merged(self):
        ledger_a, a = approved_candidate(self.project, "https://www.youtube.com/watch?v=aaa")
        remember(ledger_a, a, "approved", "Plano aberto.", "Bruno")
        first = library.learn_from_candidate(self.project, a["id"])
        other = self.project.parent / "other"
        other.mkdir()
        ledger_b, b = approved_candidate(other, "https://www.youtube.com/watch?v=bbb")
        remember(ledger_b, b, "approved", "Outro plano.", "Bruno")
        second = library.learn_from_candidate(other, b["id"])
        self.assertNotEqual(first["entry"]["asset_id"], second["entry"]["asset_id"])
        data = json.loads(library.index_path().read_text(encoding="utf-8"))
        self.assertEqual(2, len(data["assets"]))
        self.assertEqual([1, 1], [len(x["used_by"]) for x in data["assets"]])


class LibraryLockTests(LibraryBase):
    def _lock(self):
        path = library.library_dir() / "index.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    def test_a_stale_lock_left_by_a_dead_process_is_removed_and_the_write_goes_on(self):
        path = self._lock()
        old = time.time() - library.STALE_LOCK_S - 30
        os.utime(path, (old, old))
        library.learn_query("foguete decolando", "youtube", "hit")
        self.assertFalse(path.exists())
        data = json.loads(library.index_path().read_text(encoding="utf-8"))
        self.assertEqual("foguete decolando", data["queries"][0]["query"])

    def test_a_lock_that_cannot_be_inspected_counts_as_fresh_and_ends_in_locked(self):
        path = self._lock()
        real_stat = Path.stat

        def stat(self, *args, **kwargs):
            if self.name == "index.lock":
                raise PermissionError("sem acesso")
            return real_stat(self, *args, **kwargs)

        with (
            patch.object(library, "LOCK_TIMEOUT_S", 0.05),
            patch.object(Path, "stat", stat),
            self.assertLogs("getbrolls.library", level="WARNING") as cm,
            self.assertRaisesRegex(LockedError, "travado"),
        ):
            library.learn_query("foguete decolando", "youtube", "hit")
        self.assertTrue(path.exists())
        self.assertFalse(library.index_path().exists())
        event = _logs.events(cm.output, "library_lock")[0]
        self.assertEqual("True", event["timeout"])


if __name__ == "__main__":
    unittest.main()
