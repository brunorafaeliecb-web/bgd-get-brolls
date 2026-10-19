"""`getbrolls.effects`: what the log salt does when its repair fails.

A failure that is passing (a Windows sharing or lock violation, a file caught mid-rename, a repair lost to
another process) is read again until the file holds the salt that persisted: no process that took part in a
repair ends with one of its own. A salt of the process is for a link, a folder that takes no file, a file that
cannot be read, a file that never appears, or a wait that outlasts the budget, and each says so, with the
failure that ended it (`last=`, `errno=`).
"""

import contextlib
import errno
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
import _paths as _test_paths  # noqa: F401  (efeito de import: põe scripts/ no sys.path)  # pylint: disable=unused-import
from test_effects import target_of
from test_effects_salt_edges import HASH, NOT_ROOT, SaltCase

from getbrolls import _salt_read, effects

# pylint: disable=protected-access


def windows_error(winerror, number=errno.EACCES):
    """A `PermissionError` the way Windows raises it (POSIX has no `winerror`, so it is set by hand)."""
    error = PermissionError(number, "O arquivo está em uso por outro processo")
    error.winerror = winerror  # pyright: ignore[reportAttributeAccessIssue]  # POSIX has no such attribute
    return error


class SaltFailureCase(SaltCase):
    def fresh_salts(self):
        effects._salts.clear()
        effects._salt_warned.clear()

    def persisted(self):
        text = self.salt_file().read_text(encoding="ascii").strip()
        self.assertRegex(text, r"^[0-9a-f]{64}$")
        return bytes.fromhex(text)

    def lines(self):
        """`(reason, other fields)` of every `log_salt_unavailable` line."""
        return [
            (fields["reason"], {key: value for key, value in fields.items() if key != "reason"})
            for _level, name, fields in self.recorder.calls
            if name == "log_salt_unavailable"
        ]

    def replace_failing(self, times, winerror=32):
        """`Path.replace` that raises a sharing violation for the first `times` calls (of any thread)."""
        real_replace, left, guard = Path.replace, [times], threading.Lock()

        def flaky(path, target):
            with guard:
                fail = left[0] > 0
                left[0] -= 1
            if fail:
                raise windows_error(winerror)
            return real_replace(path, target)

        return mock.patch.object(Path, "replace", flaky)

    def same_hash_after_a_fresh_start(self, shown):
        self.persisted()
        self.fresh_salts()
        self.assertEqual(shown, target_of("Cliente Acme"))


