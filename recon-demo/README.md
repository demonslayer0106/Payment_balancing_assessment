# Payment settlement reconciliation demo

Explains why a payments platform's transaction records and the bank's batch settlements do not agree at month end,
and proves the explanation with a reconciliation bridge.

```
Bank Oct total = Platform Oct total - In transit + Carry-in + Rounding + Duplicates + Unexplained
```

## Run
```bash
pip install -r requirements.txt
python -m src.generate_data --out data        # writes CSVs + ground_truth.json (add --clean for no defects)
python -m pytest                              # 80 tests, incl. edge cases
python -m streamlit run app.py                # dashboard
```
Streamlit Community Cloud deploys from a GitHub repo; from Colab/Kaggle, run Streamlit in the background and expose it
through a tunnel (e.g. cloudflared or ngrok).

## Layout
| Path | Purpose |
|---|---|
| `src/config.py` | period, lookback, IST, tolerance, seed |
| `src/money.py`, `src/calendar_utils.py` | Decimal money (floats rejected), IST + business-day arithmetic |
| `src/generate_data.py` | synthetic data, planted defects, decoys, ground-truth manifest |
| `src/validate_data.py` | generator constraints (data is reconcilable *before* defects are hunted) |
| `src/reconcile.py` | normalise -> dedupe -> match -> bucket -> bridge; refund integrity; batch controls |
| `src/verify.py` | planted vs found, plus decoy and false-positive checks |
| `app.py` | Streamlit dashboard |
| `tests/` | unit, edge-case, property (fuzz) and end-to-end tests |

## The four planted discrepancies
1. **Next-month settlement**: a population of 29-31 Oct sales settling 2-3 Nov, plus a 19:00 UTC / 00:30 IST boundary sale.
2. **Rounding**: platform keeps sub-paisa amounts, bank rounds half-up per line; each line is within 0.005, the total drifts.
3. **Duplicate**: a bank line repeated in a later batch with a new `line_ref`.
4. **Orphan refund**: `original_txn_id` is a one-character corruption of a real ID. Cash agrees on both sides, so it is reported as an exposure, not a bridge item.

## Edge cases covered by tests
Late and too-early settlement, tolerance boundary (0.005 passes, 0.006 fails), aggregate rounding drift, UTC-vs-IST cutoff,
pre-window sales, duplicates in the same/different batch and across month end, platform duplicates, refunds of September and
in-transit sales (not orphans), refunds of failed payments, over-refunds, refunds before the original, wrong refund sign,
bank-only / platform-only / settled-but-failed items, out-of-range bank lines, control-total mismatches, and a 30-seed fuzz test
asserting the bridge always accounts for every unit.

## Limits
Synthetic data written by the same author as the detector: it proves internal consistency, not accuracy on real bank files.
Single currency, gross settlement, no PENDING status, no holiday calendar.

## References
Goldberg (1991), *What Every Computer Scientist Should Know About Floating-Point Arithmetic*, ACM Computing Surveys.
Fowler (2002), *Patterns of Enterprise Application Architecture* (Money pattern). ISO 20022 `camt.053` for statement structure.
The bridge format itself comes from accounting practice rather than research literature.
