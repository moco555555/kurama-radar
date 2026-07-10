# -*- coding: utf-8 -*-
"""
KURAMA MARKET RADAR v1.00
市場歪みレーダー: FX/ゴールド/株価指数の歪みを1枚のマップに可視化する。

- 各銘柄の根拠(MA乖離/長期RSI/マクロ残差/COT/ボラ/相関)を
  「自分自身の過去2年分布に対するパーセンタイル」へ正規化
- 合成した歪みスコア(0-100)× トレンド軸(-100..+100)でバブルマップ化
- 現在の指紋(全根拠ベクトル)と類似する過去局面を照合し、
  その後の中期MA回帰率・平均逆行幅を統計表示
- 出力は自己完結HTML 1枚(docs/index.html) → GitHub Pagesでそのまま公開可

使い方:
  python kurama_radar.py                # 本番(要ネット接続)
  python kurama_radar.py --demo         # 合成データで動作確認
  python kurama_radar.py --notify       # 警戒ゾーン突入時にDiscord通知
                                        # (環境変数 DISCORD_WEBHOOK_URL)
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import zipfile

import numpy as np
import pandas as pd
import requests

from dashboard_template import TEMPLATE

# ------------------------------------------------------------------ 設定
WINDOW = 504          # パーセンタイル計算窓(約2年)
MINP = 252            # 最低必要日数(約1年)
MA_M_PERIOD = 75      # 中期MA(表示は「中期MA乖離」に抽象化)
MA_L_PERIOD = 100     # 長期MA(表示は「長期MA乖離」に抽象化)
SIM_TH = 0.86         # 類似局面の類似度しきい値
ALERT_TH = 70         # 警戒ゾーン(Discord通知しきい値)

# 銘柄設定: macro=回帰残差の相手, corr=20日相関の相手, vol=ボラ指標, cot=COT対象
CONFIG = {
    "XAUUSD": {
        "ticker": "GC=F",
        "vol": ("^GVZ", "GVZ 金ボラ"),
        "macro": ("DFII10", "fred", "実質金利残差"),
        "corr": ("DX-Y.NYB", "DXY相関 20日"),
        "cot": ("GOLD - COMMODITY EXCHANGE", "COT投機筋(金)"),
    },
    "USDJPY": {
        "ticker": "JPY=X",
        "vol": (None, "ATRレジーム"),
        "macro": ("^TNX", "yf", "米金利残差"),
        "corr": ("^N225", "N225相関 20日"),
        "cot": ("JAPANESE YEN - CHICAGO MERCANTILE", "COT投機筋(円)"),
    },
    "N225": {
        "ticker": "^N225",
        "vol": (None, "ATRレジーム"),
        "macro": None,
        "corr": ("JPY=X", "USDJPY相関 20日"),
        "cot": None,
    },
    "EURUSD": {
        "ticker": "EURUSD=X",
        "vol": (None, "ATRレジーム"),
        "macro": ("^TNX", "yf", "米金利残差"),
        "corr": ("DX-Y.NYB", "DXY相関 20日"),
        "cot": ("EURO FX - CHICAGO MERCANTILE", "COT投機筋(ユーロ)"),
    },
    "GBPUSD": {
        "ticker": "GBPUSD=X",
        "vol": (None, "ATRレジーム"),
        "macro": ("^TNX", "yf", "米金利残差"),
        "corr": ("DX-Y.NYB", "DXY相関 20日"),
        "cot": ("BRITISH POUND", "COT投機筋(ポンド)"),
    },
    "GBPJPY": {
        "ticker": "GBPJPY=X",
        "vol": (None, "ATRレジーム"),
        "macro": None,
        "corr": ("JPY=X", "USDJPY相関 20日"),
        "cot": None,
    },
    "BTCUSD": {
        "ticker": "BTC-USD",
        "vol": (None, "ATRレジーム"),
        "macro": ("DX-Y.NYB", "yf", "DXY残差"),
        "corr": ("^NDX", "NAS100相関 20日"),
        "cot": ("BITCOIN", "COT投機筋(BTC)"),
    },
    "ETHUSD": {
        "ticker": "ETH-USD",
        "vol": (None, "ATRレジーム"),
        "macro": None,
        "corr": ("BTC-USD", "BTC相関 20日"),
        "cot": None,
        "ratio": ("BTC-USD", "ETH/BTC比率"),
    },
    "NAS100": {
        "ticker": "^NDX",
        "vol": ("^VIX", "VIX"),
        "macro": ("^TNX", "yf", "米金利残差"),
        "corr": ("^DJI", "ダウ相関 20日"),
        "cot": None,
    },
    "US30": {
        "ticker": "^DJI",
        "vol": ("^VIX", "VIX"),
        "macro": None,
        "corr": ("^NDX", "ナスダック相関 20日"),
        "cot": None,
    },
}

# 根拠ごとの重み(歪みスコア合成用)。手法の中核であるMA乖離を最重視。
WEIGHTS = {
    "dev_m": 2.0, "dev_l": 1.5,
    "rsi_w": 1.2, "rsi_d": 0.8,
    "macro": 1.2, "cot": 1.0,
    "corr": 1.0, "vol": 0.5, "ratio": 1.0,
}

# マップ上に相関の糸を張るペア
LINK_PAIRS = [("USDJPY", "N225"), ("NAS100", "US30"), ("NAS100", "BTCUSD")]

UA = {"User-Agent": "Mozilla/5.0 (kurama-radar/1.0)"}


# ------------------------------------------------------------------ データ取得
def fetch_ohlc(ticker: str, period: str = "3y") -> pd.DataFrame | None:
    """yfinanceから日足OHLCを取得。失敗時はNone。"""
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).history(period=period, interval="1d",
                                       auto_adjust=False)
        if df is None or len(df) < MINP:
            print(f"[warn] {ticker}: データ不足 ({0 if df is None else len(df)}本)")
            return None
        df = df.rename(columns=str.lower)[["open", "high", "low", "close"]]
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        return df[~df.index.duplicated(keep="last")]
    except Exception as e:
        print(f"[warn] {ticker}: 取得失敗 {e}")
        return None


def fetch_fred(series_id: str) -> pd.Series | None:
    """FREDからCSVを取得(APIキー不要のfredgraphエンドポイント)。"""
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["date", "value"]
        df["date"] = pd.to_datetime(df["date"])
        s = pd.to_numeric(df["value"], errors="coerce")
        s.index = df["date"]
        return s.dropna()
    except Exception as e:
        print(f"[warn] FRED {series_id}: 取得失敗 {e}")
        return None


def fetch_cot(name_contains: str) -> pd.Series | None:
    """CFTC COTレポート(legacy futures-only)から投機筋ネットポジションを取得。
    直近3年分の年次ZIPを結合。失敗しても他の根拠で継続する。"""
    frames = []
    year_now = dt.date.today().year
    for year in range(year_now - 2, year_now + 1):
        try:
            url = f"https://www.cftc.gov/files/dea/history/deacot{year}.zip"
            r = requests.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                with z.open(z.namelist()[0]) as f:
                    frames.append(pd.read_csv(f, low_memory=False))
        except Exception as e:
            print(f"[warn] COT {year}: 取得失敗 {e}")
    if not frames:
        return None
    try:
        df = pd.concat(frames, ignore_index=True)
        name_col = next(c for c in df.columns if "Market and Exchange" in c)
        date_col = next(c for c in df.columns if "YYYY-MM-DD" in c)
        long_col = next(c for c in df.columns
                        if "Noncommercial Positions-Long (All)" in c)
        short_col = next(c for c in df.columns
                         if "Noncommercial Positions-Short (All)" in c)
        sub = df[df[name_col].str.contains(name_contains, na=False)].copy()
        if sub.empty:
            print(f"[warn] COT: '{name_contains}' が見つからん")
            return None
        sub["date"] = pd.to_datetime(sub[date_col])
        net = (pd.to_numeric(sub[long_col], errors="coerce")
               - pd.to_numeric(sub[short_col], errors="coerce"))
        net.index = sub["date"]
        return net.sort_index().dropna()
    except Exception as e:
        print(f"[warn] COT パース失敗: {e}")
        return None


# ------------------------------------------------------------------ デモデータ
def demo_ohlc(seed: int, start: float, trend_boost: float = 0.0) -> pd.DataFrame:
    """合成OHLC(動作確認用)。trend_boostで直近をトレンドさせる。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=dt.date.today(), periods=780)
    n = len(idx)
    ret = rng.normal(0.0002, 0.010, n)
    ret[-70:] += trend_boost                      # 直近だけドリフト付与
    close = start * np.exp(np.cumsum(ret))
    spread = np.abs(rng.normal(0, 0.006, n)) + 0.003
    high = close * (1 + spread)
    low = close * (1 - spread)
    opn = np.roll(close, 1); opn[0] = close[0]
    return pd.DataFrame({"open": opn, "high": high, "low": low,
                         "close": close}, index=idx)


