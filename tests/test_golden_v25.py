"""Ouro da 2.5: a CLI em teste repete byte a byte a matriz gravada; contra a v2.5.0, só a lista fechada.

Regravar: `GB_UPDATE_GOLDEN=1 GB_GOLDEN_REASON="<motivo>"` nesta suíte (o motivo vai para
`REGRAVACOES.md`). Diferença nova contra a v2.5.0 só passa com item datado no `accepted.json`.
"""

import copy
import json
import os
import re
import shutil
import tempfile
import unittest
import unittest.mock
from datetime import UTC, datetime
from pathlib import Path

import _golden
import _golden_diff
import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
from _paths import CLI_ARGV, ROOT, WHEEL_MODE
from test_repository import LOCAL_PATH_PATTERN

UPDATE = os.environ.get("GB_UPDATE_GOLDEN") == "1"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
# UTC-12 e UTC+14: a qualquer hora do dia, ao menos um dos dois já está em outra data.
SHIFTED_ZONES = ("Etc/GMT+12", "Etc/GMT-14")
FRONTMATTER_DATE = re.compile(r"(?:created|updated): (\d{4}-\d\d-\d\d)")
RECORDING_DAY = re.compile(r"^- (\d{4}-\d\d-\d\d): ", re.MULTILINE)


def _expected(scenario, case):
    return (_golden.EXPECTED / scenario / f"{case}.json").read_text(encoding="utf-8")


@unittest.skipIf(WHEEL_MODE, "o ouro grava o checkout: no pacote instalado os caminhos sugeridos são outros")
@unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
class GoldenMatrixTests(unittest.TestCase):
    maxDiff = None

    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory(prefix="gb-ouro-") as raw:
            records = _golden.record_scenario(list(CLI_ARGV), ROOT, scenario, Path(raw))
        if UPDATE:
            reason = _golden.update_reason(os.environ)
            _golden.dump(records, _golden.EXPECTED / scenario)
            _golden.log_rewrite(f"{scenario}: {reason}", datetime.now(UTC).date().isoformat())
            return
        recorded = sorted(path.stem for path in (_golden.EXPECTED / scenario).glob("*.json"))
        self.assertEqual(recorded, sorted(record["case"] for record in records))
        for record in records:
            with self.subTest(case=record["case"]):
                self.assertEqual(_expected(scenario, record["case"]), _golden.serialize(record))

    def test_fluxo_matches_the_golden_byte_for_byte(self):
        self.run_scenario("fluxo")

    def test_layout0_from_v250_matches_the_golden_byte_for_byte(self):
        self.run_scenario("layout0")


@unittest.skipIf(WHEEL_MODE, "o ouro grava o checkout: no pacote instalado os caminhos sugeridos são outros")
@unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
@unittest.skipIf(os.name == "nt", "o TZ do Windows não entende Etc/GMT±N")
class GoldenOnAnotherDayTests(unittest.TestCase):
    """O ouro vale em qualquer dia: a CLI grava num fuso em que hoje é outra data e repete os mesmos bytes."""

    maxDiff = None

    def _other_day(self, raw):
        today = datetime.now(UTC).date().isoformat()
        shifted = []
        for zone in SHIFTED_ZONES:
            with unittest.mock.patch.object(_golden, "HERMETIC_TZ", zone):
                if _golden.run_date(Path(raw) / "sonda") != today:
                    shifted.append(zone)
        self.assertTrue(shifted, f"nenhum de {SHIFTED_ZONES} está em outro dia")
        return shifted[0]

    def test_a_run_dated_another_day_repeats_the_golden(self):
        with tempfile.TemporaryDirectory(prefix="gb-ouro-") as raw:
            zone = self._other_day(raw)
        # O fluxo até o `reject` (o último caso que grava frontmatter) e o layout 0 inteiro.
        for scenario, stop in (("fluxo", "reject"), ("layout0", None)):
            with (
                tempfile.TemporaryDirectory(prefix="gb-ouro-") as raw,
                unittest.mock.patch.object(_golden, "HERMETIC_TZ", zone),
            ):
                records = _golden.record_scenario(list(CLI_ARGV), ROOT, scenario, Path(raw), stop_after=stop)
            for record in records:
                with self.subTest(zone=zone, case=record["case"]):
                    self.assertEqual(_expected(scenario, record["case"]), _golden.serialize(record))


