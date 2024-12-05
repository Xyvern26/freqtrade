"""
Functions to convert orderflow data from public_trades
"""

import logging
import time
from collections import OrderedDict
from datetime import datetime

import numpy as np
import pandas as pd

from freqtrade.constants import DEFAULT_ORDERFLOW_COLUMNS, Config
from freqtrade.enums import RunMode
from freqtrade.exceptions import DependencyException


logger = logging.getLogger(__name__)

ADDED_COLUMNS = [
    "trades",
    "orderflow",
    "imbalances",
    "stacked_imbalances_bid",
    "stacked_imbalances_ask",
    "max_delta",
    "min_delta",
    "bid",
    "ask",
    "delta",
    "total_trades",
]


def _init_dataframe_with_trades_columns(dataframe: pd.DataFrame):
    """
    Populates a dataframe with trades columns
    :param dataframe: Dataframe to populate
    """
    # Initialize columns with appropriate dtypes
    for column in ADDED_COLUMNS:
        dataframe[column] = np.nan

    # Set columns to object type
    for column in (
        "trades",
        "orderflow",
        "imbalances",
        "stacked_imbalances_bid",
        "stacked_imbalances_ask",
    ):
        dataframe[column] = dataframe[column].astype(object)


def _calculate_ohlcv_candle_start_and_end(df: pd.DataFrame, timeframe: str):
    from freqtrade.exchange import timeframe_to_next_date, timeframe_to_resample_freq

    timeframe_frequency = timeframe_to_resample_freq(timeframe)
    # calculate ohlcv candle start and end
    if df is not None and not df.empty:
        df["datetime"] = pd.to_datetime(df["date"], unit="ms")
        df["candle_start"] = df["datetime"].dt.floor(timeframe_frequency)
        # used in _now_is_time_to_refresh_trades
        df["candle_end"] = df["candle_start"].apply(
            lambda candle_start: timeframe_to_next_date(timeframe, candle_start)
        )
        df.drop(columns=["datetime"], inplace=True)


def populate_dataframe_with_trades(
    cached_grouped_trades: OrderedDict[tuple[datetime, datetime], pd.DataFrame],
    config: Config,
    dataframe: pd.DataFrame,
    trades: pd.DataFrame,
) -> tuple[pd.DataFrame, OrderedDict[tuple[datetime, datetime], pd.DataFrame]]:
    """
    Populates a dataframe with trades
    :param dataframe: Dataframe to populate
    :param trades: Trades to populate with
    :return: Dataframe with trades populated
    """
    from freqtrade.exchange import timeframe_to_next_date

    timeframe = config["timeframe"]
    config_orderflow = config["orderflow"]

    # create columns for trades
    _init_dataframe_with_trades_columns(dataframe)
    if trades is None or trades.empty:
        return dataframe, cached_grouped_trades

    try:
        start_time = time.time()
        # calculate ohlcv candle start and end
        _calculate_ohlcv_candle_start_and_end(trades, timeframe)

        # get date of earliest max_candles candle
        max_candles = config_orderflow["max_candles"]
        start_date = dataframe.tail(max_candles).date.iat[0]
        # slice of trades that are before current ohlcv candles to make groupby faster
        trades = trades.loc[trades["candle_start"] >= start_date]
        trades.reset_index(inplace=True, drop=True)

        # group trades by candle start
        trades_grouped_by_candle_start = trades.groupby("candle_start", group_keys=False)

        candle_start: datetime
        for candle_start, trades_grouped_df in trades_grouped_by_candle_start:
            is_between = candle_start == dataframe["date"]
            if is_between.any():
                candle_next = timeframe_to_next_date(timeframe, candle_start)
                if candle_next not in trades_grouped_by_candle_start.groups:
                    logger.warning(
                        f"candle at {candle_start} with {len(trades_grouped_df)} trades "
                        f"might be unfinished, because no finished trades at {candle_next}"
                    )

                # Use caching mechanism
                if (candle_start, candle_next) in cached_grouped_trades:
                    cache_entry = cached_grouped_trades[(candle_start, candle_next)]
                    # dataframe.loc[is_between] = cache_entry # doesn't take, so we need workaround:
                    # Create a dictionary of the column values to be assigned
                    update_dict = {c: cache_entry[c].iat[0] for c in cache_entry.columns}
                    # Assign the values using the update_dict
                    dataframe.loc[is_between, update_dict.keys()] = pd.DataFrame(
                        [update_dict], index=dataframe.loc[is_between].index
                    )
                    continue

                # there can only be one row with the same date
                index = dataframe.index[is_between][0]
                dataframe.at[index, "trades"] = trades_grouped_df.drop(
                    columns=["candle_start", "candle_end"]
                ).to_dict(orient="records")

                # Calculate orderflow for each candle
                orderflow = trades_to_volumeprofile_with_total_delta_bid_ask(
                    trades_grouped_df, scale=config_orderflow["scale"]
                )
                dataframe.at[index, "orderflow"] = orderflow.to_dict(orient="index")
                # orderflow_series.loc[[index]] = [orderflow.to_dict(orient="index")]
                # Calculate imbalances for each candle's orderflow
                imbalances = trades_orderflow_to_imbalances(
                    orderflow,
                    imbalance_ratio=config_orderflow["imbalance_ratio"],
                    imbalance_volume=config_orderflow["imbalance_volume"],
                )
                dataframe.at[index, "imbalances"] = imbalances.to_dict(orient="index")

                stacked_imbalance_range = config_orderflow["stacked_imbalance_range"]
                dataframe.at[index, "stacked_imbalances_bid"] = stacked_imbalance_bid(
                    imbalances, stacked_imbalance_range=stacked_imbalance_range
                )

                dataframe.at[index, "stacked_imbalances_ask"] = stacked_imbalance_ask(
                    imbalances, stacked_imbalance_range=stacked_imbalance_range
                )

                bid = np.where(
                    trades_grouped_df["side"].str.contains("sell"), trades_grouped_df["amount"], 0
                )

                ask = np.where(
                    trades_grouped_df["side"].str.contains("buy"), trades_grouped_df["amount"], 0
                )
                deltas_per_trade = ask - bid
                min_delta = deltas_per_trade.cumsum().min()
                max_delta = deltas_per_trade.cumsum().max()
                dataframe.at[index, "max_delta"] = max_delta
                dataframe.at[index, "min_delta"] = min_delta

                dataframe.at[index, "bid"] = bid.sum()
                dataframe.at[index, "ask"] = ask.sum()
                dataframe.at[index, "delta"] = (
                    dataframe.at[index, "ask"] - dataframe.at[index, "bid"]
                )
                dataframe.at[index, "total_trades"] = len(trades_grouped_df)

                # Cache the result
                cached_grouped_trades[(candle_start, candle_next)] = dataframe.loc[
                    is_between
                ].copy()

                # Maintain cache size
                if (
                    config.get("runmode") in (RunMode.DRY_RUN, RunMode.LIVE)
                    and len(cached_grouped_trades) > config_orderflow["cache_size"]
                ):
                    cached_grouped_trades.popitem(last=False)
            else:
                logger.debug(f"Found NO candles for trades starting with {candle_start}")
        logger.debug(f"trades.groups_keys in {time.time() - start_time} seconds")

    except Exception as e:
        logger.exception("Error populating dataframe with trades")
        raise DependencyException(e)

    return dataframe, cached_grouped_trades


