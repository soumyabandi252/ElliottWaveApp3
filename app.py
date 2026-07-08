import streamlit as st
import pandas as pd
import subprocess, sys, os, time
from datetime import datetime

st.set_page_config(page_title="Elliott Wave Screener — NASDAQ Composite", layout="wide")

SCRIPT_NAME = "elliott_wave_NASDAQ_FINAL_optimized.py"
OUTPUT_XLSX = "Elliott_Wave_NASDAQ_Composite_Master_Workbook.xlsx"

st.title("📈 Elliott Wave Trading Screener")
st.caption("Live scan results generated from your Elliott Wave engine.")

REFRESH_SECONDS = 3600  # re-run the full scan at most once per hour

@st.cache_data(ttl=REFRESH_SECONDS, show_spinner="Running Elliott Wave scan on NASDAQ Composite... this can take several minutes.")
def run_scan_and_load():
    """
    Runs the existing Elliott Wave script as a subprocess (unchanged logic),
    which builds the Excel workbook. We then load every sheet from that
    workbook into a dict of DataFrames for display in Streamlit.
    """
    result = subprocess.run(
        [sys.executable, SCRIPT_NAME],
        capture_output=True, text=True, timeout=6000
    )
    log = result.stdout + "\n" + result.stderr

    if not os.path.exists(OUTPUT_XLSX):
        return {}, log

    sheets = pd.read_excel(OUTPUT_XLSX, sheet_name=None, engine="openpyxl")
    # Drop the merged title rows added by the workbook formatter (first 2 rows are headers/titles)
    cleaned = {}
    for name, df in sheets.items():
        try:
            df2 = df.copy()
            df2.columns = df2.iloc[1] if df2.shape[0] > 1 else df2.columns
            df2 = df2.iloc[2:].reset_index(drop=True)
            cleaned[name] = df2
        except Exception:
            cleaned[name] = df
    return cleaned, log

with st.spinner("Loading results..."):
    sheets_dict, run_log = run_scan_and_load()

st.caption(f"Last refreshed: {datetime.now():%Y-%m-%d %H:%M} (auto-refreshes every {REFRESH_SECONDS//60} min)")

if st.button("🔄 Force refresh now"):
    st.cache_data.clear()
    st.rerun()

(main_tab,) = st.tabs(["Nasdaq_Composite"])

with main_tab:
    if not sheets_dict:
        st.error("No workbook found yet. The scan may still be running or failed. See log below.")
        with st.expander("Run log"):
            st.text(run_log)
    else:
        tab_names = list(sheets_dict.keys())
        tabs = st.tabs(tab_names)
        for tab, name in zip(tabs, tab_names):
            with tab:
                df = sheets_dict[name]
                if df is None or df.empty:
                    st.info(f"No rows available for {name}.")
                    continue
                search = st.text_input(f"Filter {name} (ticker/company/state contains...)", key=f"filter_{name}")
                view = df
                if search:
                    mask = df.apply(lambda col: col.astype(str).str.contains(search, case=False, na=False))
                    view = df[mask.any(axis=1)]
                st.dataframe(view, use_container_width=True, height=650)
                st.download_button(
                    f"Download {name} as CSV",
                    view.to_csv(index=False),
                    file_name=f"{name}.csv",
                    mime="text/csv",
                    key=f"dl_{name}"
                )
