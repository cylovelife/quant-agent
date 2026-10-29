"""Single-thread limited histogram boosting with exactly the Ridge preprocessing.

@author cylovelife
"""

import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.pipeline import Pipeline

from quant_agent.competitions.hull.ridge import fit_forecaster

TREE_PARAMETERS = {
    "loss": "squared_error",
    "learning_rate": 0.05,
    "max_iter": 80,
    "max_leaf_nodes": 7,
    "max_depth": 3,
    "min_samples_leaf": 64,
    "l2_regularization": 10.0,
    "max_bins": 63,
    "categorical_features": None,
    "early_stopping": False,
    "random_state": 0,
}


def fit_tree(train: pd.DataFrame, features: tuple[str, ...]) -> Pipeline:
    return fit_forecaster(
        train, features, HistGradientBoostingRegressor(**TREE_PARAMETERS), name="tree"
    )