def demo_series(seed: int, base: float, ref: pd.Series | None = None) -> pd.Series:
    rng = np.random.default_rng(seed)
    if ref is not None:  # refと相関を持つ系列(残差テスト用)
        noise = rng.normal(0, ref.std() * 0.4, len(ref))
        return (ref * 0.6 + noise + base).rename("v")
    idx = pd.bdate_range(end=dt.date.today(), periods=780)
    scale = max(abs(base) * 0.01, 1e-6)
    return pd.Series(base + np.cumsum(rng.normal(0, scale, len(idx))),
                     index=idx)


# ------------------------------------------------------------------ 指標計算
def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def pct_rank(s: pd.Series) -> pd.Series:
    """過去WINDOW日の分布に対する現在値のパーセンタイル(0-1)。"""
    return s.rolling(WINDOW, min_periods=MINP).rank(pct=True)


def rolling_residual(y: pd.Series, x: pd.Series, n: int = 120) -> pd.Series:
    """ローリングOLS残差: yをxで回帰した残差(価格単位)。"""
    x, y = x.align(y, join="inner")
    x = x.ffill(); y = y.ffill()
    mx = x.rolling(n).mean(); my = y.rolling(n).mean()
    cov = (x * y).rolling(n).mean() - mx * my
    var = (x * x).rolling(n).mean() - mx * mx
    beta = cov / var.replace(0, np.nan)
    alpha = my - beta * mx
    return y - (alpha + beta * x)


