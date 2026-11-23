# 🌐 International Cost of Capital

A Streamlit app that estimates a project's **cost of equity, cost of debt and WACC in USD** and
converts them into the **currency of any selected country**. Both currencies use the same tax rate,
debt share and equity risk premium. The two sets of results appear side by side.

> This README was generated with AI assistance (Claude) from the agreed project specification.

---

## Features

The app has three tabs.

**Results**
- Six headline numbers, three per currency: cost of equity, after-tax cost of debt and WACC. Each
  local figure also shows its difference from USD.
- A one-line data status: either "All inputs fetched live" with the time the yields were fetched, or a
  warning listing any inputs entered by hand.
- A 7-row comparison table, USD vs. local: risk-free yield, levered beta, cost of equity, BBB spread,
  pre-tax and after-tax cost of debt, and WACC.
- **Download results (Excel):** one workbook with the settings, the full calculation detail, sources and
  dates, and the all-industry table.

**All industries**
- Every Damodaran industry under the current settings: industry, unlevered beta, WACC in USD and WACC
  in local currency. Sortable, with the selected industry highlighted. The Excel download has more
  columns.

**Sources & assumptions**
- The data sources table: for every input, the value used, source and link, data date, fetch time
  (UTC), and status (*Fetched*, *Calculated*, *Manual override* or *Manual (auto-fetch failed)*).
- Fetch problems, shown only when a source fails.
- The formulas and main assumptions, including the current conversion factor.

**Sidebar**
- **Refresh data:** skips every cache and fetches all sources again. Hover over it to see how long each
  source is cached.