def trades_to_volumeprofile_with_total_delta_bid_ask(
    trades: pd.DataFrame, scale: float
) -> pd.DataFrame:
    """
    :param trades: dataframe
    :param scale: scale aka bin size e.g. 0.5
    :return: trades binned to levels according to scale aka orderflow
    """
    df = pd.DataFrame([], columns=DEFAULT_ORDERFLOW_COLUMNS)
    # create bid, ask where side is sell or buy
    df["bid_amount"] = np.where(trades["side"].str.contains("sell"), trades["amount"], 0)
    df["ask_amount"] = np.where(trades["side"].str.contains("buy"), trades["amount"], 0)
    df["bid"] = np.where(trades["side"].str.contains("sell"), 1, 0)
    df["ask"] = np.where(trades["side"].str.contains("buy"), 1, 0)
    # round the prices to the nearest multiple of the scale
    df["price"] = ((trades["price"] / scale).round() * scale).astype("float64").values
    if df.empty:
        df["total"] = np.nan
        df["delta"] = np.nan
        return df

    df["delta"] = df["ask_amount"] - df["bid_amount"]
    df["total_volume"] = df["ask_amount"] + df["bid_amount"]
    df["total_trades"] = df["ask"] + df["bid"]

    # group to bins aka apply scale
    df = df.groupby("price").sum(numeric_only=True)
    return df


def trades_orderflow_to_imbalances(df: pd.DataFrame, imbalance_ratio: int, imbalance_volume: int):
    """
    :param df: dataframes with bid and ask
    :param imbalance_ratio: imbalance_ratio e.g. 3
    :param imbalance_volume: imbalance volume e.g. 10
    :return: dataframe with bid and ask imbalance
    """
    bid = df.bid
    # compares bid and ask diagonally
    ask = df.ask.shift(-1)
    bid_imbalance = (bid / ask) > (imbalance_ratio)
    # overwrite bid_imbalance with False if volume is not big enough
    bid_imbalance_filtered = np.where(df.total_volume < imbalance_volume, False, bid_imbalance)
    ask_imbalance = (ask / bid) > (imbalance_ratio)
    # overwrite ask_imbalance with False if volume is not big enough
    ask_imbalance_filtered = np.where(df.total_volume < imbalance_volume, False, ask_imbalance)
    dataframe = pd.DataFrame(
        {"bid_imbalance": bid_imbalance_filtered, "ask_imbalance": ask_imbalance_filtered},
        index=df.index,
    )

    return dataframe


def stacked_imbalance(
    df: pd.DataFrame, label: str, stacked_imbalance_range: int, should_reverse: bool
):
    """
    y * (y.groupby((y != y.shift()).cumsum()).cumcount() + 1)
    https://stackoverflow.com/questions/27626542/counting-consecutive-positive-values-in-python-pandas-array
    """
    imbalance = df[f"{label}_imbalance"]
    int_series = pd.Series(np.where(imbalance, 1, 0))
    stacked = int_series * (
        int_series.groupby((int_series != int_series.shift()).cumsum()).cumcount() + 1
    )

    max_stacked_imbalance_idx = stacked.index[stacked >= stacked_imbalance_range]
    stacked_imbalance_price = np.nan
    if not max_stacked_imbalance_idx.empty:
        idx = (
            max_stacked_imbalance_idx[0]
            if not should_reverse
            else np.flipud(max_stacked_imbalance_idx)[0]
        )
        stacked_imbalance_price = imbalance.index[idx]
    return stacked_imbalance_price


def stacked_imbalance_ask(df: pd.DataFrame, stacked_imbalance_range: int):
    return stacked_imbalance(df, "ask", stacked_imbalance_range, should_reverse=True)


def stacked_imbalance_bid(df: pd.DataFrame, stacked_imbalance_range: int):
    return stacked_imbalance(df, "bid", stacked_imbalance_range, should_reverse=False)
