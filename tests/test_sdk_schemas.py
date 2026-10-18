"""`sdk.schemas`: os schemas públicos do core, pelo nome."""

import json
import unittest

import _isolation  # noqa: F401  (efeito de import: define GB_HOME)  # pylint: disable=unused-import
from _paths import ROOT  # noqa: F401  (efeito de import: insere scripts/ em sys.path)  # pylint: disable=unused-import

from getbrolls import _paths as core_paths
from getbrolls.sdk import schemas


class SchemasTests(unittest.TestCase):
    def test_every_public_name_loads_the_packaged_schema(self):
        for name in schemas.NAMES:
            with self.subTest(name=name):
                on_disk = json.loads(core_paths.data_path("schemas", f"{name}.schema.json").read_text(encoding="utf-8"))
                self.assertEqual(on_disk, schemas.load(name))

    def test_an_unknown_name_is_refused_and_names_the_schema(self):
        for name in ("nao-existe", "", "brief.schema", "../brief"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Schema desconhecido: "):
                schemas.load(name)

    def test_the_schema_is_read_once_per_process(self):
        self.assertIs(schemas.load("brief"), schemas.load("brief"))


if __name__ == "__main__":
    unittest.main()
