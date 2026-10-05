"""Cut snapshots by the time a bar finished, not its date/start label."""
import pandas as pd


def closed_bars_as_of(frame: pd.DataFrame, timeframe: str, as_of: str) -> pd.DataFrame:
    if frame is None or frame.empty or "datetime" not in frame:
        return frame
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is not None:
        cutoff = cutoff.tz_convert("Asia/Shanghai").tz_localize(None)
    stamps = pd.to_datetime(frame["datetime"])
    if stamps.dt.tz is not None:
        stamps = stamps.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    if timeframe == "1d":
        close_time = stamps.dt.normalize() + pd.Timedelta(hours=15)
    elif frame.attrs.get("timestamp_semantics", "end") == "start":
        close_time = stamps + pd.Timedelta(minutes=int(timeframe.removesuffix("m")))
    else:
        close_time = stamps
    eligible = close_time <= cutoff
    if "is_closed" in frame:
        eligible &= frame["is_closed"].fillna(False).astype(bool)
    return frame.loc[eligible].copy().reset_index(drop=True)