class GoldenAgainstV250Tests(unittest.TestCase):
    """Só dados: a diferença entre a gravação da v2.5.0 e o ouro é exatamente a lista fechada."""

    def setUp(self):
        self.accepted = json.loads(_golden.ACCEPTED.read_text(encoding="utf-8"))
        self.found = _golden_diff.diff(_golden_diff.load(_golden.V250), _golden_diff.load(_golden.EXPECTED))

    def test_every_difference_has_a_rule_and_every_rule_a_difference(self):
        taken, loose, stale = _golden_diff.classify(self.found, self.accepted)
        self.assertEqual([], [(item["case"], item["where"], item["kind"]) for item in loose])
        self.assertEqual([], stale)
        self.assertEqual([], _golden_diff.drifted(taken, self.accepted))

    def test_every_rule_is_on_the_closed_list_dated_and_in_the_changelog(self):
        changelog = _golden_diff.CHANGELOG.read_text(encoding="utf-8")
        self.assertEqual([], _golden_diff.problems(self.accepted, changelog))

    def test_no_item_waits_for_ratification(self):
        # A regravação nova entra com `"ratificado": null` e `pendente`, até o mantenedor dar o ok: a suíte
        # comum diz quais (no motivo do pulo); com GB_GOLDEN_REQUIRE_RATIFIED=1 (antes de publicar), falha.
        waiting = _golden_diff.pending(self.accepted)
        if waiting and os.environ.get("GB_GOLDEN_REQUIRE_RATIFIED") != "1":
            self.skipTest("aguardando ratificação: " + ", ".join(waiting))
        self.assertEqual([], waiting)

    def test_both_recordings_cover_the_same_cases(self):
        self.assertEqual(sorted(_golden_diff.load(_golden.V250)), sorted(_golden_diff.load(_golden.EXPECTED)))


