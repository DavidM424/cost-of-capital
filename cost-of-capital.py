"""
International Cost of Capital — USD vs. local currency
=======================================================

Streamlit app that estimates a project's cost of equity, cost of debt and WACC
in USD and converts them into the currency of a selected country.

Data (latest available, auto-fetched, every input can be overridden):
  * Industry unlevered betas ........ Damodaran, betas.xls (US industries)
  * USD BBB (Baa2) default spread ... Damodaran, ratings.html (large non-financial firms)
  * Corporate tax rates ............. OECD Corporate Tax Statistics, combined statutory
                                      CIT rate (dataset DSD_TAX_CIT@DF_CIT, measure CIT_C),
                                      latest year available
  * 10-year government bond yields .. Trading Economics (scraped, no API key)
"""

from __future__ import annotations

import io
import os
import re
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
st.set_page_config(page_title="International Cost of Capital", page_icon="🌐", layout="wide")

BETAS_XLS_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
BETAS_HTML_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
RATINGS_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD_REPORT_URL = (
    "https://www.oecd.org/en/publications/corporate-tax-statistics-2026_73af6222-en/"
    "full-report/statutory-corporate-income-tax-rates_ce84abb9.html"
)
OECD_SDMX_TEMPLATE = (
    "https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,{version}/all"
    "?startPeriod={start}&dimensionAtObservation=AllDimensions&format=csvfilewithlabels"
)
TE_BONDS_URL = "https://tradingeconomics.com/bonds"
TE_COUNTRY_URL = "https://tradingeconomics.com/{slug}/government-bond-yield"

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "United States"
DEFAULT_ERP_PCT = 6.5
DEFAULT_DEBT_SHARE_PCT = 30.0

ATTEMPTS = 3  # attempts per source before falling back
BACKOFF_SECONDS = float(os.environ.get("COC_BACKOFF_SECONDS", "1.5"))
TTL_YIELDS = 15 * 60  # yields move intraday
TTL_SLOW = 24 * 60 * 60  # Damodaran (annual) and OECD (annual)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Display name -> (Trading Economics slug, ISO3 code used by OECD, currency code)
COUNTRIES: dict[str, tuple[str, str, str]] = {
    "United States": ("united-states", "USA", "USD"),
    "Argentina": ("argentina", "ARG", "ARS"),
    "Australia": ("australia", "AUS", "AUD"),
    "Austria": ("austria", "AUT", "EUR"),
    "Belgium": ("belgium", "BEL", "EUR"),
    "Brazil": ("brazil", "BRA", "BRL"),
    "Canada": ("canada", "CAN", "CAD"),
    "Chile": ("chile", "CHL", "CLP"),
    "China": ("china", "CHN", "CNY"),
    "Colombia": ("colombia", "COL", "COP"),
    "Czech Republic": ("czech-republic", "CZE", "CZK"),
    "Denmark": ("denmark", "DNK", "DKK"),
    "Egypt": ("egypt", "EGY", "EGP"),
    "Finland": ("finland", "FIN", "EUR"),
    "France": ("france", "FRA", "EUR"),
    "Germany": ("germany", "DEU", "EUR"),
    "Greece": ("greece", "GRC", "EUR"),
    "Hong Kong": ("hong-kong", "HKG", "HKD"),
    "Hungary": ("hungary", "HUN", "HUF"),
    "India": ("india", "IND", "INR"),
    "Indonesia": ("indonesia", "IDN", "IDR"),
    "Ireland": ("ireland", "IRL", "EUR"),
    "Israel": ("israel", "ISR", "ILS"),
    "Italy": ("italy", "ITA", "EUR"),
    "Japan": ("japan", "JPN", "JPY"),
    "Kenya": ("kenya", "KEN", "KES"),
    "Malaysia": ("malaysia", "MYS", "MYR"),
    "Mexico": ("mexico", "MEX", "MXN"),
    "Netherlands": ("netherlands", "NLD", "EUR"),
    "New Zealand": ("new-zealand", "NZL", "NZD"),
    "Nigeria": ("nigeria", "NGA", "NGN"),
    "Norway": ("norway", "NOR", "NOK"),
    "Pakistan": ("pakistan", "PAK", "PKR"),
    "Peru": ("peru", "PER", "PEN"),
    "Philippines": ("philippines", "PHL", "PHP"),
    "Poland": ("poland", "POL", "PLN"),
    "Portugal": ("portugal", "PRT", "EUR"),
    "Romania": ("romania", "ROU", "RON"),
    "Singapore": ("singapore", "SGP", "SGD"),
    "South Africa": ("south-africa", "ZAF", "ZAR"),
    "South Korea": ("south-korea", "KOR", "KRW"),
    "Spain": ("spain", "ESP", "EUR"),
    "Sweden": ("sweden", "SWE", "SEK"),
    "Switzerland": ("switzerland", "CHE", "CHF"),
    "Thailand": ("thailand", "THA", "THB"),
    "Turkey": ("turkey", "TUR", "TRY"),
    "United Kingdom": ("united-kingdom", "GBR", "GBP"),
    "Vietnam": ("vietnam", "VNM", "VND"),
}

