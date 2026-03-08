import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SIGNAL_SYSTEM_ROOT = ROOT / "signal_system"
for candidate in (str(ROOT), str(SIGNAL_SYSTEM_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from data.symbols import normalize_ts_code, to_akshare_symbol, to_pytdx_params


class SymbolHelpersTest(unittest.TestCase):
    def test_normalize_mainboard_symbols(self):
        self.assertEqual(normalize_ts_code("600276"), "600276.SH")
        self.assertEqual(normalize_ts_code("000001"), "000001.SZ")
        self.assertEqual(normalize_ts_code("830799"), "830799.BJ")

    def test_normalize_beijing_920_prefix_symbols(self):
        self.assertEqual(normalize_ts_code("920003"), "920003.BJ")
        self.assertEqual(normalize_ts_code("920367"), "920367.BJ")

    def test_provider_symbol_converters(self):
        self.assertEqual(to_akshare_symbol("600276.SH", with_exchange_prefix=True), "sh600276")
        self.assertEqual(to_akshare_symbol("920003.BJ", with_exchange_prefix=True), "bj920003")
        self.assertEqual(to_pytdx_params("600276.SH"), (1, "600276"))
        self.assertEqual(to_pytdx_params("000001.SZ"), (0, "000001"))


if __name__ == "__main__":
    unittest.main()