# ------------------------------------------------------------------ 銘柄ビルド
def build_symbol(name: str, df: pd.DataFrame, vol_s: pd.Series | None,
                 vol_label: str, macro_s: pd.Series | None, macro_label: str,
                 corr_ref: pd.DataFrame | None, corr_label: str,
                 cot_s: pd.Series | None, cot_label: str,
                 ratio_ref: pd.Series | None = None,
                 ratio_label: str = "") -> dict:
    """1銘柄分の根拠系列・歪みスコア・トレンド・類似局面統計を計算する。"""
    close, high, low = df["close"], df["high"], df["low"]
    a = atr(df)
    sma_m = close.rolling(MA_M_PERIOD).mean()
    sma_l = close.rolling(MA_L_PERIOD).mean()

    dev_m = (close - sma_m) / a
    dev_l = (close - sma_l) / a
    rsi_d = rsi(close)
    rsi_w = rsi(close.resample("W-FRI").last()).reindex(close.index).ffill()

    # (key, ラベル, 値系列, パーセンタイル系列, 値フォーマット)
    factors = [
        ("dev_m", "中期MA乖離", dev_m, pct_rank(dev_m), "atr"),
        ("dev_l", "長期MA乖離", dev_l, pct_rank(dev_l), "atr"),
        ("rsi_w", "週足RSI", rsi_w, pct_rank(rsi_w), "num"),
        ("rsi_d", "日足RSI", rsi_d, pct_rank(rsi_d), "num"),
    ]
    if macro_s is not None:
        resid = rolling_residual(close, macro_s.reindex(close.index).ffill())
        factors.append(("macro", macro_label, resid, pct_rank(resid), "resid"))
    if cot_s is not None:
        cot_d = cot_s.reindex(close.index, method="ffill")
        factors.append(("cot", cot_label, cot_d, pct_rank(cot_d), "int"))
    if corr_ref is not None:
        r1 = close.pct_change()
        r2 = corr_ref["close"].reindex(close.index).ffill().pct_change()
        corr = r1.rolling(20).corr(r2)
        factors.append(("corr", corr_label, corr, pct_rank(corr), "corr"))
    if ratio_ref is not None:
        ratio = close / ratio_ref.reindex(close.index).ffill()
        factors.append(("ratio", ratio_label, ratio, pct_rank(ratio), "num4"))
    if vol_s is not None:
        v = vol_s.reindex(close.index).ffill()
        factors.append(("vol", vol_label, v, pct_rank(v), "num"))
    else:
        v = a / close * 100
        factors.append(("vol", vol_label, v, pct_rank(v), "num"))

    # 歪みスコア: |pct-0.5|*200 の加重平均(欠損根拠は自動で除外)
    pct_df = pd.concat({k: p for k, _, _, p, _ in factors}, axis=1)
    ext = (pct_df - 0.5).abs() * 200
    w = pd.Series({k: WEIGHTS[k] for k in pct_df.columns})
    score = (ext * w).sum(axis=1) / (ext.notna() * w).sum(axis=1)

    # トレンド軸: 中期/長期MAの傾き(ATR相対)を-100..+100へ
    slope_m = (sma_m - sma_m.shift(10)) / (a * 10)
    slope_l = (sma_l - sma_l.shift(10)) / (a * 10)
    trend = ((slope_m + slope_l) / 2 * 400).clip(-100, 100)

    # 類似局面照合
    analog = find_analogs(pct_df, close, high, low, sma_m, a,
                          float(dev_m.iloc[-1]))

    # スナップショット(最終行)を表示用に整形
    fp = []
    for k, label, val_s, p_s, fmt in factors:
        pv, vv = p_s.iloc[-1], val_s.iloc[-1]
        if pd.isna(pv) or pd.isna(vv):
            continue
        fp.append({"key": k, "label": label,
                   "pct": round(float(pv), 3),
                   "value": fmt_value(vv, fmt)})

    trail_df = pd.concat([trend, score], axis=1, keys=["t", "s"]).dropna()
    trail = [[round(float(t), 1), round(float(s), 1)]
             for t, s in trail_df.tail(6).values]

    sc = float(score.iloc[-1]) if not pd.isna(score.iloc[-1]) else 0.0
    tr = float(trend.iloc[-1]) if not pd.isna(trend.iloc[-1]) else 0.0
    dev_now = (float(dev_m.iloc[-1]) if not pd.isna(dev_m.iloc[-1]) else 0.0)
    atr_now = float(a.iloc[-1]) if not pd.isna(a.iloc[-1]) else 0.0
    ma_now = float(sma_m.iloc[-1]) if not pd.isna(sma_m.iloc[-1]) else 0.0
    price_now = float(close.iloc[-1])
    reco = make_reco(sc, dev_now, price_now, ma_now, atr_now, fp, analog)
    return {
        "name": name,
        "score": round(sc, 1),
        "trend": round(tr, 1),
        "vol_pct": round(float(pct_df["vol"].iloc[-1])
                         if not pd.isna(pct_df["vol"].iloc[-1]) else 0.5, 3),
        "price": round(price_now, 4 if price_now < 10 else 2),
        "dev_m_val": round(dev_now, 2),
        "trail": trail,
        "factors": fp,
        "analog": analog,
        "reco": reco,
        "note": make_note(sc, tr),
        "_close": close,  # 相関リンク計算用(JSON出力前に削除)
    }