MONTH_YEAR_RE = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{4}"
)


# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def http_get(url: str, attempts: int = ATTEMPTS, **kwargs) -> requests.Response:
    """GET with retries and exponential backoff. Raises RuntimeError after the last attempt."""
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=25, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_error = exc
            if i < attempts - 1:
                time.sleep(BACKOFF_SECONDS * (2**i))
    raise RuntimeError(f"{url} failed after {attempts} attempts ({last_error})")


def to_float(value) -> float | None:
    """First number in a cell; None if there is none."""
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return None if pd.isna(value) else float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def to_number(value) -> float | None:
    """Number from a cell; '5.58%' -> 0.0558, 0.0558 -> 0.0558."""
    num = to_float(value)
    if num is None:
        return None
    return num / 100 if isinstance(value, str) and "%" in value else num


def norm(text) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def pct(x: float | None, digits: int = 3) -> str:
    return "—" if x is None or pd.isna(x) else f"{x * 100:.{digits}f}%"


# --------------------------------------------------------------------------------------
# Damodaran: industry betas
# --------------------------------------------------------------------------------------
BETA_COLUMNS = {
    "industry name": "Industry",
    "number of firms": "Firms",
    "beta": "Levered beta",
    "d/e ratio": "D/E",
    "effective tax rate": "Effective tax rate",
    "unlevered beta": "Unlevered beta",
    "unlevered beta corrected for cash": "Unlevered beta (cash-corrected)",
}


def parse_betas_table(raw: pd.DataFrame) -> pd.DataFrame | None:
    """Find the 'Industry Name' header row in a raw grid and return the industry table."""
    for idx in range(len(raw)):
        header = [norm(c) for c in raw.iloc[idx].tolist()]
        if "industry name" not in header:
            continue
        colmap: dict[int, str] = {}
        for j, h in enumerate(header):
            name = BETA_COLUMNS.get(h)
            if name and name not in colmap.values():
                colmap[j] = name
        if "Unlevered beta" not in colmap.values():
            continue
        body = raw.iloc[idx + 1 :, list(colmap)].copy()
        body.columns = [colmap[j] for j in colmap]
        body["Industry"] = body["Industry"].astype(str).str.strip()
        body = body[~body["Industry"].isin(["", "nan", "None"])]
        for col in body.columns:
            if col != "Industry":
                body[col] = body[col].map(to_number)
        body = body.dropna(subset=["Unlevered beta"])
        return body.reset_index(drop=True) if len(body) else None
    return None


def find_date_in_grid(raw: pd.DataFrame) -> str | None:
    """Look for 'Date updated' / 'Date of analysis' labels, else any 'Month YYYY' text."""
    for _, row in raw.iterrows():
        cells = row.tolist()
        for j, cell in enumerate(cells):
            label = norm(cell)
            if label.startswith(("date updated", "date of analysis", "last updated")):
                m = re.search(MONTH_YEAR_RE, str(cell))
                if m:
                    return m.group()
                for nxt in cells[j + 1 :]:
                    if isinstance(nxt, (datetime, pd.Timestamp)):
                        return nxt.strftime("%B %Y")
                    if isinstance(nxt, str) and nxt.strip():
                        m = re.search(MONTH_YEAR_RE, nxt)
                        return m.group() if m else nxt.strip()
    for _, row in raw.iterrows():
        for cell in row.tolist():
            if isinstance(cell, str):
                m = re.search(MONTH_YEAR_RE, cell)
                if m:
                    return m.group()
    return None


def html_to_grid(html: str) -> pd.DataFrame:
    soup = BeautifulSoup(html, "html.parser")
    rows = [
        [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        for tr in soup.find_all("tr")
    ]
    width = max((len(r) for r in rows), default=0)
    return pd.DataFrame([r + [None] * (width - len(r)) for r in rows])


@st.cache_data(ttl=TTL_SLOW, show_spinner=False)
def fetch_betas() -> dict:
    errors = []
    # 1) Excel file (primary)
    try:
        resp = http_get(BETAS_XLS_URL)
        sheets = pd.read_excel(io.BytesIO(resp.content), sheet_name=None, header=None, engine="xlrd")
        table, date = None, None
        for raw in sheets.values():
            table = table if table is not None else parse_betas_table(raw)
            date = date or find_date_in_grid(raw)
        if table is not None:
            return {"df": table, "data_date": date, "source": BETAS_XLS_URL,
                    "fetched": utc_now(), "status": "Fetched", "error": None}
        errors.append("betas.xls: industry table not found")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"betas.xls: {exc}")
    # 2) HTML version of the same dataset (fallback)
    try:
        resp = http_get(BETAS_HTML_URL)
        table = parse_betas_table(html_to_grid(resp.text))
        if table is not None:
            text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
            m = re.search(r"as of\s+(" + MONTH_YEAR_RE + ")", text)
            return {"df": table, "data_date": m.group(1) if m else None, "source": BETAS_HTML_URL,
                    "fetched": utc_now(), "status": "Fetched (HTML fallback)", "error": "; ".join(errors)}
        errors.append("Betas.html: industry table not found")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Betas.html: {exc}")
    return {"df": None, "data_date": None, "source": BETAS_XLS_URL, "fetched": utc_now(),
            "status": "Failed", "error": "; ".join(errors)}


