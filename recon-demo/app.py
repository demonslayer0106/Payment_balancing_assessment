"""Streamlit dashboard.  Run:  python -m streamlit run app.py"""
import pandas as pd
import streamlit as st

from src import config as C
from src.generate_data import generate
from src.money import ZERO, fmt
from src.reconcile import reconcile
from src.verify import verify_against_manifest

st.set_page_config(page_title="Settlement Reconciliation", layout="wide")


def show(df: pd.DataFrame, cols=None):
    out = (df if cols is None else df[cols]).copy()
    for c in out.columns:                      # Decimals / dates -> text so Arrow never chokes on them
        if out[c].dtype == object:
            out[c] = out[c].map(lambda v: "" if v is None else str(v))
    st.dataframe(out, hide_index=True)


@st.cache_data(show_spinner=False)
def load_synthetic(seed: int, plant: bool):
    g = generate(seed, plant)
    return g.platform, g.bank_lines, g.bank_batches, g.manifest


@st.cache_data(show_spinner=False)
def run_recon(platform, lines, batches):
    return reconcile(platform, lines, batches)


# ---------------------------------------------------------------- sidebar: data source
st.sidebar.header("Data source")
mode = st.sidebar.radio("Use", ["Synthetic demo data", "Upload CSV files"])
manifest = None
if mode == "Synthetic demo data":
    seed = st.sidebar.number_input("Random seed", value=C.SEED, step=1)
    plant = st.sidebar.checkbox("Plant the four discrepancies", value=True)
    platform_df, lines_df, batches_df, manifest = load_synthetic(int(seed), plant)
else:
    up = [st.sidebar.file_uploader(n, type="csv") for n in
          ("platform_transactions.csv", "bank_settlement_lines.csv", "bank_batches.csv (optional)")]
    if not (up[0] and up[1]):
        st.info("Upload the platform and bank-lines CSVs to reconcile them (amounts are read as text, never floats).")
        st.stop()
    rd = lambda f: pd.read_csv(f, dtype=str, keep_default_na=False)
    platform_df, lines_df = rd(up[0]), rd(up[1])
    batches_df = rd(up[2]) if up[2] else None
st.sidebar.caption(f"Period {C.PERIOD_START:%d %b} - {C.PERIOD_END:%d %b %Y} (IST) | settlement date decides the period | "
                   f"tolerance +/-{C.ROUNDING_TOLERANCE}")

res = run_recon(platform_df, lines_df, batches_df)
b = res.bridge

# ---------------------------------------------------------------- headline
st.title("Month-end settlement reconciliation")
c = st.columns(4)
c[0].metric("Platform total (Oct)", fmt(b.platform_total, 2))
c[1].metric("Bank total (Oct)", fmt(b.bank_total, 2))
c[2].metric("Raw gap (bank - platform)", fmt(b.bank_total - b.platform_total, 2))
c[3].metric("Unexplained after bridge", fmt(b.unexplained, 3))
if b.unexplained == ZERO:
    st.success("The bridge closes: every unit of the gap is attributed to a named item.")
else:
    st.warning("Residual remains - see the unexplained items in the exceptions tab.")

tabs = st.tabs(["Bridge", "Exceptions", "Timing", "Rounding", "Ground truth", "Assumptions"])

with tabs[0]:
    show(b.as_frame())
    st.caption("In-transit items are removed at the platform's full-precision amount; carry-ins are added at the bank's rounded "
               "amount. Using each side's own number is what lets the bridge close exactly.")

with tabs[1]:
    exc = res.exceptions
    if exc.empty:
        st.success("No exceptions.")
    else:
        pick = st.multiselect("Category", sorted(exc.category.unique()), default=sorted(exc.category.unique()))
        show(exc[exc.category.isin(pick)])
        st.caption("Orphan refunds, duplicates of platform rows and refund-integrity failures are control exceptions: "
                   "they may not move the bridge, but they are exposure.")

with tabs[2]:
    k1, k2 = st.columns(2)
    k1.metric("In transit (created Oct, settled Nov)", b.counts["in_transit"])
    k2.metric("Carry-in (created Sep, settled Oct)", b.counts["carry_in"])
    cols = ["txn_ref", "created_ist", "settlement_date", "platform_amount", "settled_amount"]
    st.subheader("In transit")
    show(res.bank[res.bank.bucket == "IN_TRANSIT"], cols)
    st.subheader("Carry-in")
    show(res.bank[res.bank.bucket == "CARRY_IN"], cols)

with tabs[3]:
    s = b.rounding_stats
    r = st.columns(4)
    r[0].metric("Matched lines", s["lines"])
    r[1].metric("Lines with a difference", s["nonzero_lines"])
    r[2].metric("Largest single-line difference", fmt(s["max_abs"], 3))
    r[3].metric("Net drift", fmt(b.rounding, 3))
    m = res.bank[res.bank.bucket == "MATCHED"].sort_values(["settlement_date", "line_ref"])
    cum, series = ZERO, []
    for d in m["diff"]:
        cum += d
        series.append(float(cum))               # float only for drawing the chart
    if series:
        st.line_chart(pd.DataFrame({"cumulative drift (INR)": series}))
    st.caption("Every line is within half a paisa, so a per-line check passes. The drift only appears when the lines are summed.")

with tabs[4]:
    if manifest is None:
        st.info("Ground truth exists only for synthetic data.")
    else:
        v = verify_against_manifest(res, manifest)
        st.metric("Checks passed", f"{int((v.status == 'PASS').sum())} / {len(v)}")
        show(v)
        st.caption("Caveat: we wrote both the data and the detector, so this proves internal consistency, not performance on real bank files.")

with tabs[5]:
    st.markdown("""
- One currency (INR), gross settlement (no fees or chargebacks), single reporting month.
- Platform stores UTC; every cutoff uses IST (UTC+5:30). Settlement date decides the bank period; transaction date is kept for tracing.
- Settlement lag is 1-2 business days; weekends only, no holiday calendar. Longer is flagged as *late*.
- Shared key: bank `txn_ref` = platform `txn_id` (a refund settles under its own ID).
- Duplicate = same `txn_ref` on more than one bank line. Orphan refund = original ID absent from the whole extract.
- Known limits: no PENDING status; a refund of a sale older than the lookback looks like an orphan.
""")