def make_reco(score: float, dev_m: float, price: float, ma_m: float,
              atr_v: float, factors: list, analog: dict) -> dict | None:
    """警戒ゾーン銘柄の推薦ポジションを機械生成する。
    方向=回帰方向、TP=中期MA、SL=類似局面90%tile逆行、ランク=手法ルールで降格。"""
    if score < ALERT_TH or atr_v <= 0 or ma_m <= 0 or abs(dev_m) < 0.5:
        return None
    short = dev_m > 0                     # MAより上に行き過ぎ → 回帰ショート
    fp = {f["key"]: f["pct"] for f in factors}
    adv = analog.get("adverse90") or analog.get("adverse") or 1.5
    sl = price + adv * atr_v if short else price - adv * atr_v
    reward = abs(price - ma_m)
    risk = abs(sl - price)
    rank, demotes = "A", []
    hot = ((fp.get("rsi_d", 0.5) >= 0.9 or fp.get("rsi_w", 0.5) >= 0.9)
           if short else
           (fp.get("rsi_d", 0.5) <= 0.1 or fp.get("rsi_w", 0.5) <= 0.1))
    if hot:                               # 勢い極大の逆張りは1段降格
        rank = "B"
        demotes.append("RSI過熱")
    if analog.get("n", 0) < 10:
        rank = "C" if rank == "B" else "B"
        demotes.append("類似局面サンプル少")
    elif analog.get("hit", 100) < 70:
        rank = "C" if rank == "B" else "B"
        demotes.append(f'類似局面回帰率{analog["hit"]}%')
    digits = 4 if price < 10 else (2 if price < 1000 else 0)
    return {
        "dir": "SHORT" if short else "LONG",
        "entry_lo": round(price if short else price - 0.4 * atr_v, digits),
        "entry_hi": round(price + 0.4 * atr_v if short else price, digits),
        "tp": round(ma_m, digits),
        "sl": round(sl, digits),
        "rr": round(reward / risk, 1) if risk > 0 else None,
        "rank": rank,
        "demotes": demotes,
        "cond": "H1/H4で角度転換+FT確認後" if rank != "C" else "基本見送り",
    }