class PassingFailureTests(SaltFailureCase):
    """Every repairer ends with the salt that persisted."""

    def test_a_loser_whose_set_aside_exhausts_the_sharing_retries_adopts_the_persisted_salt(self):
        self.write_salt("zz-not-hex\n")
        with self.replace_failing(effects._SHARING_TRIES + 2), mock.patch.object(effects.time, "sleep"):
            loser = target_of("Cliente Acme")
        self.assertEqual(["malformed"], self.warnings())  # the repair is not "unavailable": nothing more is said
        self.same_hash_after_a_fresh_start(loser)

    def test_many_concurrent_repairers_end_with_one_salt_when_the_set_aside_keeps_failing(self):
        self.write_salt("zz-not-hex\n")
        results = []

        def repair():
            results.append(effects._settle_salt(self.salt_file()))

        with self.replace_failing(effects._SHARING_TRIES * 3), mock.patch.object(effects.time, "sleep"):
            workers = [threading.Thread(target=repair) for _ in range(6)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual({self.persisted()}, {found for found, _reason, _last in results})
        self.assertEqual([("ok", effects._NOTHING)] * 6, [(reason, last) for _found, reason, last in results])

    def test_a_hardlink_denied_by_a_sharing_violation_is_read_again_not_answered_in_place(self):
        real_link, denied = Path.hardlink_to, [1]

        def flaky(path, target):
            if denied[0]:
                denied[0] -= 1
                raise windows_error(32)
            return real_link(path, target)

        with (
            mock.patch.object(Path, "hardlink_to", flaky),
            mock.patch.object(effects, "_create_in_place", wraps=effects._create_in_place) as in_place,
            mock.patch.object(effects.time, "sleep"),
        ):
            shown = target_of("Cliente Acme")
        self.assertEqual(0, in_place.call_count)
        self.assertEqual([], self.warnings())
        self.same_hash_after_a_fresh_start(shown)
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))

    def test_a_hardlink_refused_with_access_denied_falls_back_to_creating_in_place(self):
        with (
            mock.patch.object(Path, "hardlink_to", side_effect=windows_error(5)),
            mock.patch.object(effects, "_create_in_place", wraps=effects._create_in_place) as in_place,
        ):
            shown = target_of("Cliente Acme")
        self.assertEqual(1, in_place.call_count)
        self.assertEqual([], self.warnings())
        self.same_hash_after_a_fresh_start(shown)
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))

    def test_a_first_read_that_is_unreadable_once_is_read_again(self):
        self.write_salt("ef" * 32 + "\n")
        real_inspect, calls = effects._inspect_salt, [0]

        def mid_rename(path, *args, **kwargs):
            calls[0] += 1
            return ("unreadable", None) if calls[0] == 1 else real_inspect(path, *args, **kwargs)

        with mock.patch.object(effects, "_inspect_salt", mid_rename), mock.patch.object(effects.time, "sleep"):
            shown = target_of("Cliente Acme")
        self.assertEqual([], self.warnings())
        self.same_hash_after_a_fresh_start(shown)
        self.assertEqual(bytes.fromhex("ef" * 32), self.persisted())  # the salt on disk was never replaced

    def test_a_file_that_vanishes_while_it_is_looked_at_again_is_read_again_not_given_up(self):
        self.write_salt("ef" * 32 + "\n")
        real_inspect, real_open, calls = effects._inspect_salt, _salt_read.open_salt, {"inspect": 0, "open": 0}

        def unreadable_once(path, *args, **kwargs):
            calls["inspect"] += 1
            return ("unreadable", None) if calls["inspect"] == 1 else real_inspect(path, *args, **kwargs)

        def vanishing_once(path):
            calls["open"] += 1
            if calls["open"] == 1:
                raise FileNotFoundError(errno.ENOENT, "the winner just moved it aside")
            return real_open(path)

        with (
            mock.patch.object(effects, "_inspect_salt", unreadable_once),
            mock.patch.object(_salt_read, "open_salt", vanishing_once),
            mock.patch.object(effects.time, "sleep"),
        ):
            shown = target_of("Cliente Acme")
        self.assertEqual([], self.warnings())
        self.same_hash_after_a_fresh_start(shown)

    def test_a_set_aside_leaves_a_salt_that_is_good_by_now_where_it_is(self):
        good = "ab" * 32
        self.write_salt(good + "\n")  # another repairer won between the check and the move
        effects._set_aside(self.salt_file())
        self.assertEqual(good + "\n", self.salt_file().read_text(encoding="ascii"))

    def test_a_missed_lock_and_a_winner_between_the_attempts_still_ends_with_one_salt(self):
        """The loser missed the lock and its first rename hits a sharing violation; the winner installs the good
        salt while it waits. The retry must look at the file again, or it moves the winner's salt aside."""
        self.write_salt("zz-not-hex\n")
        winner = "ab" * 32
        real_replace, attempts = Path.replace, []

        def winner_installs_between_attempts(path, target):
            attempts.append(path)
            if len(attempts) == 1:
                self.salt_file().write_text(winner + "\n", encoding="ascii")
                raise windows_error(32)
            return real_replace(path, target)

        with (
            mock.patch.object(effects.logs, "folder_lock", return_value=contextlib.nullcontext(False)),
            mock.patch.object(Path, "replace", winner_installs_between_attempts),
            mock.patch.object(effects.time, "sleep"),
        ):
            loser = effects._settle_salt(self.salt_file())
        self.assertEqual((bytes.fromhex(winner), "ok", effects._NOTHING), loser)
        self.assertEqual(1, len(attempts))  # the second attempt never came: the file was well formed by then
        self.assertEqual(winner + "\n", self.salt_file().read_text(encoding="ascii"))
        self.assertEqual(["malformed"], self.warnings())  # an adopted salt is not a private one: no other line
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))

    def test_a_set_aside_whose_file_was_moved_by_another_repairer_is_done(self):
        self.write_salt("zz-not-hex\n")
        with mock.patch.object(Path, "replace", side_effect=FileNotFoundError(errno.ENOENT, "gone")) as replaced:
            effects._set_aside(self.salt_file())
        self.assertEqual(1, replaced.call_count)

    def test_a_set_aside_refused_for_good_is_not_tried_again(self):
        self.write_salt("zz-not-hex\n")
        with (
            mock.patch.object(Path, "replace", side_effect=PermissionError(errno.EACCES, "no")) as replaced,
            mock.patch.object(effects.time, "sleep") as slept,
            self.assertRaises(PermissionError),
        ):
            effects._set_aside(self.salt_file())
        self.assertEqual((1, 0), (replaced.call_count, slept.call_count))

    def test_a_lock_that_was_missed_still_ends_with_one_salt(self):
        self.write_salt("zz-not-hex\n")
        with mock.patch.object(effects.logs, "folder_lock", return_value=contextlib.nullcontext(False)):
            first = effects._settle_salt(self.salt_file())
            second = effects._settle_salt(self.salt_file())  # a second repairer, late and without the lock
        self.assertEqual(first, second)
        self.assertEqual((self.persisted(), "ok", effects._NOTHING), first)

    def test_a_repair_that_leaves_an_unreadable_file_reads_again(self):
        self.write_salt("zz-not-hex\n")
        real_create, calls = effects._create_salt, [0]

        def lost(path, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return "unreadable", None  # the winner's file is mid-rename when this one rereads it
            return real_create(path, **kwargs)

        with mock.patch.object(effects, "_create_salt", lost), mock.patch.object(effects.time, "sleep") as slept:
            shown = target_of("Cliente Acme")
        self.assertEqual(1, slept.call_count)
        self.assertEqual(["malformed"], self.warnings())
        self.same_hash_after_a_fresh_start(shown)

    def test_a_temporary_file_that_is_busy_is_removed_all_the_same(self):
        real_unlink, busy = Path.unlink, [2]

        def flaky(path, *args, **kwargs):
            if path.name.endswith(".tmp") and busy[0]:
                busy[0] -= 1
                raise windows_error(32)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky), mock.patch.object(effects.time, "sleep"):
            target_of("Cliente Acme")
        self.assertEqual([effects.SALT_FILE], sorted(p.name for p in self.home.iterdir()))

    def test_a_repair_sweeps_the_stale_temporary_files_and_only_those(self):
        self.write_salt("zz-not-hex\n")
        stale, fresh, other = (self.home / name for name in (".log-salt.1.a.tmp", ".log-salt.2.b.tmp", ".log-salt.bak"))
        for leftover in (stale, fresh, other):
            leftover.write_text("x", encoding="ascii")
        os.utime(stale, (1, 1))
        os.utime(other, (1, 1))
        target_of("Cliente Acme")
        self.assertEqual(
            [".log-salt", ".log-salt.2.b.tmp", ".log-salt.bak"], sorted(p.name for p in self.home.iterdir())
        )