# --------------------------------------------------------------------------------------
# Damodaran: BBB spread
# --------------------------------------------------------------------------------------
def parse_ratings(html: str) -> dict:
    """Baa2/BBB spread. Prefers the large non-financial table (coverage lower bound 2.5)."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        for i, cell in enumerate(cells):
            if not re.search(r"Baa2\s*/\s*BBB", cell):
                continue
            low = to_float(cells[i - 2]) if i >= 2 else None
            high = to_float(cells[i - 1]) if i >= 1 else None
            spread_txt = cells[i + 1] if i + 1 < len(cells) else ""
            spread = to_float(spread_txt)
            if spread is None:
                continue
            if "%" in spread_txt or spread >= 0.2:
                spread /= 100
            candidates.append((low, high, spread))
    if not candidates:
        raise ValueError("Baa2/BBB row not found")
    large = [c for c in candidates if c[0] is not None and abs(c[0] - 2.5) < 0.01]
    low, high, spread = (large or candidates)[0]
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(?:as of|updated(?: in| on)?)\s*:?\s*(" + MONTH_YEAR_RE + ")", text, re.I)
    date = m.group(1) if m else (re.search(MONTH_YEAR_RE, text).group() if re.search(MONTH_YEAR_RE, text) else None)
    return {"spread": spread, "coverage_low": low, "coverage_high": high,
            "large_firm_table": bool(large), "data_date": date}


@st.cache_data(ttl=TTL_SLOW, show_spinner=False)
def fetch_ratings() -> dict:
    try:
        resp = http_get(RATINGS_URL)
        out = parse_ratings(resp.text)
        out.update({"source": RATINGS_URL, "fetched": utc_now(), "status": "Fetched", "error": None})
        return out
    except Exception as exc:  # noqa: BLE001
        return {"spread": None, "data_date": None, "source": RATINGS_URL, "fetched": utc_now(),
                "status": "Failed", "error": str(exc)}


# --------------------------------------------------------------------------------------
# OECD: combined statutory corporate income tax rates
# --------------------------------------------------------------------------------------
def parse_oecd_csv(text: str) -> dict[str, dict]:
    """ISO3 -> {'rate': decimal, 'year': int} using measure CIT_C, latest year per country."""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    # Labelled SDMX CSVs carry both a code column ("MEASURE") and a label column ("Measure");
    # prefer the exact upper-case code column.
    cols: dict[str, str] = {}
    for c in df.columns:
        key = c.strip().upper()
        if key not in cols or c.strip() == key:
            cols[key] = c
    for need in ("REF_AREA", "TIME_PERIOD", "OBS_VALUE", "MEASURE"):
        if need not in cols:
            raise ValueError(f"OECD CSV missing column {need}")
    df = df[df[cols["MEASURE"]].str.upper() == "CIT_C"].copy()
    df["year"] = pd.to_numeric(df[cols["TIME_PERIOD"]].str[:4], errors="coerce")
    df["rate"] = pd.to_numeric(df[cols["OBS_VALUE"]], errors="coerce")
    df = df.dropna(subset=["year", "rate"])
    if df.empty:
        raise ValueError("OECD CSV has no CIT_C observations")
    if df["rate"].max() <= 1:  # defensive: values already in decimals
        df["rate"] *= 100
    # One headline combined rate per country-year; if a breakdown dimension produces
    # several rows, the headline statutory rate is the highest one.
    grouped = df.groupby([cols["REF_AREA"], "year"])["rate"].max().reset_index()
    latest = grouped.sort_values("year").groupby(cols["REF_AREA"]).tail(1)
    return {
        str(r[cols["REF_AREA"]]).upper(): {"rate": float(r["rate"]) / 100, "year": int(r["year"])}
        for _, r in latest.iterrows()
    }


@st.cache_data(ttl=TTL_SLOW, show_spinner=False)
def fetch_tax_rates() -> dict:
    start = datetime.now(timezone.utc).year - 5
    errors = []
    for version in ("", "2.0"):
        url = OECD_SDMX_TEMPLATE.format(version=version, start=start)
        try:
            resp = http_get(url)
            rates = parse_oecd_csv(resp.text)
            return {"rates": rates, "source": url, "fetched": utc_now(), "status": "Fetched", "error": None}
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
    return {"rates": {}, "source": OECD_SDMX_TEMPLATE.format(version="", start=start),
            "fetched": utc_now(), "status": "Failed", "error": "; ".join(errors)}


# --------------------------------------------------------------------------------------
# Trading Economics: 10-year government bond yields (current quote at fetch time)
# --------------------------------------------------------------------------------------
TE_HREF_RE = re.compile(r"^(?:https?://(?:www\.)?tradingeconomics\.com)?/([a-z0-9-]+)/government-bond-yield/?$")
TE_DATE_RE = re.compile(r"^(?:[A-Z][a-z]{2}/\d{1,2}|\d{1,2}:\d{2}(?::\d{2})?)$")


def parse_te_rows(html: str) -> dict[str, tuple[float, str | None]]:
    """slug -> (yield in %, TE date/time label) for every 10Y row linking to /{slug}/government-bond-yield."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, tuple[float, str | None]] = {}
    for tr in soup.find_all("tr"):
        link = tr.find("a", href=TE_HREF_RE)
        if link is None:
            continue
        slug = TE_HREF_RE.match(link["href"]).group(1)
        if slug in out:
            continue
        tds = tr.find_all("td")
        value = None
        price_cell = tr.find("td", id="p")
        if price_cell is not None:
            value = to_float(price_cell.get_text())
        if value is None:
            for td in tds:
                if td.find("a") is not None:
                    continue
                value = to_float(td.get_text())
                if value is not None:
                    break
        date = None
        date_cell = tr.find("td", id="date")
        if date_cell is not None:
            date = date_cell.get_text(strip=True) or None
        else:
            for td in reversed(tds):
                txt = td.get_text(strip=True)
                if TE_DATE_RE.match(txt):
                    date = txt
                    break
        if value is not None and -5 < value < 200:
            out[slug] = (value, date)
    return out


