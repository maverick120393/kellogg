# Capital Atlas — International Cost of Capital

A Python 3.12 / Streamlit app for comparing a project's USD and local-currency financing costs. It uses US industry beta, one common project tax rate, target leverage, a USD equity risk premium, government-yield proxies and a BBB borrowing spread.

**Agreed example:** USD WACC **8.5265%**; COP WACC **16.0670%**.

## 1. What you receive

Keep these three files together in a folder:

- `app.py`: application, financial engine, source adapters and embedded tests.
- `requirements.txt`: pinned direct dependencies.
- `README.md`: this guide.

You do not need an API key, an account with a data provider, Excel, or a database to run the app. Yield retrieval uses ordinary requests to public Trading Economics pages. Availability and public-display rights are separate questions; see the source and licensing notes below.

## 2. Install and run — first-time setup

### Install Python

Install **Python 3.12** from [python.org](https://www.python.org/downloads/). On Windows, enable **Add Python to PATH** during installation. A virtual environment is a small isolated folder containing the app's Python packages; it avoids changing packages used by other projects.

Open Terminal on macOS/Linux, or PowerShell on Windows. Change into the folder containing the three files. You can type `cd ` and then drag that folder into a macOS Terminal window to insert its path. Paths containing spaces need quotation marks.

### macOS / Linux

```bash
cd "/path/to/your/app-folder"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

### Windows PowerShell

```powershell
cd "C:\path\to\your\app-folder"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Using the full Windows interpreter path avoids PowerShell activation-policy problems.

Open the **Local URL**, normally `http://localhost:8501`, printed by Streamlit. Keep that terminal open while using the app. Press **Control+C** in the terminal to stop it. To run it again, repeat the activation and final command, not the installation.

These commands launch the app on your own computer. They do not publish a public website.

## 3. First calculation

1. Select a **country** and confirm its three-letter currency code.
2. Select an **industry**. There is deliberately no silently selected industry. Choose **Manual / supplied beta** if using your own project beta.
3. Review **USD ERP** and **target debt financing**. Defaults are 6.5% and 30%.
4. Choose **US federal** or **Selected country** as the common project tax basis. The selected rate applies to both currency columns. The initial country and tax basis are United States and US federal.
5. Open **Review data & manual overrides**. Read the fetched values and observation dates. Enable a manual checkbox to replace a value. A blank manual field remains missing; it is never treated as zero. Enter a date and source note when known.
6. Click **Calculate cost of capital**.
7. Review the comparison, calculation walkthrough, all-industry table and sensitivity chart. The results are snapshots: after changing inputs, calculate again before downloading.

### Percentage input conventions

- Enter **6.5** for a 6.5% ERP.
- Enter **30** for 30% debt financing.
- Enter **1.5** for a 1.5 **percentage-point** corporate borrowing spread (150 basis points).
- Enter **0.80** for a beta of 0.80. Beta is not a percentage.
- Enter **−0.5** for a −0.5% government yield.

Manual overrides remain active after refreshing sources. Country-specific overrides are cleared when changing country, to prevent an old country's yield or tax rate carrying into another country. Imported scenario values and the example are explicitly marked as manual assumptions. Reset returns the initial defaults.

### Reproduce the agreed Colombia example

Click **Load agreed Colombia example**, then **Calculate cost of capital**. The example is supplied by the user, not current market data. To skip all data-source network requests, first enable **Manual-only mode**.

| Input | Value |
|---|---:|
| Unlevered beta | 0.80 |
| Target debt share | 30% |
| Common project tax rate | 35% |
| USD ERP | 6.5% |
| US government yield | 4.0% |
| Colombia government yield | 12.0% |
| USD corporate borrowing spread | 1.5 percentage points |
| Local spread | Automatic |

| Output | USD | COP |
|---|---:|---:|
| Relevered beta | 1.022857 | 1.022857 |
| Cost of equity | 10.648571% | 19.160000% |
| Corporate borrowing spread | 1.500000% | 1.615385% |
| Pretax debt cost | 5.500000% | 13.615385% |
| After-tax debt cost | 3.575000% | 8.850000% |
| **WACC** | **8.526500%** | **16.067000%** |

## 4. Financial methodology

All rates are **decimal fractions internally**, e.g. 0.065 means 6.5%. D and E denote market values. d is D/(D+E), tau is the common marginal project tax rate, and beta_U is the industry's ordinary unlevered beta.

```text
D/E = d / (1 - d)
beta_E = beta_U × [1 + (1 - tau) × D/E]

Re_USD = Rf_USD + beta_E × ERP_USD
Rd_USD = Rf_USD + spread_USD

q = (1 + Rf_local) / (1 + Rf_USD)
Re_local = (1 + Re_USD) × q - 1
spread_local_automatic = spread_USD × q
Rd_local = Rf_local + spread_local_active

Rd_after_tax = Rd × (1 - tau)
WACC = (1 - d) × Re + d × Rd_after_tax
```

Without a local spread override, `Rd_local = (1 + Rd_USD) × q - 1`. If a local spread override is active, it replaces only the local spread; local equity still follows the approved conversion.

For the example:

```text
D/E = 0.30 / 0.70 = 0.4285714286
beta_E = 0.80 × [1 + 0.65 × 0.4285714286] = 1.0228571429
q = 1.12 / 1.04 = 1.0769230769
Re_local = (1 + 0.1064857143) × q - 1 = 0.1916
WACC_local = 0.70 × 0.1916 + 0.30 × 0.1361538462 × 0.65 = 0.16067
```

**Do not calculate foreign equity cost by adding the unchanged 6.5% ERP times beta to the local government yield.** CAPM is evaluated in USD first. Converting the equity cost implicitly scales the local ERP: 6.5% becomes 7.0% in the example.

**Do not translate aggregate WACC.** Translate equity and pretax debt costs first, apply tax to each currency's debt cost, then combine. Translating aggregate WACC would give a different result when tax and the currency adjustment are nonzero.

### Assumptions and practical limits

- Risk is measured against the **US market**, rather than a world market or the individual investor's portfolio.
- US industry betas proxy for project business risk. Other potential priced risks are omitted. No separate country equity premium or sovereign borrowing adjustment is added.
- Government yields are **risk-free proxies**. They contain liquidity and term premia and may contain sovereign default risk. A local-minus-US yield difference is not a pure default spread.
- Equal real rates and relative purchasing-power parity motivate q. The government-yield ratio substitutes for an expected-inflation ratio, so **no inflation dataset is required**. This does not establish actual inflation expectations, provide a hedge quote, or predict realized exchange rates.
- Countries sharing a currency may have different government yields. Under this proxy model, their outputs can differ; the difference cannot be interpreted solely as currency depreciation.
- Target leverage and the common tax assumption apply in both columns. Hamada relevering assumes zero debt beta; applying it with target leverage is a practical approximation.
- Interest deductions are fully usable. Actual tax shields depend on losses, deduction restrictions, tax planning and other company circumstances.
- Under perpetual constant debt, constant tax rates, usable deductions and the appropriate shield discount rate, PV(tax shield) = tau × D and VL = VU + tau × D. **Fixed debt and constant target debt share are different policies.** The app calculates rates, not project value or an exact target-leverage tax-shield valuation.
- The foreign spread is a **synthetic model estimate**, not a fetched local BBB quote. Holding BBB spreads fixed in leverage sensitivities does not identify an optimal debt ratio.
- Bank, insurer and other financial-sector debt requires special interpretation; those industries are flagged rather than silently excluded.
- Negative government yields and resulting financing rates are not floored. Unusual results receive a review warning.
- Use discount rates with cash flows of the matching currency, nominal/real basis, risk and claimant. WACC applies to unlevered free cash flows to the firm, not equity cash flows.

References: [Damodaran beta methodology](https://people.stern.nyu.edu/adamodar/pdfiles/val3ed/c08.pdf), [risk-free and currency assumptions](https://pages.stern.nyu.edu/adamodar/New_Home_Page/valquestions/riskfreerates.htm), [discount-rate framework](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/littlebook/discountrates.htm).

## 5. Data integration and provenance

| Input | Retrieval and definition | Cache |
|---|---|---|
| Industry beta | [Damodaran US workbook](https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls), exact ordinary **Unlevered beta** column; retain cached Excel values and workbook update date | 24 hours |
| USD borrowing spread | [Ratings table](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html), **Baa2/BBB**, left-hand large non-financial-firm section | 24 hours |
| US corporate tax | [OECD Corporate Tax Statistics](https://www.oecd.org/en/data/datasets/corporate-income-tax-rates-database.html), statutory central-government CIT rate; excludes state taxes | 24 hours |
| Foreign corporate tax | Same OECD dataset, statutory **combined** central/sub-central rate; not effective tax rate or targeted small-business rate | 24 hours |
| Government yields | [Trading Economics](https://tradingeconomics.com/bonds), public country pages; structured current 10Y Bond quote and dataset observation date | 1 hour |
| Country discovery | Public bonds page; if no country links are available, try Trading Economics' [10-year country list](https://tradingeconomics.com/country-list/government-bond-10y) | 24 hours |
| ERP and debt share | User assumptions | Session |
| Local spread | Calculated or explicitly overridden | Scenario |

OECD's exact series dimensions are pinned to dataflow `OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0`:

```text
US: USA.A.CIT.ST.PT_INC_TAX.S1311._Z._Z._Z
Foreign: ISO3.A.CIT_C.ST.PT_INC_TAX.S13._Z._Z._Z
```

Within that exact definition, the app selects the latest available valid annual observation, which may differ by country. It never replaces a missing central rate with a combined rate, or vice versa. Schema changes fail visibly and require an adapter update.

**Dates are distinct:** observation date/period describes the value; source publication/update date is shown only when exposed; retrieval and refresh timestamps describe when the app accessed it. A missing publication date is labeled, not guessed. A fresh request does not make an annual beta a daily observation. Yield observations older than seven calendar days trigger a warning.

Source caches contain public data in process memory and can be shared across sessions. User assumptions remain in the user's Streamlit session. Refresh failures retain the last successful source result and show its error and stale status. Failed requests have a 60-second automatic cooldown; explicit refresh retries immediately. A server restart removes cached data. There is no hidden bundled market-data snapshot.

### Country and currency coverage

The dropdown normally includes **all country links discovered on Trading Economics' public 10-year country list**, rather than a fixed shortlist. Not every selected country is guaranteed to have a parseable page or an OECD rate. Country membership in the list is not a claim that every adapter has been verified there.

If discovery is unavailable, a clearly labeled ISO-country **manual catalog** remains available; it contains names only, not invented yield observations. Some generated manual-catalog page slugs may not exist at Trading Economics. Enter yields manually when necessary.

ISO flags and currency labels come from pycountry/Babel metadata. Currency labels are editable and should match the selected bond. A documented metadata correction handles Bulgaria's [2026 euro adoption](https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.pr260101~c830245e42.en.html), which predates the pinned Babel update. Ambiguous mappings remain blank. The Euro area aggregate has no single OECD corporate tax rate; use US tax or an explicit manual rate.

## 6. Refreshing, overrides and exports

**Refresh source data** retries current sources, including the selected country. It does not clear your overrides. Read **Data & sources** for individual success/failure states and timestamps.

A fetched input is labeled **Source**. An active manual entry has its own checkbox, value, optional date and note, and retains the fetched reference where available. Local spread is labeled as calculated unless manually replaced. No missing number becomes a zero or a historical hardcoded default.

Exports:

- **Results CSV:** inputs, currency/country settings, output rates, provenance and assumptions. Rate values use decimal fractions.
- **Scenario JSON:** model/schema version, frozen resolved inputs, recomputed results, provenance, assumptions and the all-industry comparison. File must be under 2 MB.
- **All industries CSV:** each industry's fetched beta, recalculated outputs, common numeric assumptions and scenario provenance.

JSON import validates the model and numeric inputs, ignores imported calculated results, and loads the resolved numeric assumptions as **explicit manual overrides** for reproducibility. Original provenance is retained as historical imported metadata, not promoted to fresh fetched data. Only the active common tax rate is frozen; import sets both tax-entry fields to that value so switching the tax toggle does not inadvertently replace the imported scenario tax assumption. Import does not recreate the original server cache or freeze today's all-industry dataset.

The all-industry comparison uses **each industry's own fetched beta**, not the selected scenario's beta override. If beta data are unavailable, that comparison and its download are unavailable; the manual scenario still works.

## 7. Tests and verification

From the app folder, using the activated environment:

```bash
python app.py --self-test
python app.py --check-live
```

`--self-test` runs offline synthetic fixtures: agreed example, zero debt/tax, identity, negative yields, invalid inputs, foreign spread overrides, industry beta independence, cache failures/cooldown, parser failures, exact tax definitions, quote-vs-forecast selection, override persistence and CSV/JSON round trips. It returns a nonzero exit code on failure. Tests are embedded to keep the deliverable to three files.

`--check-live` calls the real source adapters and prints JSON with PASS/FAIL status. It returns nonzero if any source fails. It is diagnostic, not proof that every country or future refresh works. Live data may change between runs. Avoid repeatedly calling it: OECD applies rate limits.

### Verification record — November 16, 2026

- Python **3.12.12**, all pinned direct dependencies installed successfully.
- **12 embedded test methods passed**.
- Live parsing passed for **94 industry betas**, the correct **BBB row**, and **56 distinct country links**. The country-list page contained duplicate links; the app deduplicates them.
- Live US federal and Colombia combined OECD rate retrieval passed. Their latest available observation years differed (2026 and 2025 respectively); no missing year was filled in.
- Live US and Colombia government-yield parsing passed after handling the source's “US” title abbreviation. No API credentials or browser challenge bypass were used.
- Streamlit AppTest passed manual-only example calculation, changed-input invalidation and recalculation, clearing country-specific overrides after a country change, and resetting defaults without exceptions.
- Local Streamlit server startup passed. The browser displayed the agreed USD/COP results and calculation walkthrough. Desktop (1365 px) and narrow-screen (390 px) layouts were visually inspected.
- A results CSV was downloaded through the browser and its actual rows were inspected: both WACCs and manual-input provenance matched the agreed example.
- An accompanying preview image is supplied for convenience; it is not required to run the three-file application.
- Mocked outage tests verified retaining last-success data, failure cooldown and missing-source behavior. A separate Streamlit AppTest simulated a complete source outage, verified missing-input blocking, recovered using manual entries, and applied a local spread override. These are distinct from successful live integrations.
- No public deployment or exhaustive country-by-country verification is claimed. Trading Economics accessibility from a future cloud host remains unverified.

## 8. Deploy on Streamlit Community Cloud

This follows the GitHub-backed approach used for the earlier RER dashboard.

1. Create a GitHub repository for this app. Upload **only the three deliverable files** to its root. Keep virtual environments, local caches and private data out of GitHub.
2. Sign in at [Streamlit Community Cloud](https://share.streamlit.io/) and connect the GitHub account.
3. Choose **Create app** and select the repository and branch.
4. Set the main file path to **`app.py`**.
5. In advanced settings, select **Python 3.12**. No secrets or API-key configuration is required for this version.
6. Deploy and inspect the build logs. Community Cloud installs packages from `requirements.txt`.
7. Open the deployed URL. First run the agreed example, then check each live source and verify a calculation using fetched values.
8. Test refreshing, manual entry and downloads from the deployed app. If scraping is blocked there, keep manual entry available and report that integration as unavailable rather than claiming it works.

Official instructions: [deploy an app](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy) and [manage dependencies](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

The code has no accounts, database, saved private scenarios, or authentication layer. Anyone with access to a public deployment can use its controls. Scenario JSON lets a user save and reload their own assumptions. Source caches are not durable across server restarts or sleeping hosts.

## 9. Troubleshooting

| Symptom | What to do |
|---|---|
| `python3.12` / `py` is not found | Install Python 3.12, reopen the terminal, and check the command. Do not assume macOS' preinstalled Python is 3.12. |
| `No module named streamlit` | Use the virtual environment interpreter and rerun `python -m pip install -r requirements.txt`. |
| Streamlit command is not found | Use `python -m streamlit run app.py` rather than the bare `streamlit` command. |
| PowerShell blocks activation | Use `.\.venv\Scripts\python.exe` directly as shown above. |
| Browser says connection refused | Keep the Streamlit terminal running and open the exact printed Local URL. |
| Port 8501 is occupied | Run `python -m streamlit run app.py --server.port 8502`. |
| Source returns 403, 429, a challenge, or no expected fields | Wait and refresh sparingly, inspect the source link, and enter dated values manually. Do not bypass access controls. |
| OECD rate unavailable | Check tax basis and country mapping. Use the tax override; do not treat another country or another rate definition as a substitute. |
| Results look roughly 100 times too large | Review UI percentage inputs: use 6.5, not 650, for 6.5%; JSON/CSV rates use 0.065. |
| Calculations do not appear | Select an industry, supply every required input, check currency code, then click Calculate. A blank enabled override blocks calculation. |
| Old results disappear after editing | Expected: calculate again so the visible result and downloads match your assumptions. |
| Corporate credit quality seems unchanged as leverage increases | Expected: this v1 keeps the BBB spread common unless overridden; sensitivity is not credit-rating optimization. |
| Flag glyphs do not render | A browser/OS font issue; country names and currency codes still identify the scenarios. |

## 10. Source attribution and reuse

- **Aswath Damodaran / NYU Stern:** industry betas and rating spreads. His [site-use guidance](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/guide.html) requests attribution and restricts reselling the data itself.
- **OECD Corporate Tax Statistics:** acknowledge dataset, observation year, URL and access date. API availability is subject to [OECD terms](https://www.oecd.org/en/about/terms-conditions.html) and [rate/access limits](https://www.oecd.org/en/data/insights/data-explainers/2024/11/Api-best-practices-and-recommendations.html).
- **Trading Economics:** public country-page government-yield observations and coverage. Its [public terms](https://tradingeconomics.com/terms.aspx) describe a limited personal license. Successful scraping is not evidence of permission to redistribute data through a public app or exports. Relevant page metadata may identify additional licenses; public/commercial display and redistribution rights have **not been independently resolved**. Confirm the applicable rights before public publication.

This app is a transparent estimation tool under the agreed approximations; it does not claim to observe a country's true risk-free rate or a local BBB company's actual borrowing quote.