class GivingUpTests(SaltFailureCase):
    """A salt of the process, said with the true reason and what failed."""

    def test_an_exhausted_budget_uses_a_salt_of_the_process_and_says_busy(self):
        self.write_salt("zz-not-hex\n")
        with (
            self.replace_failing(10_000),
            mock.patch.object(effects, "_SALT_SETTLE_S", 0.05),
            mock.patch.object(_salt_read, "SHARING_WAIT_S", 0.001),
        ):
            shown = target_of("Cliente Acme")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual([("malformed", {}), ("busy", {"last": "busy", "errno": errno.EACCES})], self.lines())
        self.assertEqual("zz-not-hex\n", self.salt_file().read_text(encoding="ascii"))  # nothing was persisted

    def test_the_budget_is_checked_before_a_pass_and_the_lock_wait_is_what_is_left(self):
        self.write_salt("zz-not-hex\n")
        with mock.patch.object(effects, "_SALT_SETTLE_S", 0):
            self.assertEqual((None, "busy", effects._NOTHING), effects._settle_salt(self.salt_file()))
        waits = []

        def asked(_path, *, wait_s, **_kwargs):
            waits.append(wait_s)
            return "ok", b"x"

        with mock.patch.object(effects, "_SALT_SETTLE_S", 0.5), mock.patch.object(effects, "_create_salt", asked):
            effects._settle_salt(self.salt_file())
        self.assertEqual(1, len(waits))
        self.assertLessEqual(waits[0], 0.5)

    def test_access_denied_that_never_goes_away_is_readonly_after_a_handful_of_passes(self):
        with (
            mock.patch.object(effects, "_new_salt_file", side_effect=windows_error(5)),
            mock.patch.object(effects.time, "sleep") as slept,
        ):
            shown = target_of("Cliente Acme")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual([("readonly", {"last": "create", "errno": errno.EACCES})], self.lines())
        self.assertEqual(effects._SHARING_TRIES - 1, slept.call_count)  # never the whole budget
        self.assertFalse(self.salt_file().exists())

    def test_access_denied_on_the_open_of_the_salt_is_unreadable_after_the_open_retries(self):
        self.write_salt("ab" * 32 + "\n")
        with (
            mock.patch.object(_salt_read, "open_salt", side_effect=windows_error(5)),
            mock.patch.object(effects.time, "sleep") as slept,
        ):
            shown = target_of("Cliente Acme")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual([("unreadable", {"last": "unreadable", "errno": errno.EACCES})], self.lines())
        self.assertEqual(effects._SHARING_TRIES - 1, slept.call_count)  # the open's own handful, nothing more

    def test_a_refusal_that_is_no_sharing_violation_is_not_waited_for(self):
        for number, name in ((errno.EACCES, "EACCES"), (errno.ENOSPC, "ENOSPC"), (errno.EROFS, "EROFS")):
            with self.subTest(error=name):
                self.fresh_salts()
                self.recorder.calls.clear()
                with (
                    mock.patch.object(effects, "_new_salt_file", side_effect=OSError(number, name)),
                    mock.patch.object(effects.time, "sleep") as slept,
                ):
                    target_of("Cliente Acme")
                self.assertEqual([("readonly", {"last": "create", "errno": number})], self.lines())
                self.assertEqual(0, slept.call_count)

    def test_a_failure_that_repeats_gives_up_as_what_it_is(self):
        self.write_salt("zz-not-hex\n")
        with (
            mock.patch.object(effects, "_create_salt", return_value=("unreadable", None)),
            mock.patch.object(effects.time, "sleep") as slept,
        ):
            target_of("Cliente Acme")
        self.assertEqual([("malformed", {}), ("unreadable", {"last": "unreadable"})], self.lines())
        self.assertEqual(effects._SHARING_TRIES - 1, slept.call_count)

    def test_a_repair_that_keeps_coming_back_malformed_gives_up_as_busy(self):
        self.write_salt("zz-not-hex\n")
        with (
            mock.patch.object(effects, "_create_salt", return_value=("malformed", None)),
            mock.patch.object(effects.time, "sleep"),
        ):
            target_of("Cliente Acme")
        self.assertEqual([("malformed", {}), ("busy", {"last": "malformed"})], self.lines())

    def test_a_file_that_never_appears_is_a_salt_of_the_process_and_says_nolink(self):
        with mock.patch.object(effects, "_create_salt", return_value=("missing", None)):
            shown = target_of("Cliente Acme")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual(["nolink"], self.warnings())

    def test_a_link_at_the_salt_path_is_not_waited_for_and_nothing_is_moved(self):
        self.home.mkdir()
        elsewhere = self.tmp / "outro-sal"
        elsewhere.write_text("ab" * 32 + "\n", encoding="ascii")
        self.salt_file().symlink_to(elsewhere)
        with (
            mock.patch.object(Path, "replace", side_effect=AssertionError("replace")) as replaced,
            mock.patch.object(effects.time, "sleep") as slept,
        ):
            shown = target_of("Cliente Acme")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual(["link"], self.warnings())
        self.assertEqual((0, 0), (replaced.call_count, slept.call_count))
        self.assertTrue(self.salt_file().is_symlink())

    def test_a_file_that_became_a_link_while_it_was_looked_at_again_is_a_link(self):
        self.write_salt("ab" * 32 + "\n")
        with (
            mock.patch.object(effects, "_inspect_salt", return_value=("unreadable", None)),
            mock.patch.object(_salt_read, "open_salt", side_effect=OSError(errno.ELOOP, "link")),
        ):
            self.assertEqual((None, "link", effects._NOTHING), effects._settle_salt(self.salt_file()))

    @unittest.skipUnless(NOT_ROOT, "permissões POSIX, sem root")
    def test_a_salt_that_is_denied_for_good_is_unreadable_and_not_waited_for(self):
        self.write_salt("ab" * 32 + "\n")
        self.salt_file().chmod(0)
        self.addCleanup(self.salt_file().chmod, 0o600)
        with mock.patch.object(effects.time, "sleep") as slept:
            shown = target_of("Cliente Acme")
        self.assertRegex(shown, f"^{HASH}$")
        self.assertEqual([("unreadable", {"last": "unreadable", "errno": errno.EACCES})], self.lines())
        self.assertEqual(0, slept.call_count)

    @unittest.skipUnless(NOT_ROOT, "permissões POSIX, sem root")
    def test_a_malformed_salt_in_a_read_only_folder_says_both(self):
        self.write_salt("zz-not-hex\n")
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        target_of("Cliente Acme")
        reasons = [reason for reason, _fields in self.lines()]
        self.assertEqual(["malformed", "readonly"], reasons)

    def test_a_reason_is_said_once_and_a_new_one_is_said_too(self):
        effects._warn_salt("home", "busy")
        effects._warn_salt("home", "busy", ("busy", 32))
        effects._warn_salt("home", "readonly", ("create", None))
        self.assertEqual([("busy", {}), ("readonly", {"last": "create"})], self.lines())