class ClosedListGuardTests(unittest.TestCase):
    """A lista fechada leva só o que foi ratificado: outra mudança nos mesmos lugares fica de fora."""

    @classmethod
    def setUpClass(cls):
        cls.accepted = json.loads(_golden.ACCEPTED.read_text(encoding="utf-8"))
        cls.v250 = _golden_diff.load(_golden.V250)
        cls.expected = _golden_diff.load(_golden.EXPECTED)

    def assert_caught(self, mutate):
        records = copy.deepcopy(self.expected)
        mutate(records)
        taken, loose, stale = _golden_diff.classify(_golden_diff.diff(self.v250, records), self.accepted)
        drifted = _golden_diff.drifted(taken, self.accepted)
        self.assertTrue(loose or stale or drifted, "a mudança passou como se estivesse ratificada")
        return taken

    @staticmethod
    def edit_diary(record, rel, change):
        line = json.loads(record["appends"][rel][0])
        change(line)
        record["appends"][rel][0] = json.dumps(line, ensure_ascii=False)

    def test_a_diagnostics_line_counts_only_its_recovered_field(self):
        def mutate(records):
            def change(line):
                line["state_committed"] = False
                line["warnings"].append({"code": "NOVO"})

            self.edit_diary(records["fluxo/verify"], "work/proj/brolls/diagnostics.jsonl", change)

        self.assert_caught(mutate)

    def test_recovered_true_in_a_diagnostics_line_is_not_the_ratified_false(self):
        def mutate(records):
            self.edit_diary(
                records["fluxo/approve"],
                "work/proj/brolls/diagnostics.jsonl",
                lambda line: line.update(recovered=True),
            )

        self.assert_caught(mutate)

    def test_status_layout_is_version_0_inferred_on_a_2_5_project(self):
        def mutate(records):
            records["layout0/l0_status"]["stdout"]["json"]["layout"] = {
                "version": 1,
                "source": "marker",
                "problem": None,
            }

        self.assert_caught(mutate)

    def test_a_layout_key_outside_status_is_not_ratified(self):
        def mutate(records):
            records["fluxo/deliver"]["stdout"]["json"]["layout"] = {
                "version": 0,
                "source": "inferred",
                "problem": None,
            }

        self.assert_caught(mutate)

    def test_a_usage_error_still_exits_with_2(self):
        self.assert_caught(lambda records: records["fluxo/err_bad_int"].update(exit=1))

    def test_a_usage_error_gains_no_traceback_nor_repr(self):
        def mutate(records):
            records["fluxo/err_bad_int"]["stderr"]["json"].update(traceback="<TRACEBACK>", repr="X()")

        self.assert_caught(mutate)

    def test_a_usage_error_gains_no_recovered(self):
        self.assert_caught(lambda records: records["fluxo/err_bad_int"]["stderr"]["json"].update(recovered=True))

    def test_another_change_in_a_ratified_manifest_is_caught(self):
        def mutate(records):
            lines = records["fluxo/verify"]["files"]["work/proj/brolls/manifest.json"]["lines"]
            index = lines.index('        "status": "approved",')
            lines[index] = '        "status": "rejected",'

        self.assert_caught(mutate)

    def test_other_bytes_in_a_ratified_review_page_are_caught(self):
        def mutate(records):
            records["fluxo/preview"]["files"]["work/proj/brolls/review.html"] = {"bytes": 1, "sha256": "0" * 64}

        self.assert_caught(mutate)

    def test_an_extra_line_in_a_ratified_log_is_caught(self):
        def mutate(records):
            records["fluxo/err_project_locked"]["appends"]["work/proj/brolls/getbrolls.log"].append("level=INFO x")

        self.assert_caught(mutate)

    def test_an_unrelated_brief_problem_is_caught(self):
        def mutate(records):
            records["fluxo/brief"]["stdout"]["json"]["summary"]["problems"].append("Outro problema.")

        self.assert_caught(mutate)

    def test_other_text_in_a_ratified_error_is_caught(self):
        def mutate(records):
            records["fluxo/err_library_locked"]["stderr"]["json"]["error"] = "Qualquer coisa."

        self.assert_caught(mutate)

    def test_another_key_in_references_json_is_caught(self):
        def mutate(records):
            records["fluxo/remember"]["files"]["work/proj/brolls/references.json"]["lines"].insert(2, '  "x": 1,')

        self.assert_caught(mutate)

    def test_the_diagnostics_rule_takes_only_recovered_leaves(self):
        records = copy.deepcopy(self.expected)
        taken, _, _ = _golden_diff.classify(_golden_diff.diff(self.v250, records), self.accepted)
        for item in taken["diagnostico-recovered"]:
            with self.subTest(case=item["case"], where=item["where"]):
                self.assertTrue(item["where"].endswith("/recovered"))
                self.assertEqual(("added", False), (item["kind"], item["new"]))


