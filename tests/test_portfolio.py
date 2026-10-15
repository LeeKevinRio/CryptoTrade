"""投資組合型候選：不偷看未來、資金費方向正確、選參隔離、官方月檔解析"""

import io
import unittest
import zipfile

import numpy as np
import pandas as pd

from backtest import portfolio as pf
from backtest import vision_data as vd
from backtest.validation import Trade, gate


def _panel(n=200, syms=6, seed=0, drift=None):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    rets = rng.normal(0, 0.03, (n, syms))
    if drift is not None:
        rets += np.asarray(drift)[None, :]
    px = 100 * np.cumprod(1 + rets, axis=0)
    cols = [f"C{i}USDT" for i in range(syms)]
    return (pd.DataFrame(px, index=idx, columns=cols),
            pd.DataFrame(0.0, index=idx, columns=cols))


class TestXsmom(unittest.TestCase):
    def test_prefix_invariant(self):
        """接上未來資料後，已完結的週報酬不能改變"""
        c, f = _panel(220)
        full = pf.xsmom_weeks(c, f, 14, 2, "ls")
        part = pf.xsmom_weeks(c.iloc[:150], f.iloc[:150], 14, 2, "ls")
        self.assertEqual([w.ret for w in full[:len(part)]], [w.ret for w in part])

    def test_persistent_trends_make_money(self):
        """強者恆強的合成資料：多強空弱必須賺"""
        c, f = _panel(300, drift=[0.01, 0.006, 0.002, -0.002, -0.006, -0.01])
        weeks = pf.xsmom_weeks(c, f, 14, 2, "ls")
        self.assertGreater(np.mean([w.ret for w in weeks]), 0)

    def test_long_pays_positive_funding(self):
        idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
        c = pd.DataFrame(100.0, index=idx, columns=["A", "B"])
        f = pd.DataFrame(0.001, index=idx, columns=["A", "B"])          # 每日 0.1%
        w = pf.xsmom_weeks(c, f, 7, 1, "long")
        # 價格不動：每週付 7 × 0.1% 資金費；第一週另付進場手續費
        self.assertAlmostEqual(w[1].ret, -0.007, places=9)
        self.assertAlmostEqual(w[0].ret, -0.007 - pf.PERP_FEE, places=9)

    def test_ls_is_dollar_neutral(self):
        c, f = _panel(120)
        for w in pf.xsmom_weeks(c, f, 14, 2, "ls"):
            self.assertLessEqual(len(w.contrib), 4)


class TestCarry(unittest.TestCase):
    def _flat(self, rate, n=80):
        idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
        return (pd.DataFrame(100.0, index=idx, columns=["A"]),
                pd.DataFrame(rate, index=idx, columns=["A"]))

    def test_collects_funding_when_high(self):
        c, f = self._flat(0.0005)                                       # 年化 18.25%
        w = pf.carry_weeks(c, f, entry_apr=10, exit_ratio=0.5, lookback=3)
        self.assertAlmostEqual(w[1].ret, 0.5 * 0.0035, places=9)        # 半數資金在永續空單
        self.assertAlmostEqual(w[0].ret, 0.5 * 0.0035 - 0.5 * (pf.SPOT_FEE + pf.PERP_FEE), places=9)

    def test_stays_out_when_low(self):
        c, f = self._flat(0.0001)                                       # 年化 3.65%
        w = pf.carry_weeks(c, f, entry_apr=10, exit_ratio=0.5, lookback=3)
        self.assertTrue(all(x.ret == 0 for x in w))

    def test_decision_uses_only_past_funding(self):
        """調倉日之後的資金費暴衝，不能讓當週提前進場"""
        c, f = self._flat(0.0, n=80)
        f.iloc[pf.WARMUP + 1:, 0] = 0.01
        w = pf.carry_weeks(c, f, entry_apr=10, exit_ratio=0.5, lookback=3)
        self.assertEqual(w[0].ret, 0.0)
        self.assertGreater(w[1].ret, 0.0)


class TestWalkForward(unittest.TestCase):
    def test_selection_uses_train_weeks_only(self):
        dates = pd.date_range("2024-01-01", periods=60, freq="W", tz="UTC")
        # A：訓練期好、測試期差；B：訓練期差、測試期好 → 正確流程必選 A
        a = [pf.Week(d, 0.01 if i < 52 else -0.05, {}) for i, d in enumerate(dates)]
        b = [pf.Week(d, -0.01 if i < 52 else 0.05, {}) for i, d in enumerate(dates)]
        real_b, real_c = pf.BUILDERS["xsmom"], pf.combos
        pf.BUILDERS["xsmom"] = lambda c, f, tag: a if tag == "A" else b
        pf.combos = lambda s: [{"tag": "A"}, {"tag": "B"}]
        try:
            out = pf.walk_forward(None, None, "xsmom")
        finally:
            pf.BUILDERS["xsmom"], pf.combos = real_b, real_c
        self.assertEqual([w.ret for w in out], [-0.05] * 8)   # 兩折皆選 A

    def test_contrib_overrides_symbol_check(self):
        trades = [Trade("PORT", f"2024-{i % 12 + 1:02d}", 0.5) for i in range(100)]
        r = gate(trades, symbol_contrib={"A": 100.0, "B": -60.0})
        ok = next(o for n, o, _ in r.checks if n == "不靠單一標的")
        self.assertFalse(ok)


class TestVisionParse(unittest.TestCase):
    def _zip(self, text):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("x.csv", text)
        return b.getvalue()

    def test_klines_with_and_without_header(self):
        row = "1704067200000,100,110,90,105,1000,1704153599999,0,0,0,0,0"
        hdr = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbqv,ignore"
        for text in (row, hdr + "\n" + row):
            df = vd.parse_klines(self._zip(text))
            self.assertEqual(df["close"].iloc[0], 105.0)
            self.assertEqual(str(df.index[0].date()), "2024-01-01")

    def test_microsecond_timestamps(self):
        df = vd.parse_klines(self._zip("1735689600000000,1,1,1,2,1,0,0,0,0,0,0"))
        self.assertEqual(str(df.index[0].date()), "2025-01-01")

    def test_funding(self):
        text = "calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,0.0001\n"
        f = vd.parse_funding(self._zip(text))
        self.assertAlmostEqual(f["rate"].iloc[0], 0.0001)

    def test_month_list_excludes_current(self):
        from datetime import datetime, timezone
        ms = vd.month_list(3, datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertEqual(ms, [(2026, 7), (2026, 8), (2026, 9)])


if __name__ == "__main__":
    unittest.main()


class TestPointInTimeUniverse(unittest.TestCase):
    def test_uses_only_past_volume(self):
        idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
        v = pd.DataFrame({"A": 1.0, "B": 2.0, "C": 3.0}, index=idx)
        v.loc[idx[40]:, "A"] = 1e9                      # A 在第 40 天後才爆量
        e = pf.pit_universe(v, top_n=2, window=5)
        self.assertFalse(e[39, 0])                      # 爆量前：A 不在池內
        self.assertTrue(e[45, 0])                       # 爆量 5 天後：A 進池
        self.assertFalse(e[3].any())                    # 暖身不足：誰都不可交易

    def test_xsmom_respects_universe(self):
        c, f = _panel(120)
        none = np.zeros(c.shape, dtype=bool)
        weeks = pf.xsmom_weeks(c, f, 14, 2, "ls", eligible=none)
        self.assertTrue(all(w.ret == 0 and not w.contrib for w in weeks))
