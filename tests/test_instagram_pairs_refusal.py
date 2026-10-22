"""instagram_pairs: limite de ritmo espera, URL expirada recaptura, recusa para o lote (sem rede)."""

import contextlib
import io
import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
from _paths import ROOT  # noqa: F401  (efeito de import: insere scripts/ em sys.path)  # pylint: disable=unused-import

from getbrolls import instagram_pairs as ig
from getbrolls import queue

PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
NOW = 1_800_000_000  # 2027-01-15, só um instante fixo para o relógio do teste
PAST_OE = f"{NOW - 3600:x}"
FUTURE_OE = f"{NOW + 3600:x}"
CDN = "https://scontent.cdninstagram.com/v/t50/video.mp4"


def signed(oe):
    return f"{CDN}?_nc_ht=scontent.cdninstagram.com&oh=00_abc&oe={oe}" if oe else f"{CDN}?oh=00_abc"


def write_pairs(configs, stems, url=CDN):
    configs.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (configs / f"{stem}_video.conf").write_text(f'url = "{url}"\n', encoding="utf-8")
        (configs / f"{stem}_audio.conf").write_text(f'url = "{url}"\n', encoding="utf-8")


def curl_failure(status, commands=None):
    def fail(cmd, **_kwargs):
        if commands is not None:
            commands.append(cmd)
        raise subprocess.CalledProcessError(
            22, cmd, stderr=f"curl: (22) The requested URL returned error: {status}".encode()
        )

    return fail


def batch_args(root, *extra):
    return [
        "--config-dir",
        str(root / "configs"),
        "--output-dir",
        str(root / "out"),
        "--parts-dir",
        str(root / "parts"),
        "--config-output-root",
        str(root),
        "--layout",
        "flat",
        "--summary-json",
        str(root / "work/summary.json"),
        "--project",
        str(root),
        "--pace",
        "0",
        *extra,
    ]


def download(conf, root):
    return ig.download_or_reuse(
        cfg_path=conf,
        part_path=root / "p.mp4",
        config_output_root=root,
        force_download=False,
        prefer_config_output=False,
    )


class SignedUrlExpiryTests(unittest.TestCase):
    def test_only_an_oe_well_in_the_past_is_expired(self):
        self.assertTrue(ig.signed_url_expired(signed(PAST_OE), now=NOW))
        self.assertFalse(ig.signed_url_expired(signed(FUTURE_OE), now=NOW))
        self.assertFalse(ig.signed_url_expired(signed(f"{NOW:x}"), now=NOW), "no instante exato ainda vale")

    def test_the_clock_skew_margin_sends_the_ambiguous_cases_to_the_refusal(self):
        margin = ig.OE_SKEW_S
        self.assertEqual(300, margin)
        self.assertFalse(ig.signed_url_expired(signed(f"{NOW - margin + 1:x}"), now=NOW), "vencido há 299 s: recusa")
        self.assertFalse(ig.signed_url_expired(signed(f"{NOW - margin:x}"), now=NOW), "vencido há 300 s: recusa")
        self.assertTrue(ig.signed_url_expired(signed(f"{NOW - margin - 1:x}"), now=NOW), "vencido há 301 s: expirada")

    def test_a_zero_oe_is_treated_as_absent(self):
        for oe in ("0", "00", "0000000000000000"):
            with self.subTest(oe=oe):
                self.assertFalse(ig.signed_url_expired(signed(oe), now=NOW))

    def test_no_oe_or_an_unreadable_one_is_not_a_confirmation(self):
        for url in (signed(None), signed("zzzz"), signed(""), f"{CDN}?oe=", f"{CDN}?oe=0x", "não é url", ""):
            with self.subTest(url=url):
                self.assertFalse(ig.signed_url_expired(url, now=NOW))

    def test_oe_is_read_from_the_query_only(self):
        self.assertFalse(ig.signed_url_expired(f"{CDN}/oe={PAST_OE}/video.mp4", now=NOW))
        self.assertFalse(ig.signed_url_expired(f"{CDN}#oe={PAST_OE}", now=NOW))

    def test_a_repeated_oe_is_ambiguous_and_never_counts_as_expired(self):
        self.assertFalse(ig.signed_url_expired(f"{CDN}?oe={PAST_OE}&oe={FUTURE_OE}", now=NOW))


class DownloadClassificationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(ig.socket, "getaddrinfo", return_value=PUBLIC_DNS))
        self.enterContext(patch.object(ig.time, "time", return_value=NOW))
        self.root = Path(tempfile.mkdtemp(prefix="gb-igrefusal-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def conf(self, url):
        path = self.root / "v.conf"
        path.write_text(f'url = "{url}"\n', encoding="utf-8")
        return path

    def fails(self, url, status):
        with (
            patch.object(ig.subprocess, "run", side_effect=curl_failure(status)),
            self.assertRaises(ig.CollectError) as caught,
        ):
            download(self.conf(url), self.root)
        return caught.exception

    def test_a_429_is_a_rate_limit_with_the_cooldown_and_the_old_wording(self):
        error = self.fails(signed(FUTURE_OE), 429)
        self.assertTrue(error.cooldown)
        self.assertFalse(error.refused)
        self.assertEqual("429", error.http_status)
        self.assertIn("HTTP 429", error.message)
        self.assertIn("aguarde o cooldown", error.message)
        self.assertNotIn("recapture", error.message, "um limite de ritmo não pede URL nova")

    def test_a_403_with_oe_in_the_past_is_expired_and_recaptured_by_the_same_route(self):
        error = self.fails(signed(PAST_OE), 403)
        self.assertFalse(error.cooldown)
        self.assertFalse(error.refused)
        self.assertIn("expirada", error.message)
        self.assertIn("recapture pela mesma rota", error.message)

    def test_the_expired_message_never_carries_the_url_or_a_status_the_queue_would_read_as_a_refusal(self):
        error = self.fails(signed(PAST_OE), 403)
        self.assertNotIn("cdninstagram", error.message)
        self.assertNotIn("oe=", error.message)
        self.assertNotIn("403", error.message)
        self.assertIsNone(queue.failure_kind(error.message))

    def test_a_403_with_oe_in_the_future_or_absent_is_a_refusal_not_a_wait(self):
        for url in (signed(FUTURE_OE), signed(None), signed("zzzz")):
            with self.subTest(url=url):
                error = self.fails(url, 403)
                self.assertTrue(error.refused)
                self.assertFalse(error.cooldown)
                self.assertEqual("403", error.http_status)
                self.assertIn("HTTP 403", error.message)
                self.assertIn("pare o lote", error.message)
                self.assertIn("diagnostique", error.message)
                self.assertNotIn("cooldown", error.message)
                self.assertNotIn("aguarde", error.message)
                self.assertNotIn("cdninstagram", error.message)

    def test_a_401_is_a_refusal_too(self):
        error = self.fails(signed(FUTURE_OE), 401)
        self.assertTrue(error.refused)
        self.assertFalse(error.cooldown)
        self.assertEqual("401", error.http_status)

    def test_the_refusal_reason_is_read_as_a_refusal_by_the_queue(self):
        self.assertEqual("access_refused", queue.failure_kind(self.fails(signed(None), 403).message))

    def test_other_statuses_stay_plain_failures(self):
        for status in (404, 410, 500):
            with self.subTest(status=status):
                error = self.fails(signed(PAST_OE), status)
                self.assertFalse(error.cooldown)
                self.assertFalse(error.refused)
                self.assertIn("curl falhou para o config", error.message)


class BatchStopTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(ig, "ensure_tool"))
        self.enterContext(patch.object(ig.time, "sleep"))
        self.enterContext(patch.object(ig.time, "time", return_value=NOW))
        self.enterContext(patch.object(ig.socket, "getaddrinfo", return_value=PUBLIC_DNS))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.root = Path(tempfile.mkdtemp(prefix="gb-igrefusal-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_pairs(self.root / "configs", ["01_A", "02_B", "03_C"], url=CDN)
        state = queue.empty_state()
        queue.add(
            state,
            "instagram",
            ["https://www.instagram.com/reel/ABC123xyz/", "https://www.instagram.com/reel/DEF456uvw/"],
        )
        queue.claim_next(state)  # o item que o coletor está processando
        queue.save(queue.queue_path(self.root), state)

    def run_batch(self, failing, *extra):
        commands = []
        with patch.object(ig.subprocess, "run", side_effect=failing(commands)):
            code = ig.main(batch_args(self.root, *extra))
        summary = json.loads((self.root / "work/summary.json").read_text(encoding="utf-8"))
        return code, commands, summary

    def assert_ends_through_the_abort_path(self, *extra):
        code, commands, summary = self.run_batch(lambda c: curl_failure(403, c), *extra)
        self.assertEqual(1, code)
        self.assertEqual(1, len(commands), "depois da recusa nenhum outro pedido sai")
        self.assertEqual(["failed", "skipped", "skipped"], [r["status"] for r in summary["results"]])
        self.assertEqual(["aborted", "aborted"], [r["reason"] for r in summary["results"][1:]])
        self.assertIn("HTTP 403", summary["results"][0]["reason"])
        self.assertIsNone(summary["stopped_by"])
        self.assertIn("-- lote interrompido", self.err.getvalue())

    def test_a_refusal_ends_the_batch_through_the_abort_path(self):
        self.assert_ends_through_the_abort_path()

    def test_a_refusal_ends_the_batch_even_with_continue_on_error(self):
        self.assert_ends_through_the_abort_path("--continue-on-error")

    def test_a_refusal_records_the_stop_in_the_queue_without_a_cooldown(self):
        self.run_batch(lambda c: curl_failure(403, c), "--continue-on-error")
        saved = queue.load(queue.queue_path(self.root))
        state = saved["providers"]["instagram"]
        self.assertIsNone(state["cooldown_until"])
        self.assertEqual(0, state["cooldown_strikes"])
        self.assertEqual("http_403", state["stopped"]["class"])
        self.assertEqual("failed", saved["items"][0]["status"])
        self.assertIn("parada registrada", self.err.getvalue())
        self.assertNotIn("cooldown recorded", self.err.getvalue())
        with self.assertRaises(ValueError):
            queue.claim_next(saved)

    def test_a_429_still_stops_with_the_cooldown_and_no_stop(self):
        code, commands, summary = self.run_batch(lambda c: curl_failure(429, c), "--continue-on-error")
        self.assertEqual(1, code)
        self.assertEqual(1, len(commands))
        self.assertEqual("cooldown", summary["stopped_by"])
        self.assertEqual(["cooldown", "cooldown"], [r["reason"] for r in summary["results"][1:]])
        state = queue.load(queue.queue_path(self.root))["providers"]["instagram"]
        self.assertTrue(state["cooldown_until"])
        self.assertNotIn("stopped", state)

    def test_an_expired_url_stops_at_the_first_item_so_no_request_goes_out_per_item(self):
        write_pairs(self.root / "configs", ["01_A", "02_B", "03_C"], url=signed(PAST_OE))
        code, commands, summary = self.run_batch(lambda c: curl_failure(403, c), "--continue-on-error")
        self.assertEqual(1, code)
        self.assertEqual(1, len(commands), "URLs capturadas juntas vencem juntas: um pedido, não um por item")
        self.assertEqual(["failed", "skipped", "skipped"], [r["status"] for r in summary["results"]])
        self.assertIn("recapture pela mesma rota", summary["results"][0]["reason"])
        self.assertEqual(["recapture", "recapture"], [r["reason"] for r in summary["results"][1:]])
        self.assertEqual("recapture", summary["stopped_by"])
        state = queue.load(queue.queue_path(self.root))["providers"].get("instagram", {})
        self.assertFalse(state.get("cooldown_until"))
        self.assertNotIn("stopped", state, "expirada não é recusa: a fila não para")

    def test_an_expired_url_without_continue_on_error_ends_like_any_failure(self):
        write_pairs(self.root / "configs", ["01_A", "02_B", "03_C"], url=signed(PAST_OE))
        code, commands, summary = self.run_batch(lambda c: curl_failure(403, c))
        self.assertEqual(1, code)
        self.assertEqual(1, len(commands))
        self.assertEqual(["aborted", "aborted"], [r["reason"] for r in summary["results"][1:]])

    def test_a_stopped_queue_blocks_the_collector_before_any_request(self):
        queue.record_refusal(self.root, "instagram", "CDN recusou com HTTP 403", reason_class="http_403")
        for extra in ((), ("--continue-on-error",)):
            with self.subTest(extra=extra):
                self.err.truncate(0)
                self.err.seek(0)
                code, commands, summary = self.run_batch(lambda c: curl_failure(200, c), *extra)
                self.assertEqual(1, code)
                self.assertEqual([], commands, "nenhum pedido sai com a fila parada")
                self.assertEqual(["skipped"] * 3, [r["status"] for r in summary["results"]])
                self.assertEqual(["aborted"] * 3, [r["reason"] for r in summary["results"]])
                self.assertIn("-- lote interrompido: fila do instagram parada em", self.err.getvalue())
                self.assertIn("esperar não autoriza retomar", self.err.getvalue())

    def test_the_collector_runs_again_after_a_person_resumes_the_queue(self):
        queue.record_refusal(self.root, "instagram", "CDN recusou com HTTP 403", reason_class="http_403")
        path = queue.queue_path(self.root)
        data = queue.load(path)
        queue.resume(
            data,
            "instagram",
            by="Bruno",
            statement="voltei a logar",
            expect=data["providers"]["instagram"]["stopped"]["at"],
        )
        queue.save(path, data)
        _code, commands, _summary = self.run_batch(lambda c: curl_failure(429, c), "--continue-on-error")
        self.assertEqual(1, len(commands), "a fila liberada deixa o lote pedir de novo")

    def test_an_unreadable_queue_also_blocks_the_collector(self):
        queue.queue_path(self.root).write_text("{ não é json", encoding="utf-8")
        code, commands, _summary = self.run_batch(lambda c: curl_failure(200, c))
        self.assertEqual(1, code)
        self.assertEqual([], commands)
        self.assertIn("fila do projeto ilegível", self.err.getvalue())

    def test_a_collector_run_without_a_project_is_outside_the_stop(self):
        args = batch_args(self.root)
        index = args.index("--project")
        del args[index : index + 2]
        commands = []
        with patch.object(ig.subprocess, "run", side_effect=curl_failure(429, commands)):
            self.assertEqual(1, ig.main(args))
        self.assertEqual(1, len(commands))

    def test_without_a_project_the_refusal_still_ends_the_batch_and_warns(self):
        args = batch_args(self.root)
        index = args.index("--project")
        del args[index : index + 2]
        with patch.object(ig.subprocess, "run", side_effect=curl_failure(403)):
            self.assertEqual(1, ig.main(args))
        self.assertIn("parada não registrada (nenhum --project informado", self.err.getvalue())

    def test_a_queue_that_cannot_record_the_stop_warns_and_the_batch_still_ends(self):
        with patch("getbrolls.queue.record_refusal", side_effect=ValueError("fila ruim")):
            code, commands, _summary = self.run_batch(lambda c: curl_failure(403, c))
        self.assertEqual(1, code)
        self.assertEqual(1, len(commands))
        self.assertIn("parada não registrada (fila ruim)", self.err.getvalue())

    def test_a_project_without_a_queue_json_warns(self):
        (self.root / "work" / "queue.json").unlink()
        self.run_batch(lambda c: curl_failure(403, c))
        self.assertIn("parada não registrada (nenhum work/queue.json", self.err.getvalue())

    def test_the_refusal_record_works_run_as_a_script(self):
        with patch.object(ig, "__package__", ""):
            stamp = ig.record_queue_refusal(self.root, "CDN recusou com HTTP 403", "http_403")
        self.assertTrue(stamp)
        self.assertIn("stopped", queue.load(queue.queue_path(self.root))["providers"]["instagram"])


if __name__ == "__main__":
    unittest.main()
