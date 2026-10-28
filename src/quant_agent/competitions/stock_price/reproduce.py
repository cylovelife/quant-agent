"""Reproduce the calendar submission without re-consuming holdout scores.

@author cylovelife
"""

import argparse
import hashlib
import json
from pathlib import Path

from quant_agent.competitions.stock_price.baseline import build_features, fit_predict
from quant_agent.competitions.stock_price.data import load_dataset
from quant_agent.competitions.stock_price.submission import server_rows


def reproduce(raw: Path, contract: Path, output: Path, *, complement: bool) -> dict:
    """Refit only the fixed calendar model; never upload or evaluate holdout."""
    if output.exists():
        raise FileExistsError(output)
    data = load_dataset(raw, contract)
    features = build_features(data.prices).dropna()
    names = list(features.columns.drop("Date"))
    panel = data.labels.merge(features, on="Date", validate="one_to_one")
    cutoff = data.prices.Date.iloc[-len(data.sample)]
    train = panel.loc[panel.label_end < cutoff]
    inference = features.loc[features.Date >= cutoff]
    if len(inference) != len(data.sample):
        raise ValueError("missing or reordered inference rows")
    prediction, _ = fit_predict("calendar", train, inference, names)
    source = data.sample.assign(Target=prediction)
    prior = int(train.Target.mean() >= 0.5)
    rows = server_rows(source, missing_prior=prior, complement=complement)
    with output.open("x", newline="") as stream:
        rows.to_csv(stream, index=False, lineterminator="\n")
    return {
        "rows": len(rows),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "original_input_rows": len(source),
        "missing_input_raw_prior": prior,
        "public_lb_adaptive_complement": complement,
        "holdout_evaluated": False,
        "uploaded": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--public-lb-complement",
        action="store_true",
        help="Explicitly reproduce the historically Public-LB-selected output encoding",
    )
    args = parser.parse_args()
    result = reproduce(args.raw, args.contract, args.output, complement=args.public_lb_complement)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