class DoctorProbeTests(SaltFailureCase):
    """`doctor` sees a folder that takes no file even where `os.access` cannot (ACLs on Windows)."""

    def leftovers(self):
        return sorted(p.name for p in self.home.glob(".getbrolls-probe.*"))

    def test_a_folder_that_refuses_the_probe_is_readonly_and_a_good_one_is_left_clean(self):
        self.home.mkdir()
        self.assertEqual("missing", effects.salt_state())
        self.assertEqual([], sorted(self.home.iterdir()))
        real_open = os.open

        def refusing(path, flags, *args):
            if ".getbrolls-probe" in str(path):
                raise windows_error(5)
            return real_open(path, flags, *args)

        with mock.patch.object(effects.os, "open", side_effect=refusing):
            self.assertEqual("readonly", effects.salt_state())
        self.assertEqual([], sorted(self.home.iterdir()))

    def test_the_probe_file_is_removed_even_when_something_goes_wrong_after_it_was_created(self):
        self.home.mkdir()
        real_close = os.close
        interrupted = [True]

        def interrupt_once(fd):
            real_close(fd)
            if interrupted[0]:
                interrupted[0] = False
                raise KeyboardInterrupt

        with mock.patch.object(effects.os, "close", interrupt_once), self.assertRaises(KeyboardInterrupt):
            effects._accepts_a_file(self.home)
        self.assertEqual([], self.leftovers())

    def test_a_probe_file_that_is_busy_for_a_moment_is_removed_all_the_same(self):
        self.home.mkdir()
        real_unlink, busy = Path.unlink, [2]

        def flaky(path, *args, **kwargs):
            if path.name.startswith(".getbrolls-probe") and busy[0]:
                busy[0] -= 1
                raise windows_error(32)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky), mock.patch.object(effects.time, "sleep"):
            self.assertTrue(effects._accepts_a_file(self.home))
        self.assertEqual([], self.leftovers())