- **Inputs:** industry, target country, equity risk premium (ERP), target debt share (D/V), and a
  toggle for the project tax rate (U.S. rate or the selected country's rate).
- **Manual overrides:** unlevered beta, both corporate tax rates, the USD BBB spread, both 10-year
  yields, and the local BBB spread. With its override off, the local spread field shows the calculated
  value.

### Defaults when the app opens

| Setting | Default |
|---|---|
| Industry | Software (System & Application) |
| Target country | United States |
| Project tax rate | U.S. rate |
| Equity risk premium | 6.5% |
| Debt share (D/V) | 30% |

---

## Data sources

| Input | Source | Details | Cache |
|---|---|---|---|
| Unlevered beta | [Damodaran betas.xls](https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls) | U.S. industry averages, "Unlevered beta" column (not the cash-corrected one). Falls back to [Betas.html](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html) if the Excel file can't be read. | 24 h |
| USD BBB spread | [Damodaran ratings.html](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html) | Baa2/BBB spread from the large non-financial firms table | 24 h |
| Corporate tax rates | [OECD Corporate Tax Statistics](https://www.oecd.org/en/publications/corporate-tax-statistics-2026_73af6222-en/full-report/statutory-corporate-income-tax-rates_ce84abb9.html) | Combined (central + sub-central) statutory rate, measure `CIT_C` of dataset `OECD.CTP.TPS / DSD_TAX_CIT@DF_CIT`. This is the data behind the report's Figure 2.1, pulled from the OECD SDMX API. Uses the latest year available (2026). | 24 h |
| 10-year government bond yields | [Trading Economics](https://tradingeconomics.com/bonds) (scraped, no API key) | The current quote at the moment of fetching | ~15 min |

### How yields are fetched

1. Read the `tradingeconomics.com/bonds` table (up to 3 attempts, waiting longer after each failure).
2. If the country isn't found there, read the country's own page,
   `tradingeconomics.com/{country}/government-bond-yield` (up to 3 attempts).
3. If both fail, the app asks you to enter the yield by hand. It never invents a value.

If any source can't be reached, the app shows a warning and asks for that input under
**Manual overrides**. The sources table on the *Sources & assumptions* tab marks every value as
*Fetched*, *Fetched (fallback)*, *Calculated*, *Manual override* or *Manual (auto-fetch failed)*.

---

## Methodology

Notation: d = D/V, t = project tax rate, β<sub>U</sub> = unlevered beta, ERP = equity risk premium,
s = USD BBB spread, y<sub>US</sub> / y<sub>L</sub> = U.S. / local 10-year government bond yields.

```
D/E       = d / (1 − d)
β_L       = β_U × [1 + (1 − t) × D/E]                (Hamada)

Ke_USD    = y_US + β_L × ERP
Kd_USD    = y_US + s                                  after-tax: Kd_USD × (1 − t)
WACC_USD  = (1 − d) × Ke_USD + d × Kd_USD × (1 − t)

f         = (1 + y_L) / (1 + y_US)                    conversion factor
Ke_L      = (1 + Ke_USD) × f − 1
s_L       = s × f                                     (auto; can be overridden)
Kd_L      = y_L + s_L
WACC_L    = (1 − d) × Ke_L + d × Kd_L × (1 − t)
```

### Main assumptions

1. **Currency conversion uses the gap between the two 10-year government bond yields**
   (relative interest-rate parity). The local yield also includes that government's default risk,
   so part of the country's risk enters the local-currency rates through the conversion.
2. **The beta is Damodaran's U.S. industry unlevered beta, not the cash-corrected version.** It is
   relevered at the target D/E using the project tax rate.
3. **One tax rate is used for both currencies:** either the U.S. or the selected country's OECD
   combined statutory rate. It applies both to relevering the beta and to the tax saving on interest.
4. **Cost of debt = risk-free yield + the BBB (Baa2) spread for large non-financial firms.** The
   local spread is the USD spread scaled by the conversion factor, unless you override it.
5. **Local WACC is built from the converted cost of equity and cost of debt.** The tax saving
   therefore applies to the full local interest cost, so the result is not exactly
   `(1 + WACC_USD) × f − 1`.
6. **Yields are taken during the trading day.** Opening the app at different times can give
   slightly different results. Damodaran updates its data each January; OECD tax rates are annual.
7. **A manual beta override applies only to the selected industry.** The all-industry table always
   uses Damodaran's betas.

### Worked example (values checked on 23 Nov 2026)

Inputs: β<sub>U</sub> 1.23, ERP 6.5%, D/V 30%, s 1.11%, y<sub>US</sub> 4.93%, y<sub>Brazil</sub> 15.73%.

| Case | Levered beta | Ke USD | WACC USD | Ke local | WACC local |
|---|---|---|---|---|---|
| U.S. (default), U.S. tax 25.53% | 1.6226 | 15.48% | 12.18% | 15.48% | 12.18% |
| Brazil, U.S. tax 25.53% | 1.6226 | 15.48% | 12.18% | 27.36% | 22.94% |
| Brazil, Brazil tax 34% | 1.5779 | 15.19% | 11.83% | 27.04% | 22.29% |

---

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run cost-of-capital.py
```

## Deploy on Streamlit Community Cloud

1. Push `cost-of-capital.py`, `requirements.txt` and `README.md` to the top level of a GitHub
   repository. `requirements.txt` must sit next to `cost-of-capital.py`, or Streamlit won't install
   the packages.
2. Go to [share.streamlit.io](https://share.streamlit.io) and choose **Create app**, then select
   the repository and branch, with `cost-of-capital.py` as the main file.
3. Deploy. No secrets or API keys are needed.

## Files

| File | Purpose |
|---|---|
| `cost-of-capital.py` | The Streamlit app: data fetching, calculations and user interface |
| `requirements.txt` | Python packages the app needs |
| `README.md` | This document |

## Limitations

- Scraping depends on each site's current page layout. If Damodaran, the OECD or Trading Economics
  change their pages, the affected input switches to manual entry and the app shows a warning.
- Trading Economics may block or limit automated requests from cloud servers. When that happens,
  enter yields under **Manual overrides**.
- The country list is a fixed set of about 50 countries with both a Trading Economics 10-year
  yield page and OECD tax data.

## Disclaimer

For educational and analytical use only. This is not investment, tax or financial advice. Data
belongs to the respective providers: Aswath Damodaran (NYU Stern), the OECD and Trading Economics.
