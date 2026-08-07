"""Train the household-lapse model, log to MLflow, and gate promotion on beating a baseline.

The gate is the point, not the model
------------------------------------
Any gradient-boosted tree will produce a plausible PR-AUC on a tabular problem with a 20-30% base
rate. That number means nothing on its own — it has to beat something a stakeholder would
otherwise have done for free.

The baseline here is **recency alone**: rank households by days since last purchase, intervene on
the top N. That is not a strawman; it is what a competent analyst does with a SQL query and no
model, and it is genuinely hard to beat on lapse problems because recency really is the dominant
signal. A model that cannot beat it is a model that costs money to maintain and adds nothing.

MLR-004 makes the gate binding: a model failing MLR-002 cannot take the `champion` alias. The
check runs before registration, not after, so a failing model never reaches a place someone could
promote it from by hand.

    python -m retail_lakehouse.ml.train
    python -m retail_lakehouse.ml.train --no-register    # evaluate without touching the registry
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from retail_lakehouse.ml import features as feat

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED = REPO_ROOT / "data" / "interim" / "seed-parquet" / "transaction_data.parquet"

# The margin is registered here, before the model is trained, so it cannot be chosen to fit the
# result. Ten percent relative improvement over the baseline is the bar: enough to be worth the
# maintenance cost of a model, small enough to be achievable if the extra features carry any
# signal at all.
REQUIRED_RELATIVE_LIFT = 0.10
RANDOM_STATE = 20260807


@dataclass(frozen=True)
class Evaluation:
    model_pr_auc: float
    baseline_pr_auc: float
    relative_lift: float
    model_roc_auc: float
    baseline_roc_auc: float
    n_train: int
    n_test: int
    lapse_rate_train: float
    lapse_rate_test: float
    passes_gate: bool


def _pr_auc(y_true: np.ndarray, score: np.ndarray) -> float:
    from sklearn.metrics import average_precision_score

    return float(average_precision_score(y_true, score))


def _roc_auc(y_true: np.ndarray, score: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y_true, score))


def train_and_evaluate(fs: feat.FeatureSet) -> tuple[object, Evaluation, pd.DataFrame]:
    import lightgbm as lgb
    from sklearn.model_selection import train_test_split

    # The households are split randomly here, and that is correct *because the temporal split
    # already happened upstream*: every feature comes from days 1-547 and every label from
    # 548-711, for every household. Splitting households randomly at this point cannot leak the
    # future, because no row contains the future. Doing the temporal split at this stage instead
    # would be the classic error.
    X_train, X_test, y_train, y_test = train_test_split(
        fs.X, fs.y, test_size=0.25, random_state=RANDOM_STATE, stratify=fs.y
    )

    model = lgb.LGBMClassifier(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=30,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=RANDOM_STATE,
        verbose=-1,
    )
    model.fit(X_train, y_train)
    model_score = np.asarray(model.predict_proba(X_test))[:, 1]

    # The baseline: recency alone, higher is more likely to lapse. No fitting, no parameters.
    baseline_score = X_test["recency_days"].to_numpy(dtype=float)

    model_pr = _pr_auc(y_test.to_numpy(), model_score)
    baseline_pr = _pr_auc(y_test.to_numpy(), baseline_score)
    lift = (model_pr - baseline_pr) / baseline_pr

    evaluation = Evaluation(
        model_pr_auc=model_pr,
        baseline_pr_auc=baseline_pr,
        relative_lift=lift,
        model_roc_auc=_roc_auc(y_test.to_numpy(), model_score),
        baseline_roc_auc=_roc_auc(y_test.to_numpy(), baseline_score),
        n_train=len(X_train),
        n_test=len(X_test),
        lapse_rate_train=float(y_train.mean()),
        lapse_rate_test=float(y_test.mean()),
        passes_gate=lift >= REQUIRED_RELATIVE_LIFT,
    )

    importance = (
        pd.DataFrame(
            {"feature": fs.feature_names, "gain": model.booster_.feature_importance("gain")}
        )
        .sort_values("gain", ascending=False)
        .reset_index(drop=True)
    )
    return model, evaluation, importance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-register", action="store_true", help="skip the model registry")
    # Required, not defaulted. ENV-001 caught the default `dng_dev` here and it was right to:
    # writing to a model registry is exactly the operation where a convenient default lets you
    # register a dev experiment into prod without noticing. The operator states the environment.
    parser.add_argument(
        "--catalog",
        required=True,
        help="environment catalog to register into, e.g. the dev or test catalog",
    )
    args = parser.parse_args()

    if not SEED.exists():
        print(f"Seed not found at {SEED}. Run: python3 scripts/fetch_data.py", file=sys.stderr)
        return 1

    transactions = pd.read_parquet(
        SEED,
        columns=[
            "household_key",
            "BASKET_ID",
            "DAY",
            "PRODUCT_ID",
            "QUANTITY",
            "SALES_VALUE",
            "STORE_ID",
            "RETAIL_DISC",
            "COUPON_DISC",
        ],
    )
    print(f"seed: {len(transactions):,} real transaction lines")

    # MLR-006 before anything else. A leaking feature set makes every number below meaningless,
    # so it is checked before the numbers exist rather than after they look good.
    feat.assert_no_leakage(transactions)
    print("MLR-006: no feature reads past the split boundary")

    fs = feat.build(transactions)
    print(
        f"features: {len(fs):,} households, {len(fs.feature_names)} features, "
        f"lapse rate {fs.lapse_rate:.1%}"
    )

    model, ev, importance = train_and_evaluate(fs)

    print(f"\n{'metric':<24}{'model':>10}{'baseline':>12}")
    print("-" * 46)
    print(f"{'PR-AUC':<24}{ev.model_pr_auc:>10.4f}{ev.baseline_pr_auc:>12.4f}")
    print(f"{'ROC-AUC':<24}{ev.model_roc_auc:>10.4f}{ev.baseline_roc_auc:>12.4f}")
    print(
        f"\nrelative lift on PR-AUC  {ev.relative_lift:+.1%}   (gate: >= {REQUIRED_RELATIVE_LIFT:.0%})"
    )
    print(f"gate: {'PASS' if ev.passes_gate else 'FAIL'}")

    print(f"\n{'top features by gain':<28}")
    for _, row in importance.head(8).iterrows():
        print(f"  {row['feature']:<26}{row['gain']:>12,.0f}")

    try:
        import mlflow

        mlflow.set_tracking_uri("databricks")
        mlflow.set_experiment(f"/Users/{_current_user()}/dng-household-lapse")

        with mlflow.start_run() as run:
            mlflow.log_params(
                {
                    "split_day": feat.SPLIT_DAY,
                    "required_relative_lift": REQUIRED_RELATIVE_LIFT,
                    "random_state": RANDOM_STATE,
                    "n_features": len(fs.feature_names),
                    "evaluation_data": "real seed only (MLR-003)",
                }
            )
            mlflow.log_metrics({k: v for k, v in asdict(ev).items() if isinstance(v, (int, float))})
            mlflow.log_dict(importance.to_dict("records"), "feature_importance.json")

            if ev.passes_gate and not args.no_register:
                # MLR-004: registration happens inside the gate, so a failing model never reaches
                # a place from which someone could promote it by hand.
                mlflow.set_registry_uri("databricks-uc")
                mlflow.sklearn.log_model(
                    model,
                    name="model",
                    registered_model_name=f"{args.catalog}.gold.household_lapse",
                )
                print(f"\nregistered: {args.catalog}.gold.household_lapse")
            elif not ev.passes_gate:
                mlflow.set_tag("gate", "failed")
                print(
                    "\nnot registered: the model did not beat the baseline by the required margin"
                )

            print(f"mlflow run: {run.info.run_id}")

    except Exception as exc:
        # Logging failing must not silently discard the evaluation. Print it and carry the
        # non-zero exit only if the gate itself failed.
        print(f"\nMLflow logging unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)

    Path(REPO_ROOT / "data" / "ml").mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "data" / "ml" / "evaluation.json").write_text(
        json.dumps(asdict(ev), indent=2) + "\n", encoding="utf-8"
    )
    return 0 if ev.passes_gate else 2


def _current_user() -> str:
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient().current_user.me().user_name or "unknown"


if __name__ == "__main__":
    sys.exit(main())