class SaltReadModuleTests(unittest.TestCase):
    def test_which_errors_are_sharing_and_which_are_patient(self):
        self.assertEqual([True, True, True, False], [_salt_read.sharing(windows_error(n)) for n in (5, 32, 33, 87)])
        self.assertEqual([False, True, True, False], [_salt_read.patient(windows_error(n)) for n in (5, 32, 33, 87)])
        self.assertFalse(_salt_read.sharing(PermissionError(errno.EACCES, "no winerror")))
        self.assertFalse(_salt_read.patient(OSError(errno.EIO, "not even a PermissionError")))

    def test_a_retry_covers_access_denied_and_lock_violations_too(self):
        for number in (5, 32, 33):
            with self.subTest(winerror=number):
                failures = [windows_error(number), windows_error(number)]

                def operation(pending=failures):  # pylint: disable=dangerous-default-value
                    if pending:
                        raise pending.pop()
                    return "done"

                with mock.patch.object(_salt_read.time, "sleep"):
                    self.assertEqual("done", _salt_read.while_shared(operation))

    def test_why_a_salt_is_unreadable(self):
        cases = {
            "busy": (windows_error(32), (("busy", errno.EACCES))),
            "denied": (windows_error(5), ("denied", errno.EACCES)),
            "link": (OSError(errno.ELOOP, "loop"), ("link", errno.ELOOP)),
            "vanished": (FileNotFoundError(errno.ENOENT, "gone"), ("again", errno.ENOENT)),
            "descriptors": (OSError(errno.EMFILE, "too many"), ("again", errno.EMFILE)),
        }
        for name, (error, expected) in cases.items():
            with self.subTest(case=name), mock.patch.object(_salt_read, "open_salt", side_effect=error):
                self.assertEqual(expected, _salt_read.why_unreadable(Path("sal")))
        with mock.patch.object(_salt_read, "open_salt", return_value=None):
            self.assertEqual(("link", None), _salt_read.why_unreadable(Path("sal")))
        with (
            mock.patch.object(_salt_read, "open_salt", return_value=99),
            mock.patch.object(_salt_read.os, "close") as closed,
        ):
            self.assertEqual(("again", None), _salt_read.why_unreadable(Path("sal")))
        closed.assert_called_once_with(99)


if __name__ == "__main__":
    unittest.main()
