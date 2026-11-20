import dataclasses

import pandas as pd
import pytest

from src import config as C
from src.generate_data import generate, write_files
from src.money import D
from src.reconcile import reconcile
from src.validate_data import validate
from src.verify import verify_against_manifest


@pytest.fixture(scope="module")
def gen():
    return generate(C.SEED, plant_defects=True)


@pytest.fixture(scope="module")
def res(gen):
    return reconcile(gen.platform, gen.bank_lines, gen.bank_batches)


def test_generator_constraints_hold(gen):
    assert len(validate(gen)) > 100


def test_findings_match_ground_truth_with_zero_false_positives(res, gen):
    v = verify_against_manifest(res, gen.manifest)
    assert (v.status == "PASS").all(), v.to_string()


def test_bridge_closes_to_zero(res):
    assert res.bridge.unexplained == 0 and res.bridge.unclassified == 0


def test_realistic_volumes(gen, res):
    c = gen.manifest["row_counts"]
    assert 1400 <= c["platform"] <= 1500 and 40 <= c["failed"] <= 60 and 60 <= c["refunds"] <= 70
    assert 80 <= res.bridge.counts["in_transit"] <= 130          # a population, not a lone item


def test_decoys_are_present_in_the_data_and_unflagged(gen, res):
    d = gen.manifest["decoys"]
    assert len(d["refunds_of_september_sales"]) >= 5 and len(d["refunds_of_late_october_sales"]) >= 1
    assert len(d["same_amount_same_day_payments"]) >= 3
    flagged = set(res.exceptions.ref)
    assert not flagged & set(d["refunds_of_september_sales"] + d["refunds_of_late_october_sales"])
    assert not flagged & {t for g in d["same_amount_same_day_payments"] for t in g}


def test_rounding_is_a_visible_aggregate_but_every_line_is_within_tolerance(res):
    s = res.bridge.rounding_stats
    assert s["max_abs"] <= C.ROUNDING_TOLERANCE and s["nonzero_lines"] >= 30
    assert D("0.05") <= res.bridge.rounding <= D("0.50")


def test_utc_cutoff_would_give_a_different_answer(gen, res):
    naive = gen.platform[(gen.platform.status == "SUCCESS") & gen.platform.created_at_utc.str.startswith("2026-10")]
    naive_total = sum((D(a) if t == "PAYMENT" else -D(a)) for a, t in zip(naive.amount, naive.txn_type))
    assert naive_total != res.bridge.platform_total


def test_clean_baseline_has_no_exceptions_and_a_zero_residual():
    g = generate(C.SEED, plant_defects=False)
    validate(g)
    r = reconcile(g.platform, g.bank_lines, g.bank_batches)
    assert r.exceptions.empty and r.bridge.unexplained == 0
    assert (verify_against_manifest(r, g.manifest).status == "PASS").all()


def test_same_seed_same_data_and_defects_do_not_change_the_base():
    a, b = generate(7), generate(7)
    assert a.platform.equals(b.platform) and a.bank_lines.equals(b.bank_lines)
    clean = generate(7, plant_defects=False)
    shared = set(clean.platform.txn_id)
    assert shared <= set(a.platform.txn_id) and len(a.platform) == len(clean.platform) + 1   # + boundary txn


def test_validator_catches_a_corrupted_dataset(gen):
    failed = gen.platform[gen.platform.status == "FAILED"].txn_id.iloc[0]
    row = pd.DataFrame([dict(batch_id="BATCH-20261015", line_ref="LN-BAD", txn_ref=failed,
                             settlement_date="2026-10-15", settled_amount="1.00", currency="INR")])
    bad = dataclasses.replace(gen, bank_lines=pd.concat([gen.bank_lines, row], ignore_index=True))
    with pytest.raises(AssertionError):
        validate(bad)


def test_csv_round_trip_gives_identical_results(gen, res, tmp_path):
    write_files(gen, tmp_path)
    rd = lambda n: pd.read_csv(tmp_path / n, dtype=str, keep_default_na=False)
    r2 = reconcile(rd("platform_transactions.csv"), rd("bank_settlement_lines.csv"), rd("bank_batches.csv"))
    assert r2.bridge.as_frame().equals(res.bridge.as_frame())
    assert r2.exceptions.ref.tolist() == res.exceptions.ref.tolist()
