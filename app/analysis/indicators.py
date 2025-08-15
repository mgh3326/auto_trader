import pandas as pd
import ta


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    macd_ind = ta.trend.MACD(
        close=df["close"], window_slow=26, window_fast=12, window_sign=9
    )
    df["macd"] = macd_ind.macd()  # MACD 선
    df["macd_signal"] = macd_ind.macd_signal()  # 시그널 선
    df["macd_diff"] = macd_ind.macd_diff()  # 히스토그램(교차값)

    # ② RSI(14) -----------------------------------------------
    df["rsi14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

    # ③ Bollinger Bands(20, 2σ) -------------------------------
    bb = ta.volatility.BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = df["bb_upper"] - df["bb_lower"]  # 밴드 폭

    # ④ Stochastic Fast ---------------------------------------
    sto_fast = ta.momentum.StochasticOscillator(
        high=df["high"], low=df["low"], close=df["close"], window=14, smooth_window=3
    )
    df["stoch_k"] = sto_fast.stoch()
    df["stoch_d"] = sto_fast.stoch_signal()  # 3일 EMA

    # …볼밴, 스토캐스틱 등 추가
    return df
