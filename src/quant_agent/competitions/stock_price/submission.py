"""Explicit server packaging for the audited stock-price-predictor mismatch.

The official sample has 364 inputs; the scoring service accepts IDs 0..364.
The extra prediction has no invented Date/OHLC and uses an explicit prior.

@author cylovelife
"""

import numpy as np
import pandas as pd


def server_rows(source: pd.DataFrame, *, missing_prior: int, complement: bool) -> pd.DataFrame:
    """Keep source alignment; make missing-input and output encoding explicit."""
    if (
        list(source) != ["Id", "Target"]
        or len(source) != 364
        or not np.array_equal(source.Id.to_numpy(), np.arange(364))
        or not np.isin(source.Target, [0, 1]).all()
        or missing_prior not in (0, 1)
        or not isinstance(complement, bool)
    ):
        raise ValueError("invalid original sample alignment or binary prediction")
    values = np.append(source.Target.to_numpy(dtype=int), missing_prior)
    if complement:
        values = 1 - values
    return pd.DataFrame({"ID": np.arange(365), "Target": values.astype(int)})