def fmt_value(v: float, fmt: str) -> str:
    if fmt == "atr":
        return f"{v:+.1f}×ATR"
    if fmt == "corr":
        return f"{v:+.2f}"
    if fmt == "int":
        return f"{v:+,.0f}枚"
    if fmt == "resid":
        return f"{v:+,.1f} 乖離"
    if fmt == "num4":
        return f"{v:.4f}"
    return f"{v:.1f}"


def make_note(score: float, trend: float) -> str:
    d = "買い" if trend > 15 else ("売り" if trend < -15 else "中立")
    if score >= ALERT_TH:
        return f"{d}トレンド極大 / 反発警戒" if d != "中立" else "歪み極大 / 反発警戒"
    if score >= 50:
        return f"{d}トレンド / 歪み育成中"
    return "平常圏 / 見送り推奨"


def find_analogs(pct_df: pd.DataFrame, close, high, low, sma_m, a,
                 dev_m_now: float) -> dict:
    """現在の指紋と類似する過去局面を探し、その後5営業日の統計を返す。"""
    P = pct_df.dropna(thresh=min(5, pct_df.shape[1]))
    if len(P) < MINP:
        return {"n": 0}
    now = P.iloc[-1]
    hist = P.iloc[:-10]                              # 直近10日は除外
    sim = 1 - (hist - now).abs().mean(axis=1)        # L1ベース類似度
    cand = sim[sim >= SIM_TH].sort_index()
    picks, last = [], None
    for d in cand.index:                             # 連続日をクラスタ除去
        if last is None or (d - last).days > 5:
            picks.append(d)
        last = d
    if not picks:
        return {"n": 0}

    dirn = 1 if dev_m_now >= 0 else -1
    idx = close.index
    hits, days, adverse = [], [], []
    for d in picks:
        i = idx.get_loc(d)
        if i + 5 >= len(idx) or pd.isna(a.iloc[i]) or a.iloc[i] == 0:
            continue
        entry, atr0 = close.iloc[i], a.iloc[i]
        hit, worst = 0, 0.0
        for k in range(1, 6):
            j = i + k
            if dirn > 0:
                worst = max(worst, (high.iloc[j] - entry) / atr0)
                if low.iloc[j] <= sma_m.iloc[j]:
                    hit = k
                    break
            else:
                worst = max(worst, (entry - low.iloc[j]) / atr0)
                if high.iloc[j] >= sma_m.iloc[j]:
                    hit = k
                    break
        hits.append(1 if hit else 0)
        if hit:
            days.append(hit)
        adverse.append(worst)
    n = len(hits)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "hit": round(100 * sum(hits) / n),
        "avg_days": round(float(np.mean(days)), 1) if days else None,
        "adverse": round(float(np.mean(adverse)), 1),
        "adverse90": round(float(np.quantile(adverse, 0.9)), 1),
    }


