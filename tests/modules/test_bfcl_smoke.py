import json
import tempfile
import unittest
from pathlib import Path

from bench.bfcl.smoke import check_generated_cases


class BFCLSmokeTest(unittest.TestCase):
    def test_generation_requires_exact_case_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "non_live" / "BFCL_v4_simple_python_result.json"
            result.parent.mkdir()
            expected = {"simple_python": ["simple_python_0", "simple_python_1"]}
            entries = [{"id": case_id, "result": []} for case_id in expected["simple_python"]]

            def write(rows):
                result.write_text("".join(json.dumps(row) + "\n" for row in rows))

            write(entries)
            check_generated_cases(root, expected)
            for invalid in (entries[:1], entries + entries[:1], [*entries, {"id": "extra"}]):
                write(invalid)
                with self.assertRaises(ValueError):
                    check_generated_cases(root, expected)

            write([entries[0], {**entries[1], "error": "timeout"}])
            with self.assertRaises(ValueError):
                check_generated_cases(root, expected)
