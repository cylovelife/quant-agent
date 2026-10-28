"""Two-part rolling competition windows with actual label-end purging.

@author cylovelife
"""

import pandas as pd


def purged_window(
    panel: pd.DataFrame, start: str, end_exclusive: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep entire dates; labels must mature strictly before the next boundary."""
    if "Date" not in panel or "label_end" not in panel:
        raise ValueError("Date and actual label_end are required")
    dates = pd.to_datetime(panel.Date, errors="coerce")
    ends = pd.to_datetime(panel.label_end, errors="coerce")
    begin, end = pd.Timestamp(start), pd.Timestamp(end_exclusive)
    if begin >= end or dates.isna().any() or ends.isna().any() or (ends <= dates).any():
        raise ValueError("invalid dates or label end")
    fit = panel.loc[(dates < begin) & (ends < begin)].copy()
    valid = panel.loc[(dates >= begin) & (dates < end) & (ends < end)].copy()
    if fit.empty or valid.empty:
        raise ValueError("empty training or evaluation window")
    return fit, valid
