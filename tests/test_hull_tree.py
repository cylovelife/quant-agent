"""Limited tree interaction learning and shared temporal/mapping regression tests.

@author cylovelife
"""

import numpy as np
import pandas as pd

from quant_agent.competitions.hull.ridge import TARGET, fit_ridge, run_window
from quant_agent.competitions.hull.tree import TREE_PARAMETERS, fit_tree


def nonlinear_panel(n=1800):
    rng = np.random.default_rng(58)
    x = rng.uniform(-1, 1, (n, 2))
    y = 0.01 * ((x[:, 0] > 0) & (x[:, 1] > 0)) + rng.normal(0, 0.0005, n)
    return pd.DataFrame(
        {
            "date_id": np.arange(n),
            "M1": x[:, 0],
            "M2": x[:, 1],
            "E1": np.nan,
            TARGET: y,
            "forward_returns": y + 0.0002,
            "risk_free_rate": 0.0001,
        }
    )


def test_tree_learns_known_threshold_interaction_with_same_preprocessing():
    panel = nonlinear_panel()
    features = ("M1", "M2", "E1")
    train, valid = panel.iloc[:1200], panel.iloc[1200:]
    ridge, tree = fit_ridge(train, features), fit_tree(train, features)
    a = ridge.predict(valid[list(features)])
    b = tree.predict(valid[list(features)])
    assert np.mean((b - valid[TARGET]) ** 2) < np.mean((a - valid[TARGET]) ** 2) * 0.3
    np.testing.assert_array_equal(
        ridge.named_steps["features"].transform(valid[list(features)]),
        tree.named_steps["features"].transform(valid[list(features)]),
    )
    assert tree.named_steps["tree"].n_iter_ == 80
    assert tree.named_steps["tree"].get_params()["early_stopping"] is False
    assert tree.named_steps["tree"].n_features_in_ == 6
    assert (
        tree.named_steps["tree"].get_params()["max_leaf_nodes"] == TREE_PARAMETERS["max_leaf_nodes"]
    )


def test_tree_calibration_is_label_isolated_and_rowwise_equivalent():
    panel = nonlinear_panel(800)
    features = ("M1", "M2", "E1")
    a = run_window(panel, features, 600, 700, blocks=2, block_rows=80, fit_model=fit_tree)
    altered = panel.copy()
    altered.loc[altered.date_id >= 600, TARGET] = 99
    altered.loc[altered.date_id >= 700, list(features)] = 1000
    b = run_window(altered, features, 600, 700, blocks=2, block_rows=80, fit_model=fit_tree)
    np.testing.assert_array_equal(a.predictions.forecast, b.predictions.forecast)
    np.testing.assert_array_equal(a.predictions.allocation, b.predictions.allocation)
    assert a.mapping == b.mapping
    assert a.oof.label_end_id.max() < 600
    assert a.model_fits == 3
    rows = panel.loc[panel.date_id.isin(a.predictions.date_id), list(features)]
    np.testing.assert_array_equal(
        [a.allocate(row) for _, row in rows.groupby(rows.index)], a.predictions.allocation
    )