# ------------------------------------------------------------------ 全体組み立て
def run(demo: bool) -> dict:
    ohlc: dict[str, pd.DataFrame] = {}
    if demo:
        print("[demo] 合成データで実行")
        ohlc = {
            "XAUUSD": demo_ohlc(1, 2600, trend_boost=0.004),
            "USDJPY": demo_ohlc(2, 152, trend_boost=0.001),
            "N225": demo_ohlc(3, 39000),
            "NAS100": demo_ohlc(4, 21000, trend_boost=0.002),
            "US30": demo_ohlc(5, 43000),
            "EURUSD": demo_ohlc(13, 1.08),
            "GBPUSD": demo_ohlc(14, 1.27, trend_boost=-0.001),
            "GBPJPY": demo_ohlc(15, 193, trend_boost=0.002),
            "BTCUSD": demo_ohlc(16, 95000, trend_boost=0.003),
            "ETHUSD": demo_ohlc(17, 3300),
        }
        refs = {"DX-Y.NYB": demo_ohlc(6, 105), "^TNX": demo_ohlc(7, 4.3),
                "^GVZ": demo_ohlc(8, 16), "^VIX": demo_ohlc(9, 15),
                "JPY=X": ohlc["USDJPY"], "^N225": ohlc["N225"],
                "^NDX": ohlc["NAS100"], "^DJI": ohlc["US30"],
                "BTC-USD": ohlc["BTCUSD"]}
        macro_fred = {"DFII10": demo_series(10, 2.0)}
        cot = {"GOLD - COMMODITY EXCHANGE": demo_series(11, 200000),
               "JAPANESE YEN - CHICAGO MERCANTILE": demo_series(12, -50000),
               "EURO FX - CHICAGO MERCANTILE": demo_series(18, 30000),
               "BRITISH POUND": demo_series(19, -20000),
               "BITCOIN": demo_series(20, 15000)}
    else:
        need = set()
        for cfg in CONFIG.values():
            need.add(cfg["ticker"])
            if cfg["vol"][0]:
                need.add(cfg["vol"][0])
            if cfg["macro"] and cfg["macro"][1] == "yf":
                need.add(cfg["macro"][0])
            if cfg["corr"]:
                need.add(cfg["corr"][0])
            if cfg.get("ratio"):
                need.add(cfg["ratio"][0])
        fetched = {t: fetch_ohlc(t) for t in sorted(need)}
        for name, cfg in CONFIG.items():
            if fetched.get(cfg["ticker"]) is None:
                print(f"[error] {name}: 主データ取得失敗、スキップ")
                continue
            ohlc[name] = fetched[cfg["ticker"]]
        refs = fetched
        macro_fred = {}
        for cfg in CONFIG.values():
            if cfg["macro"] and cfg["macro"][1] == "fred":
                sid = cfg["macro"][0]
                if sid not in macro_fred:
                    macro_fred[sid] = fetch_fred(sid)
        cot = {}
        for cfg in CONFIG.values():
            if cfg["cot"] and cfg["cot"][0] not in cot:
                cot[cfg["cot"][0]] = fetch_cot(cfg["cot"][0])

    symbols = []
    for name, cfg in CONFIG.items():
        if name not in ohlc:
            continue
        vol_t, vol_label = cfg["vol"]
        vol_s = None
        if vol_t:
            ref = refs.get(vol_t)
            vol_s = ref["close"] if isinstance(ref, pd.DataFrame) else ref
        macro_s, macro_label = None, ""
        if cfg["macro"]:
            mid, kind, macro_label = cfg["macro"]
            if kind == "fred":
                macro_s = macro_fred.get(mid)
            else:
                ref = refs.get(mid)
                macro_s = ref["close"] if isinstance(ref, pd.DataFrame) else ref
        corr_ref, corr_label = None, ""
        if cfg["corr"]:
            corr_ref = refs.get(cfg["corr"][0])
            corr_label = cfg["corr"][1]
        cot_s, cot_label = None, ""
        if cfg["cot"]:
            cot_s = cot.get(cfg["cot"][0])
            cot_label = cfg["cot"][1]
        ratio_s, ratio_label = None, ""
        if cfg.get("ratio"):
            ref = refs.get(cfg["ratio"][0])
            if ref is not None:
                ratio_s = ref["close"] if isinstance(ref, pd.DataFrame) else ref
                ratio_label = cfg["ratio"][1]
        symbols.append(build_symbol(name, ohlc[name], vol_s, vol_label,
                                    macro_s, macro_label, corr_ref, corr_label,
                                    cot_s, cot_label, ratio_s, ratio_label))

    # 相関リンク(マップ上の糸)
    links = []
    closes = {s["name"]: s.pop("_close") for s in symbols}
    for a_name, b_name in LINK_PAIRS:
        if a_name in closes and b_name in closes:
            ra = closes[a_name].pct_change()
            rb = closes[b_name].reindex(closes[a_name].index).ffill().pct_change()
            c = ra.rolling(20).corr(rb)
            p = pct_rank(c)
            if pd.isna(c.iloc[-1]) or pd.isna(p.iloc[-1]):
                continue
            links.append({"a": a_name, "b": b_name,
                          "corr": round(float(c.iloc[-1]), 2),
                          "broken": bool(p.iloc[-1] <= 0.12)})

    now_jst = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)
    day = now_jst.day
    eom = (now_jst + dt.timedelta(days=1)).month != now_jst.month
    symbols.sort(key=lambda s: -s["score"])
    return {
        "updated": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "alert_th": ALERT_TH,
        "sim_th": int(SIM_TH * 100),
        "symbols": symbols,
        "links": links,
        "today": {
            "gotobi": bool(day % 5 == 0 and day <= 30),
            "month_end": bool(eom),
            "weekday": ["月", "火", "水", "木", "金", "土", "日"][now_jst.weekday()],
        },
        "demo": bool(demo),
    }