def parse_te_summary(html: str) -> tuple[float, str | None] | None:
    """Fallback: the 'yield on ... 10Y ... rose/eased to X% on <date>' summary sentence."""
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "description"})
    text = ((meta.get("content", "") if meta else "") + " " + soup.get_text(" ", strip=True))
    m = re.search(
        r"10[- ]?(?:Y|Year)[^%]{0,160}?\b(?:to|at|around|of)\s+(-?\d+(?:\.\d+)?)%"
        r"(?:\s+on\s+([A-Z][a-z]+ \d{1,2}, \d{4}))?",
        text,
    )
    if not m:
        return None
    return float(m.group(1)), m.group(2)


@st.cache_data(ttl=TTL_YIELDS, show_spinner=False)
def fetch_te_bonds_table() -> dict:
    try:
        resp = http_get(TE_BONDS_URL)
        return {"rows": parse_te_rows(resp.text), "fetched": utc_now(), "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"rows": {}, "fetched": utc_now(), "error": str(exc)}


@st.cache_data(ttl=TTL_YIELDS, show_spinner=False)
def fetch_yield(slug: str) -> dict:
    """Current 10Y quote: /bonds table first, then the country page; both with retries."""
    table = fetch_te_bonds_table()
    if slug in table["rows"]:
        value, date = table["rows"][slug]
        return {"value": value / 100, "te_date": date, "fetched": table["fetched"],
                "source": TE_BONDS_URL, "status": "Fetched", "error": None}
    url = TE_COUNTRY_URL.format(slug=slug)
    try:
        resp = http_get(url)
        rows = parse_te_rows(resp.text)
        if slug in rows:
            value, date = rows[slug]
        else:
            found = parse_te_summary(resp.text)
            if found is None:
                raise ValueError("10Y quote not found on country page")
            value, date = found
        return {"value": value / 100, "te_date": date, "fetched": utc_now(), "source": url,
                "status": "Fetched (country page)", "error": table["error"]}
    except Exception as exc:  # noqa: BLE001
        return {"value": None, "te_date": None, "fetched": utc_now(), "source": url,
                "status": "Failed", "error": f"/bonds: {table['error'] or 'country not in table'}; page: {exc}"}


def clear_all_caches() -> None:
    for fn in (fetch_betas, fetch_ratings, fetch_tax_rates, fetch_te_bonds_table, fetch_yield):
        fn.clear()


# --------------------------------------------------------------------------------------
# Finance
# --------------------------------------------------------------------------------------
def cost_of_capital(bu, erp, d, t, y_us, y_l, s_usd, s_local_override=None) -> dict:
    """All rates as decimals. Works on scalars or numpy/pandas arrays for `bu`."""
    de = d / (1 - d)
    bl = bu * (1 + (1 - t) * de)
    ke_usd = y_us + bl * erp
    kd_usd = y_us + s_usd
    wacc_usd = (1 - d) * ke_usd + d * kd_usd * (1 - t)
    f = (1 + y_l) / (1 + y_us)
    ke_l = (1 + ke_usd) * f - 1
    s_auto = s_usd * f
    s_l = s_auto if s_local_override is None else s_local_override
    kd_l = y_l + s_l
    wacc_l = (1 - d) * ke_l + d * kd_l * (1 - t)
    return {
        "de": de, "bl": bl, "f": f,
        "ke_usd": ke_usd, "kd_usd": kd_usd, "kd_usd_at": kd_usd * (1 - t), "wacc_usd": wacc_usd,
        "s_auto": s_auto, "s_l": s_l,
        "ke_l": ke_l, "kd_l": kd_l, "kd_l_at": kd_l * (1 - t), "wacc_l": wacc_l,
    }


# --------------------------------------------------------------------------------------
# Input widgets with manual override
# --------------------------------------------------------------------------------------
def overridable(label: str, fetched: float | None, key: str, *, percent: bool, step: float,
                help_text: str | None = None) -> tuple[float | None, bool]:
    """Checkbox + number input. Returns (value, is_manual). Forces manual entry if fetch failed."""
    missing = fetched is None
    on = st.checkbox(f"Override {label}", value=missing, disabled=missing, key=f"{key}__on",
                     help=help_text)
    if not on:
        return fetched, False
    if missing:
        st.caption(f"⚠️ Auto-fetch failed — enter {label} manually.")
    scale = 100 if percent else 1
    default = None if missing else round(fetched * scale, 4)
    value = st.number_input(
        f"{label} ({'%' if percent else 'x'})", value=default, step=step,
        format="%.4f" if not percent else "%.3f", key=f"{key}__val",
    )
    return (None if value is None else value / scale), True


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------
st.title("🌐 International Cost of Capital")
st.caption(
    "Cost of equity, cost of debt and WACC for a project in USD and in the currency of a selected "
    "country — same tax rate, debt share and equity risk premium in both currencies."
)

with st.sidebar:
    st.header("Data")
    if st.button("🔄 Refresh data", width="stretch",
                 help="Ignores all caches and refetches every source now. Yields are cached ~15 min "
                      "(intraday quotes); Damodaran and OECD data 24 h."):
        clear_all_caches()
        st.rerun()

with st.spinner("Fetching Damodaran, OECD and Trading Economics data…"):
    betas = fetch_betas()
    ratings = fetch_ratings()
    taxes = fetch_tax_rates()

# ---- Industry & country ------------------------------------------------------------------
beta_df = betas["df"]
industries = list(beta_df["Industry"]) if beta_df is not None else [DEFAULT_INDUSTRY]
country_names = [DEFAULT_COUNTRY] + sorted(c for c in COUNTRIES if c != DEFAULT_COUNTRY)

with st.sidebar:
    st.header("Project")
    industry = st.selectbox("Industry", industries,
                            index=industries.index(DEFAULT_INDUSTRY) if DEFAULT_INDUSTRY in industries else 0)
    country = st.selectbox("Target country", country_names, index=0)
    erp = st.slider("Equity risk premium (%)", 2.0, 12.0, DEFAULT_ERP_PCT, 0.1) / 100
    d = st.slider("Target debt share, D/V (%)", 0.0, 80.0, DEFAULT_DEBT_SHARE_PCT, 1.0) / 100

slug, iso, ccy = COUNTRIES[country]
us_slug, us_iso, _ = COUNTRIES[DEFAULT_COUNTRY]
is_us = country == DEFAULT_COUNTRY

with st.spinner("Fetching 10-year government bond yields…"):
    y_us_info = fetch_yield(us_slug)
    y_l_info = y_us_info if is_us else fetch_yield(slug)

fetched_beta = None
if beta_df is not None and industry in set(beta_df["Industry"]):
    fetched_beta = float(beta_df.loc[beta_df["Industry"] == industry, "Unlevered beta"].iloc[0])
tax_us_info = taxes["rates"].get(us_iso)
tax_l_info = taxes["rates"].get(iso)

with st.sidebar:
    tax_choice = st.radio(
        "Project tax rate", ["U.S.", "Selected country"], horizontal=True, index=0,
        format_func=lambda o: "U.S. rate" if o == "U.S." else f"{country} rate",
        help="The same tax rate is used for both currencies (beta relevering and debt tax shield).",
        disabled=is_us,
    )
    use_us_tax = is_us or tax_choice == "U.S."

    with st.expander("Manual overrides", expanded=False):
        bu, bu_manual = overridable("unlevered beta", fetched_beta, f"beta::{industry}",
                                    percent=False, step=0.01)
        t_us, t_us_manual = overridable("U.S. tax rate", tax_us_info["rate"] if tax_us_info else None,
                                        "tax::USA", percent=True, step=0.01)
        if is_us:
            t_l, t_l_manual = t_us, t_us_manual
        else:
            t_l, t_l_manual = overridable(f"{country} tax rate",
                                          tax_l_info["rate"] if tax_l_info else None,
                                          f"tax::{iso}", percent=True, step=0.01)
        s_usd, s_manual = overridable("USD BBB spread", ratings["spread"], "spread::USD",
                                      percent=True, step=0.01)
        y_us, y_us_manual = overridable("U.S. 10Y yield", y_us_info["value"], "yield::USA",
                                        percent=True, step=0.01)
        if is_us:
            y_l, y_l_manual = y_us, y_us_manual
        else:
            y_l, y_l_manual = overridable(f"{country} 10Y yield", y_l_info["value"], f"yield::{iso}",
                                          percent=True, step=0.01)

        # Local-currency BBB spread: off = shows the automatic calculation; on = manual value.
        s_local_override = None
        if is_us:
            st.caption("Local BBB spread: the selected country is the U.S., so it equals the USD spread.")
        elif None in (s_usd, y_us, y_l):
            st.caption("Local BBB spread: available once the USD spread and both yields are set.")
        else:
            s_auto_preview = s_usd * (1 + y_l) / (1 + y_us)
            s_l_on = st.checkbox("Override local BBB spread", value=False, key=f"slocal::{iso}__on",
                                 help="Off: calculated as USD spread × (1 + local yield) / (1 + U.S. yield).")
            if s_l_on:
                val = st.number_input("Local BBB spread (%)", value=round(s_auto_preview * 100, 4),
                                      step=0.01, format="%.3f", key=f"slocal::{iso}__val")
                s_local_override = val / 100
            else:
                st.number_input("Local BBB spread (%) — calculated", value=round(s_auto_preview * 100, 4),
                                format="%.3f", disabled=True)

# Stop cleanly if a required input is missing (fetch failed and no manual value yet).
required = {"Unlevered beta": bu, "U.S. tax rate": t_us, "USD BBB spread": s_usd, "U.S. 10Y yield": y_us}
if not is_us:
    required.update({f"{country} tax rate": t_l, f"{country} 10Y yield": y_l})
missing_inputs = [k for k, v in required.items() if v is None]
if missing_inputs:
    st.warning(
        "Waiting for input: " + ", ".join(missing_inputs)
        + ". Automatic fetch failed for these — open **Manual overrides** in the sidebar and enter them."
    )
    st.stop()

t = t_us if use_us_tax else t_l
r = cost_of_capital(bu, erp, d, t, y_us, y_l, s_usd, s_local_override)
tax_label = f"U.S. tax rate ({pct(t, 2)})" if use_us_tax else f"{country} tax rate ({pct(t, 2)})"
local_label = f"{ccy} ({country})"


# ---- Sources & dates (built first: the Results tab summarises it) -------------------------
def status(manual: bool, info_status: str) -> str:
    if manual:
        return "Manual override" if info_status != "Failed" else "Manual (auto-fetch failed)"
    return info_status


def yield_date(info: dict) -> str:
    return f"TE quote: {info['te_date']}" if info.get("te_date") else "—"


sources_rows = [
    ("Unlevered beta", f"{bu:.4f}", "Damodaran, Betas by Sector (US)", betas["source"],
     betas["data_date"] or "—", betas["fetched"], status(bu_manual, betas["status"])),
    ("USD BBB spread (Baa2/BBB)", pct(s_usd), "Damodaran, Ratings & spreads (large non-financial)",
     ratings["source"], ratings.get("data_date") or "—", ratings["fetched"], status(s_manual, ratings["status"])),
]
if not is_us:
    sources_rows.append(
        ("Local BBB spread", pct(r["s_l"]),
         "Manual input" if s_local_override is not None else "Calculated: USD spread × conversion factor",
         None, "—", "—", "Manual override" if s_local_override is not None else "Calculated")
    )
sources_rows.append(
    ("U.S. tax rate (combined)", pct(t_us, 2), "OECD Corporate Tax Statistics (CIT_C)", OECD_REPORT_URL,
     str(tax_us_info["year"]) if tax_us_info else "—", taxes["fetched"],
     status(t_us_manual, taxes["status"] if tax_us_info else "Failed"))
)
if not is_us:
    sources_rows.append(
        (f"{country} tax rate (combined)", pct(t_l, 2), "OECD Corporate Tax Statistics (CIT_C)", OECD_REPORT_URL,
         str(tax_l_info["year"]) if tax_l_info else "—", taxes["fetched"],
         status(t_l_manual, taxes["status"] if tax_l_info else "Failed"))
    )
sources_rows.append(
    ("U.S. 10Y yield", pct(y_us), "Trading Economics", y_us_info["source"], yield_date(y_us_info),
     y_us_info["fetched"], status(y_us_manual, y_us_info["status"]))
)
if not is_us:
    sources_rows.append(
        (f"{country} 10Y yield", pct(y_l), "Trading Economics", y_l_info["source"], yield_date(y_l_info),
         y_l_info["fetched"], status(y_l_manual, y_l_info["status"]))
    )
sources = pd.DataFrame(sources_rows, columns=["Input", "Value used", "Source", "Link", "Data date",
                                              "Fetched", "Status"])

# Everything entered by hand, including a manual local spread (a calculated spread is not manual).
manual_items = sources.loc[sources["Status"].str.startswith("Manual"), "Input"].tolist()
yield_times = sorted({y_us_info["fetched"]} | ({y_l_info["fetched"]} if not is_us else set()))

fetch_errors = {
    "Damodaran betas": betas.get("error") if betas["status"] == "Failed" else None,
    "Damodaran ratings": ratings.get("error"),
    "OECD tax rates": taxes.get("error"),
    "U.S. 10Y yield": y_us_info.get("error") if y_us_info["status"] == "Failed" else None,
    f"{country} 10Y yield": None if is_us or y_l_info["status"] != "Failed" else y_l_info.get("error"),
}

# ---- Tabs ------------------------------------------------------------------------------------
tab_results, tab_industries, tab_sources = st.tabs(["Results", "All industries", "Sources & assumptions"])

# ---- Tab 1: Results --------------------------------------------------------------------------
with tab_results:
    st.subheader(f"{industry} · {country}")
    st.caption(f"Project tax rate: **{tax_label}** · ERP **{pct(erp, 2)}** · D/V **{pct(d, 1)}**")

    col_usd, col_loc = st.columns(2)
    with col_usd:
        st.markdown("#### USD")
        a, b, c = st.columns(3)
        a.metric("Cost of equity", pct(r["ke_usd"], 2))
        b.metric("After-tax cost of debt", pct(r["kd_usd_at"], 2))
        c.metric("WACC", pct(r["wacc_usd"], 2))
    with col_loc:
        st.markdown(f"#### {local_label}")
        a, b, c = st.columns(3)
        a.metric("Cost of equity", pct(r["ke_l"], 2), f"{(r['ke_l'] - r['ke_usd']) * 100:+.2f} pp vs USD")
        b.metric("After-tax cost of debt", pct(r["kd_l_at"], 2),
                 f"{(r['kd_l_at'] - r['kd_usd_at']) * 100:+.2f} pp vs USD")
        c.metric("WACC", pct(r["wacc_l"], 2), f"{(r['wacc_l'] - r['wacc_usd']) * 100:+.2f} pp vs USD")

    # One-line data status in place of the full sources table.
    times = " / ".join(yield_times)
    if manual_items:
        st.warning(
            f"{len(manual_items)} input(s) entered manually: {', '.join(manual_items)} · "
            f"yields as of {times} · details in **Sources & assumptions**",
            icon="⚠️",
        )
    else:
        st.success(f"All inputs fetched live · yields as of {times} · details in **Sources & assumptions**",
                   icon="✅")

    detail = pd.DataFrame(
        [
            ("10-year government bond yield (risk-free)", pct(y_us), pct(y_l)),
            ("Levered beta", f"{r['bl']:.4f}", f"{r['bl']:.4f}"),
            ("Cost of equity", pct(r["ke_usd"]), pct(r["ke_l"])),
            ("BBB default spread", pct(s_usd), pct(r["s_l"])),
            ("Pre-tax cost of debt", pct(r["kd_usd"]), pct(r["kd_l"])),
            ("After-tax cost of debt", pct(r["kd_usd_at"]), pct(r["kd_l_at"])),
            ("WACC", pct(r["wacc_usd"]), pct(r["wacc_l"])),
        ],
        columns=["Item", "USD", local_label],
    )
    st.dataframe(detail, hide_index=True, width="stretch")

    # Full calculation detail goes to the Excel workbook (not shown on screen).
    full_detail = pd.DataFrame(
        [
            ("10-year government bond yield (risk-free)", pct(y_us), pct(y_l)),
            ("Unlevered beta", f"{bu:.4f}", f"{bu:.4f}"),
            ("Debt-to-equity (D/E)", f"{r['de']:.4f}", f"{r['de']:.4f}"),
            ("Levered beta", f"{r['bl']:.4f}", f"{r['bl']:.4f}"),
            ("Equity risk premium", pct(erp), "— (converted)"),
            ("Cost of equity", pct(r["ke_usd"]), pct(r["ke_l"])),
            ("BBB default spread", pct(s_usd), pct(r["s_l"])),
            ("Pre-tax cost of debt", pct(r["kd_usd"]), pct(r["kd_l"])),
            ("Project tax rate", pct(t, 2), pct(t, 2)),
            ("After-tax cost of debt", pct(r["kd_usd_at"]), pct(r["kd_l_at"])),
            ("Equity share (E/V)", pct(1 - d, 1), pct(1 - d, 1)),
            ("Debt share (D/V)", pct(d, 1), pct(d, 1)),
            ("WACC", pct(r["wacc_usd"]), pct(r["wacc_l"])),
            ("Conversion factor f = (1+y_local)/(1+y_US)", "1.00000", f"{r['f']:.5f}"),
        ],
        columns=["Item", "USD", local_label],
    )

    # All-industry table (full columns kept for the Excel download).
    if beta_df is None:
        all_ind = pd.DataFrame()
    else:
        all_r = cost_of_capital(beta_df["Unlevered beta"].to_numpy(), erp, d, t, y_us, y_l, s_usd,
                                s_local_override)
        all_ind = pd.DataFrame({
            "Industry": beta_df["Industry"],
            "Firms": beta_df.get("Firms"),
            "Unlevered beta": beta_df["Unlevered beta"],
            "Levered beta": all_r["bl"],
            "Cost of equity USD (%)": all_r["ke_usd"] * 100,
            "WACC USD (%)": all_r["wacc_usd"] * 100,
            f"Cost of equity local {ccy} (%)": all_r["ke_l"] * 100,
            f"WACC local {ccy} (%)": all_r["wacc_l"] * 100,
        })

    settings = pd.DataFrame(
        [
            ("Industry", industry), ("Target country", country), ("Local currency", ccy),
            ("Project tax rate used", tax_label), ("Equity risk premium", pct(erp, 2)),
            ("Debt share D/V", pct(d, 1)),
            ("Local BBB spread", "manual" if s_local_override is not None else "calculated (USD spread × f)"),
            ("Generated", utc_now()),
        ],
        columns=["Setting", "Value"],
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        settings.to_excel(writer, sheet_name="Settings", index=False)
        full_detail.to_excel(writer, sheet_name="Results", index=False)
        sources.to_excel(writer, sheet_name="Sources & dates", index=False)
        if not all_ind.empty:
            all_ind.to_excel(writer, sheet_name="All industries", index=False)
    st.download_button(
        "⬇️ Download results (Excel)", buffer.getvalue(), file_name=f"cost_of_capital_{iso}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Workbook with settings, full calculation detail, sources and dates, and all industries.",
    )

# ---- Tab 2: All industries -------------------------------------------------------------------
with tab_industries:
    st.subheader(f"All-industry comparison · {country}")
    if all_ind.empty:
        st.info("Industry table unavailable (Damodaran fetch failed). Use Refresh data to retry.")
    else:
        st.caption(
            "Same country, tax rate, ERP, D/V and spreads as the Results tab; Damodaran betas for every "
            "industry. The selected industry is highlighted. Click a column header to sort. A manual beta "
            "override applies only to the selected industry. More columns are in the Excel download."
        )
        shown = all_ind[["Industry", "Unlevered beta", "WACC USD (%)", f"WACC local {ccy} (%)"]]
        st.dataframe(
            shown.style.apply(
                lambda row: ["background-color: rgba(255, 215, 0, 0.25)" if row["Industry"] == industry else ""]
                * len(row),
                axis=1,
            ).format({"Unlevered beta": "{:.2f}", "WACC USD (%)": "{:.2f}", f"WACC local {ccy} (%)": "{:.2f}"},
                     na_rep="—"),
            hide_index=True, width="stretch", height=560,
        )

# ---- Tab 3: Sources & assumptions ------------------------------------------------------------
with tab_sources:
    st.subheader("Data sources and dates")
    st.dataframe(sources, hide_index=True, width="stretch",
                 column_config={"Link": st.column_config.LinkColumn("Link", display_text="open")})
    if any(fetch_errors.values()):
        with st.expander("⚠️ Fetch problems", expanded=True):
            for name, err in fetch_errors.items():
                if err:
                    st.write(f"**{name}:** {err}")
    if tax_l_info is None and not is_us and taxes["status"] != "Failed":
        st.info(f"OECD data has no rate for {country}; enter it under Manual overrides.")

    with st.expander("Methodology and main assumptions", expanded=False):
        st.markdown(
            "**Formulas** (d = D/V, t = project tax rate, β<sub>U</sub> = unlevered beta, s = USD BBB spread)",
            unsafe_allow_html=True,
        )
        st.latex(r"D/E = \frac{d}{1-d}, \qquad \beta_L = \beta_U\,[1 + (1-t)\,D/E]")
        st.latex(r"K_e^{USD} = y_{US} + \beta_L \times ERP, \qquad K_d^{USD} = y_{US} + s")
        st.latex(r"WACC^{USD} = (1-d)\,K_e^{USD} + d\,K_d^{USD}(1-t)")
        st.latex(r"f = \frac{1+y_{L}}{1+y_{US}}, \qquad K_e^{L} = (1+K_e^{USD})\,f - 1")
        st.latex(r"s_L^{auto} = s \times f, \qquad K_d^{L} = y_L + s_L")
        st.latex(r"WACC^{L} = (1-d)\,K_e^{L} + d\,K_d^{L}(1-t)")
        st.markdown(f"Current conversion factor: **f = {r['f']:.5f}**")
        st.markdown(
            """
**Assumptions**
- **Currency conversion** uses the 10-year government bond yield differential (relative interest-rate
  parity). The local yield also contains sovereign default risk, so part of country risk enters the local
  rates through the conversion.
- **Beta**: Damodaran's U.S. industry *unlevered* beta (not the cash-corrected version), relevered at the
  target D/E with the chosen project tax rate (Hamada).
- **One tax rate, both currencies**: either the U.S. or the selected country's OECD combined statutory
  rate (latest year available), applied to beta relevering and the debt tax shield.
- **Cost of debt**: risk-free yield + BBB (Baa2) default spread for large non-financial firms. The local
  spread is the USD spread scaled by the conversion factor unless overridden.
- **Local WACC** is built from converted components, so the tax shield applies to the full local
  interest cost (it is therefore not exactly `(1+WACC_USD)·f − 1`).
- **Yields are intraday quotes** at fetch time; opening the app at different moments can give slightly
  different results. Damodaran data are updated annually (January); OECD rates are annual.
"""
        )

st.caption(
    "Sources: Aswath Damodaran (NYU Stern) industry betas and ratings spreads; OECD Corporate Tax "
    "Statistics; Trading Economics. For educational and analytical use — not investment advice."
)