class SnapshotPlatformOnlyTests(unittest.TestCase):
    """A foto deixa de fora só a trava da rotação de um log de projeto (`work/**/brolls/`)."""

    def test_only_the_project_log_lock_is_left_out(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            kept = [
                "gbhome/logs/.getbrolls.log.lock",
                "work/proj/.getbrolls.log.lock",
                "userhome/brolls/.getbrolls.log.lock",
            ]
            skipped = ["work/proj/brolls/.getbrolls.log.lock", "work/a/b/brolls/.getbrolls.log.lock"]
            for rel in kept + skipped:
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_bytes(b"")
            self.assertEqual(sorted(kept), sorted(_golden.snapshot(tmp)))


class GoldenFixtureHygieneTests(unittest.TestCase):
    def test_fixtures_carry_no_machine_path(self):
        problems = []
        for path in sorted(_golden.FIXTURES.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".md", ".html"}:
                text = path.read_text(encoding="utf-8")
                if LOCAL_PATH_PATTERN.search(text) or "/opt/homebrew" in text or "gb-ouro-" in text:
                    problems.append(path.relative_to(ROOT).as_posix())
        self.assertEqual([], problems)

    def test_fixtures_carry_no_frontmatter_date_of_a_recording_day(self):
        # As datas fixas dos modelos (docs/BRIEF.md, docs/RULES.md) ficam; a do dia em que se gravou, não.
        days = set(RECORDING_DAY.findall(_golden.REWRITES.read_text(encoding="utf-8")))
        problems = []
        for folder in (_golden.EXPECTED, _golden.V250, _golden.LAYOUT0):
            for path in sorted(folder.rglob("*")) if folder.is_dir() else ():
                if not path.is_file() or path.suffix not in {".json", ".md"}:
                    continue
                if days & set(FRONTMATTER_DATE.findall(path.read_text(encoding="utf-8"))):
                    problems.append(path.relative_to(ROOT).as_posix())
        self.assertTrue(days)
        self.assertEqual([], problems)

    def test_layout0_fixture_is_dated_on_a_fixed_past_day(self):
        # O que a CLI gerou no projeto congelado é de um dia anterior a qualquer rodada, como no
        # projeto de quem vem da 2.5; as datas fixas dos modelos (BRIEF.md, RULES.md) ficam.
        proj = _golden.LAYOUT0 / "proj"
        generated = [proj / "brolls" / "credits.md", proj / "entrega" / "README.md", *proj.glob("entrega/*/ORIGEM.md")]
        dates = {path.name: set(FRONTMATTER_DATE.findall(path.read_text(encoding="utf-8"))) for path in generated}
        self.assertEqual({name: {_golden.FROZEN_DAY} for name in ("credits.md", "README.md", "ORIGEM.md")}, dates)
        leftovers = [
            path.relative_to(ROOT).as_posix()
            for path in sorted(_golden.LAYOUT0.rglob("*"))
            if path.is_file()
            and path.suffix in {".json", ".jsonl", ".md"}
            and _golden.TODAY in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], leftovers)

    def test_materialize_keeps_the_past_day_of_the_frozen_project(self):
        with tempfile.TemporaryDirectory(prefix="gb-ouro-") as raw:
            project = _golden.materialize_layout0(Path(raw))
            credits_md = (project / "proj" / "brolls" / "credits.md").read_text(encoding="utf-8")
        self.assertIn(f"created: {_golden.FROZEN_DAY}\nupdated: {_golden.FROZEN_DAY}\n", credits_md)

    def test_layout0_golden_covers_a_project_created_on_an_earlier_day(self):
        # O `review` reescreve o credits.md com a data da rodada; o `verify` mantém o `created:` antigo.
        review = json.loads(_expected("layout0", "l0_review"))["files"]
        verify = json.loads(_expected("layout0", "l0_verify"))["files"]
        credits_md = review["work/layout0/proj/brolls/credits.md"]["lines"]
        readme = verify["work/layout0/proj/entrega/README.md"]["lines"]
        self.assertEqual(["created: <TODAY>", "updated: <TODAY>"], credits_md[3:5])
        self.assertEqual([f"created: {_golden.FROZEN_DAY}", "updated: <TODAY>"], readme[3:5])

    def test_update_needs_a_one_line_reason(self):
        for env in ({}, {"GB_GOLDEN_REASON": "  "}, {"GB_GOLDEN_REASON": "a\nb"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                _golden.update_reason(env)
        self.assertEqual(
            "volta ao byte da v2.5.0", _golden.update_reason({"GB_GOLDEN_REASON": " volta ao byte da v2.5.0 "})
        )


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gb-ouro-norm-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        roots = _golden.Roots(self.tmp, Path("/repo/getbrolls"), {"<PY>": "/opt/py/bin/python3"})
        self.norm = _golden.Normalizer(roots, "2.6.0")

    def test_masks_what_varies_between_runs(self):
        raw = (
            f'{{"at": "2026-10-02T14:25:49.234925+00:00", "project": "{self.tmp}/work/proj", '
            '"id": "0b9c2f3e-1d2a-4c5b-9e8f-7a6b5c4d3e2f", "duration_ms": 15, '
            '"traceback": "Traceback (most recent call last):\\n  File \\"x.py\\", line 3"}'
        )
        expected = (
            '{"at": "<TS>", "project": "<T>/work/proj", "id": "<UUID:1>", '
            '"duration_ms": "<MS>", "traceback": "<TRACEBACK>"}'
        )
        self.assertEqual(expected, self.norm.text(raw))

    def test_masks_the_date_of_the_run_and_the_next_day_but_not_other_dates(self):
        norm = _golden.Normalizer(self.norm.roots, "2.6.0", today="2026-10-31")
        raw = "created: 2026-10-31\nupdated: 2026-11-01\nat 2026-10-31T23:59:59Z\nfonte de 2026-10-30\n"
        self.assertEqual("created: <TODAY>\nupdated: <TODAY>\nat <TS>\nfonte de 2026-10-30\n", norm.text(raw))

    def test_freeze_writes_the_fixed_past_day_in_the_frontmatter_only(self):
        text = "---\ncreated: 2026-10-08\nupdated: 2026-10-08\n---\nGravado em 2026-10-08.\n"
        self.assertEqual(
            "---\ncreated: 2000-01-01\nupdated: 2000-01-01\n---\nGravado em 2026-10-08.\n",
            _golden.freeze_text(text, self.norm.roots, "2026-10-08"),
        )
        self.assertEqual(
            "created: 2025-03-04\n", _golden.freeze_text("created: 2025-03-04\n", self.norm.roots, "2026-10-08")
        )
        with self.assertRaises(ValueError):
            _golden.freeze_text("/repo/getbrolls/scripts/gb.py", self.norm.roots, "2026-10-08")

    def test_keeps_what_the_contract_promises(self):
        raw = '{"exit": 2, "error_code": "INVALID_DATA", "summary": {"line": "Registrei o candidato local:f6c9."}}'
        self.assertEqual(raw, self.norm.text(raw))

    def test_masks_versions_interpreter_and_log_stamps(self):
        self.assertEqual(
            "getbrolls <VERSION> (Python <PYV>; dados: checkout)",
            self.norm.text("getbrolls 2.6.0 (Python 3.11.15; dados: checkout)"),
        )
        self.assertEqual(
            '<PYNAME> "<CODE>/scripts/gb.py" status', self.norm.text('python "/repo/getbrolls/scripts/gb.py" status')
        )
        self.assertEqual(
            "level=INFO logger=getbrolls.cli event=command_end ms=<MS>",
            self.norm.text("2026-10-02T14:25:49.234Z level=INFO logger=getbrolls.cli event=command_end ms=41"),
        )

    def test_accented_root_in_any_json_form(self):
        roots = _golden.Roots(Path("/t/João"), Path("/r"), {})
        for raw in ('"/t/João/proj"', '"/t/Jo\\u00e3o/proj"'):
            with self.subTest(raw=raw):
                self.assertEqual('"<T>/proj"', roots.apply(raw))

    def test_hermetic_env_ignores_the_callers_environment(self):
        leaked = {"PEXELS_API_KEY": "k", "GB_PLUGINS": "demo", "GB_PROFILE": "/x/getbrolls.toml", "GB_HOME": "/real"}
        with unittest.mock.patch.dict(os.environ, leaked):
            env = _golden.hermetic_env(self.tmp)
        self.assertNotIn("PEXELS_API_KEY", env)
        self.assertNotIn("GB_PLUGINS", env)
        self.assertEqual("off", env["GB_PROFILE"])
        self.assertEqual(str(self.tmp / "gbhome"), env["GB_HOME"])
        self.assertEqual(str(self.tmp / "vazio.env"), env["GB_ENV_FILE"])
        self.assertEqual(b"", (self.tmp / "vazio.env").read_bytes())

    def test_hermetic_env_keeps_coverage_measurement_and_nothing_else(self):
        # Sem a variável o filho sai do gate de cobertura; com ela, só ela passa.
        clean = {key: value for key, value in os.environ.items() if key != "COVERAGE_PROCESS_START"}
        with unittest.mock.patch.dict(os.environ, clean, clear=True):
            self.assertNotIn("COVERAGE_PROCESS_START", _golden.hermetic_env(self.tmp))
        with unittest.mock.patch.dict(os.environ, {"COVERAGE_PROCESS_START": "/x/coveragerc", "COVERAGE_RCFILE": "/y"}):
            env = _golden.hermetic_env(self.tmp)
        self.assertEqual("/x/coveragerc", env["COVERAGE_PROCESS_START"])
        self.assertNotIn("COVERAGE_RCFILE", env)

    def test_windows_separators_after_a_root_become_slashes(self):
        self.assertEqual(
            '"<T>/work/proj"', _golden.Roots(Path("C:\\t"), Path("C:\\r"), {}).apply('"C:\\\\t\\\\work\\\\proj"')
        )


if __name__ == "__main__":
    unittest.main()
