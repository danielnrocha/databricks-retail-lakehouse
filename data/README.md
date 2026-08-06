# Data

## Seed dataset — attribution required

**dunnhumby — *The Complete Journey***
Mendeley Data, DOI [10.17632/7myy93ym6k.1](https://data.mendeley.com/datasets/7myy93ym6k/1)
Licence: **CC BY 4.0** — https://creativecommons.org/licenses/by/4.0/

Approximately two years of basket-level transactions from 2,500 frequent-shopper households at a
US grocery retailer, across eight relational tables.

The dataset is **not vendored** into this repository. It is roughly 100 MB of CSV, it belongs in a
Unity Catalog volume rather than in git history, and committing it would make every future clone
pay for it forever. `make data` fetches it into `data/raw/`.

## Directory contract

| Path | Committed? | Contents |
|---|---|---|
| `data/raw/` | No | The fetched dunnhumby CSVs, untouched |
| `data/interim/` | No | Profiling output and fitted distributions used by the generator |
| `data/generated/` | No | Amplifier output staged for upload to the landing volume |
| `data/fixtures/` | **Yes** | Small golden fixtures asserted against by tests |

`data/fixtures/` is committed on purpose. A golden fixture is a *contract*: when it changes, the
diff must be reviewable. A fixture you cannot read in a pull request is not a fixture, it is a
binary blob nobody audits.

## Known modelling constraint

`transaction_data.DAY` is a relative integer (1–711) with no calendar anchor. `DAY = 1` is anchored
to a Monday so that day-of-week structure — which is genuinely present in the data — survives.
**Calendar seasonality is unavailable and must not be inferred.** Any feature or dashboard implying
month, quarter, or holiday effects would be fabricating signal. See
[ADR-0003](../docs/adr/ADR-0003-dataset-selection.md).