# ------------------------------------------------------------------ 出力/通知
def notify_discord(data: dict, state_path: str) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        print("[info] DISCORD_WEBHOOK_URL 未設定、通知スキップ")
        return
    prev = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    crossed = [s for s in data["symbols"]
               if s["score"] >= ALERT_TH and prev.get(s["name"], 0) < ALERT_TH]
    for s in crossed:
        top = sorted(s["factors"],
                     key=lambda f: -abs(f["pct"] - 0.5))[:3]
        lines = [f'{f["label"]}: {f["value"]} ({round(f["pct"]*100)}%tile)'
                 for f in top]
        an = s["analog"]
        if an.get("n"):
            lines.append(f'類似局面 {an["n"]}回 → 5日内MA回帰 {an["hit"]}% '
                         f'/ 平均逆行 {an["adverse"]}×ATR')
        embed = {
            "title": f'🎯 {s["name"]} 警戒ゾーン突入 (歪み {s["score"]})',
            "description": s["note"] + "\n" + "\n".join(lines),
            "color": 0xFF5D5D,
        }
        try:
            requests.post(url, json={"embeds": [embed]}, timeout=15)
            print(f'[notify] {s["name"]} score={s["score"]}')
        except Exception as e:
            print(f"[warn] Discord通知失敗: {e}")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({s["name"]: s["score"] for s in data["symbols"]}, f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="合成データで実行")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args()

    data = run(demo=args.demo)
    if not data["symbols"]:
        print("[error] 有効な銘柄なし")
        return 1

    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] {args.out} を出力 "
          f"({', '.join(s['name'] + ':' + str(s['score']) for s in data['symbols'])})")

    if args.notify and not args.demo:
        notify_discord(data, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
