"""International Cost of Capital. Python 3.12.

Run: python -m streamlit run app.py
Offline checks: python app.py --self-test
Live adapter checks: python app.py --check-live

Rates are decimal fractions in calculations and exports; UI inputs are percentages.
No embedded market observations or silent provider substitutions.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import html
import io
import json
import math
import re
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

BETA_URL = 'https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls'
RATING_URL = 'https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html'
BONDS_URL = 'https://tradingeconomics.com/bonds'
COVERAGE_URL = 'https://tradingeconomics.com/country-list/government-bond-10y'
TAX_BASE = 'https://sdmx.oecd.org/public/rest/v1/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0/'
TAX_SOURCE = 'https://www.oecd.org/en/data/datasets/corporate-income-tax-rates-database.html'
MODEL = 'usd-capm-yield-ratio-v1'
SCHEMA = 1
ASSUMPTIONS = [
    'Risk is measured relative to the US market; US industry betas proxy for project risk.',
    'CAPM omits other potential sources of priced risk; no separate country equity premium is added.',
    'Government yields are currency-specific risk-free proxies and may contain sovereign, liquidity and term premia.',
    'Equal real rates and relative PPP motivate the yield-ratio currency adjustment; this is an approximation, not an FX forecast.',
    'USD ERP is the base premium. Translating equity costs also scales the implied local ERP.',
    'Market-value target leverage is common to both scenarios; Hamada assumes zero debt beta.',
    'The common marginal tax rate assumes fully usable interest deductions. Fixed perpetual debt and constant target leverage are different policies.',
    'Foreign borrowing spread is a synthetic model estimate, not an observed local BBB quote.',
    'Currency WACCs are built from equity and after-tax debt components, not by translating aggregate WACC.',
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a number, not a boolean.')
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} is required and must be numeric.') from None
    if not math.isfinite(n):
        raise ValueError(f'{label} must be finite.')
    return n


@dataclass(frozen=True)
class Inputs:
    beta_u: float
    debt_share: float
    tax: float
    erp: float
    rf_us: float
    rf_local: float
    spread_us: float
    spread_local_override: float | None = None

    def validated(self) -> Inputs:
        nums = {k: finite(v, k) for k, v in asdict(self).items() if v is not None}
        for k in ('debt_share', 'tax'):
            if not 0 <= nums[k] < 1:
                raise ValueError(f'{k} must be at least 0% and below 100%.')
        for k in ('rf_us', 'rf_local'):
            if nums[k] <= -1:
                raise ValueError(f'{k} must be above -100%.')
        if nums['erp'] < 0 or nums['spread_us'] < 0:
            raise ValueError('ERP and corporate borrowing spreads cannot be negative.')
        if nums.get('spread_local_override', 0) < 0:
            raise ValueError('Local borrowing spread cannot be negative.')
        return Inputs(**nums)


def calculate(inputs: Inputs) -> dict[str, float]:
    """Pure financial engine. Apply tax AFTER translating pretax borrowing costs."""
    x = inputs.validated()
    de = x.debt_share / (1 - x.debt_share)
    beta_e = x.beta_u * (1 + (1 - x.tax) * de)
    q = (1 + x.rf_local) / (1 + x.rf_us)
    re_us = x.rf_us + beta_e * x.erp
    rd_us = x.rf_us + x.spread_us
    auto_spread = q * x.spread_us
    local_spread = auto_spread if x.spread_local_override is None else x.spread_local_override
    re_local = (1 + re_us) * q - 1
    rd_local = x.rf_local + local_spread
    out = dict(de=de, beta_e=beta_e, q=q, re_us=re_us, re_local=re_local,
               spread_local_auto=auto_spread, spread_local=local_spread,
               rd_us=rd_us, rd_local=rd_local, implied_local_erp=q*x.erp)
    for suffix, re_, rd_ in [('us', re_us, rd_us), ('local', re_local, rd_local)]:
        after = rd_ * (1 - x.tax)
        out[f'rd_after_{suffix}'] = after
        out[f'equity_contribution_{suffix}'] = (1 - x.debt_share) * re_
        out[f'debt_contribution_{suffix}'] = x.debt_share * after
        out[f'wacc_{suffix}'] = out[f'equity_contribution_{suffix}'] + out[f'debt_contribution_{suffix}']
    if not all(math.isfinite(v) for v in out.values()):
        raise ValueError('Inputs produce non-finite results; reduce extreme assumptions.')
    return out


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, allow_nan=False).encode()).hexdigest()


def request_bytes(url: str, accept: str | None = None) -> tuple[bytes, dict]:
    """Ordinary public HTTP only. Bounded retries for transient failures, no bypass."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    headers = {'User-Agent': 'InternationalCostOfCapital/1.0 (educational financial calculator)'}
    if accept:
        headers['Accept'] = accept
    retry = Retry(total=1, connect=1, read=0, status=1, backoff_factor=.3,
                  status_forcelist=(500, 502, 503, 504), respect_retry_after_header=False)
    with requests.Session() as session:
        session.mount('https://', HTTPAdapter(max_retries=retry))
        with session.get(url, headers=headers, timeout=(5, 15), stream=True) as r:
            r.raise_for_status()
            blocks, size = [], 0
            for block in r.iter_content(65536):
                size += len(block)
                if size > 12_000_000:
                    raise ValueError('Source response exceeds the 12 MB limit.')
                blocks.append(block)
            return b''.join(blocks), dict(r.headers)


def soup(text: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(text, 'html.parser')


def observation(value: float, source: str, url: str, period: str,
                basis: str, published: str | None = None) -> dict:
    return dict(value=finite(value, basis), provenance='fetched', source=source, url=url,
                observation_date=period, publication_date=published,
                retrieved_at=utc_now(), basis=basis)


def parse_betas(content: bytes) -> dict:
    import pandas as pd
    book = pd.ExcelFile(io.BytesIO(content), engine='xlrd')
    for sheet in book.sheet_names:
        frame = pd.read_excel(book, sheet_name=sheet, header=None)
        for row_idx in range(min(25, len(frame))):
            cells = [str(v).strip().lower() for v in frame.iloc[row_idx]]
            if 'industry name' not in cells or 'unlevered beta' not in cells:
                continue
            industry_col, beta_col = cells.index('industry name'), cells.index('unlevered beta')
            updated = None
            for _, row in frame.iloc[:row_idx].iterrows():
                if 'date updated' in str(row.iloc[0]).lower():
                    try:
                        updated = pd.Timestamp(row.iloc[1]).date().isoformat()
                    except (ValueError, TypeError):
                        pass
            if not updated:
                raise ValueError('Beta workbook has no identifiable update date.')
            industries = {}
            for _, row in frame.iloc[row_idx+1:].iterrows():
                name = str(row.iloc[industry_col]).strip()
                if not name or name.lower() == 'nan' or name.lower().startswith('total market'):
                    continue
                try:
                    beta = finite(row.iloc[beta_col], 'Industry beta')
                except ValueError:
                    continue
                industries[name] = observation(beta, 'Aswath Damodaran — US industries',
                    BETA_URL, updated, 'Ordinary Unlevered beta; US company sample', updated)
            if not industries:
                raise ValueError('No numeric industry betas found.')
            return industries
    raise ValueError('Expected Industry Name / Unlevered beta columns were not found.')


def load_betas() -> dict:
    return parse_betas(request_bytes(BETA_URL)[0])


def parse_spread(text: str) -> dict:
    page = soup(text)
    plain = page.get_text(' ', strip=True)
    date_match = re.search(r'as of\s+([A-Za-z]+\s+\d{4})', plain, re.I)
    if not date_match or 'large non-financial' not in plain.lower():
        raise ValueError('BBB source date or large non-financial-firm section is missing.')
    for row in page.find_all('tr'):
        cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'])]
        # The source places large non-financial firms LEFT of financial firms.
        for i, cell in enumerate(cells[:5]):
            if re.fullmatch(r'Baa2\s*/\s*BBB', cell, re.I):
                if i+1 >= len(cells) or '%' not in cells[i+1]:
                    raise ValueError('BBB spread has missing percentage units.')
                val = finite(cells[i+1].replace('%', '').strip(), 'BBB spread') / 100
                if val < 0:
                    raise ValueError('BBB spread is negative.')
                return observation(val, 'Aswath Damodaran — Baa2/BBB', RATING_URL,
                    date_match.group(1), 'Large non-financial firms; USD spread above risk-free proxy')
    raise ValueError('Baa2/BBB non-financial-firm row was not found.')


def load_spread() -> dict:
    return parse_spread(request_bytes(RATING_URL)[0].decode('utf-8', errors='replace'))


def parse_coverage(text: str, base: str = COVERAGE_URL) -> dict:
    countries = {}
    for a in soup(text).select('a[href]'):
        path = urlparse(urljoin(base, a['href'])).path
        match = re.fullmatch(r'/([a-z][a-z-]*)/government-bond-yield', path)
        name = a.get_text(' ', strip=True)
        if match and name and not re.search(r'\b\d+\s*Y\b', name, re.I):
            slug = match.group(1)
            countries[slug] = dict(name=name, slug=slug,
                url=f'https://tradingeconomics.com/{slug}/government-bond-yield')
    if not countries:
        raise ValueError('No country 10-year yield links found in the public page.')
    return countries


def load_coverage() -> dict:
    """Same-provider discovery fallback; never replaces yield observations."""
    raw, _ = request_bytes(BONDS_URL)
    try:
        return parse_coverage(raw.decode('utf-8', errors='replace'), BONDS_URL)
    except ValueError:
        raw, _ = request_bytes(COVERAGE_URL)
        return parse_coverage(raw.decode('utf-8', errors='replace'))


def json_nodes(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from json_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from json_nodes(item)


def parse_yield(text: str, country: dict) -> dict:
    """Bind current quote to the 10Y instrument and its dataset observation date."""
    page = soup(text)
    title = page.title.get_text(' ', strip=True) if page.title else ''
    if not re.search(r'10\s*[- ]?\s*(?:Year|Y\b)', title, re.I):
        raise ValueError('Page title does not identify a 10-year instrument.')
    aliases = {'United States': ['United States', 'US'],
               'United Kingdom': ['United Kingdom', 'UK'],
               'South Korea': ['South Korea', 'Korea'],
               'Czech Republic': ['Czech Republic', 'Czechia']}
    names = aliases.get(country['name'], [country['name']])
    def matches_country(value):
        return any(re.search(r'\b'+re.escape(name)+r'\b', value, re.I) for name in names)
    if not matches_country(title):
        raise ValueError('Yield page country does not match the selected country.')
    pattern = re.search(r'TEChartsMeta\s*=\s*(\[.*?\]);', text, re.S)
    if not pattern:
        raise ValueError('Structured current quote missing; refusing to read forecasts or news.')
    records = json.loads(pattern.group(1))
    quote = next((r for r in records if r.get('type') == 'Bond' and
                  re.search(r'\b10\s*Y\b', r.get('name', ''), re.I)), None)
    if quote is None:
        raise ValueError('No structured 10-year Bond quote found.')
    value = finite(quote.get('last'), 'Government yield')
    # Units must be explicit on the source page, not inferred from a bare number.
    page_text = page.get_text(' ', strip=True)
    if not re.search(r'\bpercent\b', page_text, re.I) and '%' not in page_text:
        raise ValueError('Yield source percentage units could not be confirmed.')
    period, published = None, None
    for script in page.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        for node in json_nodes(obj):
            if node.get('@type') != 'Dataset':
                continue
            if not matches_country(str(node.get('name', ''))):
                continue
            temporal = str(node.get('temporalCoverage', '')).split('/')[-1]
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', temporal):
                period = date.fromisoformat(temporal).isoformat()
            modified = str(node.get('dateModified', ''))
            if modified:
                published = modified
    if not period:
        raise ValueError('Current quote observation date could not be confirmed.')
    if date.fromisoformat(period) > datetime.now(timezone.utc).date():
        raise ValueError('Source reports a future observation date.')
    if value <= -100:
        raise ValueError('Government yield must be above -100%.')
    return observation(value/100, 'Trading Economics', country['url'], period,
        f"{country['name']} 10-year local-currency government yield proxy", published)


def load_yield(country: dict) -> dict:
    return parse_yield(request_bytes(country['url'])[0].decode('utf-8', errors='replace'), country)


def parse_tax_csv(text: str, iso3: str, us: bool, url: str) -> dict:
    """Select one exact statutory rate definition before choosing its latest year."""
    import pandas as pd
    frame = pd.read_csv(io.StringIO(text), dtype=str)
    needed = {'REF_AREA','FREQ','MEASURE','TARGETING','UNIT_MEASURE','SECTOR',
              'DIV_TREATMENT','RATE_STRUCTURE','TAX_BASE','TIME_PERIOD','OBS_VALUE'}
    if not needed.issubset(frame.columns):
        raise ValueError('OECD CSV schema changed or a non-data response was returned.')
    selected = frame
    filters = dict(REF_AREA=iso3, FREQ='A', MEASURE='CIT' if us else 'CIT_C',
                   TARGETING='ST', UNIT_MEASURE='PT_INC_TAX', SECTOR='S1311' if us else 'S13',
                   DIV_TREATMENT='_Z', RATE_STRUCTURE='_Z', TAX_BASE='_Z')
    for col, value in filters.items():
        selected = selected[selected[col] == value]
    selected = selected.copy()
    selected['numeric_value'] = pd.to_numeric(selected['OBS_VALUE'], errors='coerce')
    selected['year'] = pd.to_numeric(selected['TIME_PERIOD'], errors='coerce')
    selected = selected.dropna(subset=['numeric_value', 'year'])
    selected = selected[selected['year'] <= datetime.now(timezone.utc).year]
    if selected.empty:
        raise ValueError(f'No OECD statutory {"central" if us else "combined"} rate for {iso3}.')
    latest = selected[selected['year'] == selected['year'].max()]
    if len(latest) != 1:
        raise ValueError('OECD rate is ambiguous; manual tax entry is required.')
    row = latest.iloc[0]
    if 'UNIT_MULT' in row and str(row['UNIT_MULT']) not in ('0', 'nan', ''):
        raise ValueError('Unexpected OECD unit multiplier.')
    val = finite(row['numeric_value'], 'Tax rate') / 100
    if not 0 <= val < 1:
        raise ValueError('OECD tax rate is outside [0%, 100%).')
    return observation(val, 'OECD Corporate Tax Statistics', url, str(int(row['year'])),
        'US central-government statutory rate (federal only)' if us else
        f'{iso3} combined central/sub-central statutory rate')


def load_tax(iso3: str, us: bool = False) -> dict:
    if not re.fullmatch('[A-Z]{3}', iso3):
        raise ValueError('Country has no unambiguous OECD ISO mapping; enter tax manually.')
    key = '.'.join([iso3, 'A', 'CIT' if us else 'CIT_C', 'ST', 'PT_INC_TAX',
                    'S1311' if us else 'S13', '_Z', '_Z', '_Z'])
    url = TAX_BASE + key + '?format=csvfile'
    raw, _ = request_bytes(url, 'text/csv')
    return parse_tax_csv(raw.decode('utf-8-sig'), iso3, us, url)


class SourceCache:
    """Process-memory cache: retain last success across refresh failures.

    Shared public source data only. User assumptions are kept in session state.
    Failures are cooled down for 60 seconds; force refresh ignores that cooldown.
    """
    def __init__(self):
        self.entries: dict[str, dict] = {}
        self.lock = threading.RLock()

    def get(self, key: str, loader: Callable, ttl: int, force: bool = False) -> dict:
        now = time.time()
        with self.lock:
            previous = copy.deepcopy(self.entries.get(key, {}))
        if not force and previous and now - previous.get('attempt_epoch', 0) < (60 if previous.get('error') else ttl):
            return previous
        result = dict(previous, attempt_epoch=now, last_attempt=utc_now())
        try:
            result.update(data=loader(), fetched_epoch=time.time(), error=None, last_success=utc_now())
        except Exception as exc:
            # Network/parser errors are exposed. A missing source never becomes zero.
            result['error'] = f'{type(exc).__name__}: {str(exc)[:350]}'
        with self.lock:
            self.entries[key] = copy.deepcopy(result)
        return result


def country_metadata(name: str) -> dict:
    import pycountry
    from babel.numbers import get_territory_currencies
    aliases = {'South Korea':'Korea, Republic of', 'Russia':'Russian Federation',
               'Taiwan':'Taiwan, Province of China', 'Vietnam':'Viet Nam',
               'Turkey':'Türkiye', 'Czech Republic':'Czechia', 'Euro Area':'EU'}
    if name.casefold() == 'euro area':
        return dict(iso3='', iso2='EU', currency='EUR', flag='🇪🇺')
    try:
        obj = pycountry.countries.lookup(aliases.get(name, name))
        currencies = get_territory_currencies(obj.alpha_2, start_date=date.today())
        # Metadata convenience only; currency label is always editable in the UI.
        code = currencies[0] if len(currencies) == 1 else ''
        # Babel 2.17 predates this change. ECB: Bulgaria adopted EUR on 2026-01-01.
        # https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.pr260101~c830245e42.en.html
        if obj.alpha_2 == 'BG' and date.today() >= date(2026, 1, 1):
            code = 'EUR'
        return dict(iso3=obj.alpha_3, iso2=obj.alpha_2, currency=code,
                    flag=''.join(chr(127397+ord(c)) for c in obj.alpha_2))
    except LookupError:
        return dict(iso3='', iso2='', currency='', flag='🌐')


def industry_comparison(industries: dict, x: Inputs) -> list[dict]:
    rows = []
    for name, obs in sorted(industries.items()):
        try:
            y = replace(x, beta_u=obs['value'])
            rows.append(dict(industry=name, beta_u=y.beta_u, **calculate(y),
                             beta_observation_date=obs['observation_date'], beta_source=obs['url']))
        except (ValueError, KeyError):
            continue
    return rows


def scenario_document(x: Inputs, settings: dict, provenance: dict, industries: list[dict]) -> dict:
    return dict(schema_version=SCHEMA, model=MODEL, exported_at=utc_now(),
                inputs=asdict(x.validated()), settings=settings,
                results=calculate(x), provenance=provenance, assumptions=ASSUMPTIONS,
                industry_comparison=industries,
                units='Rates are decimal fractions; beta, q and D/E are dimensionless.')


def parse_scenario(raw: bytes) -> dict:
    if len(raw) > 2_000_000:
        raise ValueError('Scenario JSON must be smaller than 2 MB.')
    doc = json.loads(raw, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(f'Invalid {s}')))
    if doc.get('schema_version') != SCHEMA or doc.get('model') != MODEL:
        raise ValueError('Unsupported scenario version or financial methodology.')
    x = Inputs(**doc['inputs']).validated()
    settings = doc['settings']
    for name in ('country_slug', 'country_name', 'industry', 'currency', 'tax_basis'):
        if not isinstance(settings.get(name), str) or not settings[name] or len(settings[name]) > 160:
            raise ValueError(f'Scenario setting {name} is missing or invalid.')
    if not re.fullmatch('[a-z][a-z-]*', settings['country_slug']):
        raise ValueError('Invalid country identifier.')
    if not re.fullmatch('[A-Z]{3}', settings['currency']):
        raise ValueError('Currency must be a three-letter code.')
    if settings['tax_basis'] not in ('US federal', 'Selected country'):
        raise ValueError('Unrecognized tax basis.')
    # Never trust imported computed results or source provenance as current fetched data.
    return dict(inputs=asdict(x), settings=settings, original_provenance=doc.get('provenance', {}))


def result_csv(doc: dict) -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['section', 'name', 'value', 'units_or_metadata'])
    writer.writerow(['model', 'methodology', MODEL, ''])
    for k, v in doc['settings'].items():
        writer.writerow(['setting', k, v, ''])
    for k, v in doc['inputs'].items():
        writer.writerow(['input', k, v, 'decimal rate or dimensionless beta'])
    for k, v in doc['results'].items():
        writer.writerow(['result', k, v, 'decimal rate or dimensionless ratio'])
    for k, v in doc['provenance'].items():
        writer.writerow(['provenance', k, json.dumps(v, ensure_ascii=False), 'JSON metadata'])
    for item in ASSUMPTIONS:
        writer.writerow(['assumption', '', item, ''])
    return stream.getvalue().encode('utf-8-sig')


def example_scenario() -> dict:
    x = Inputs(.8, .3, .35, .065, .04, .12, .015)
    settings = dict(country_slug='colombia', country_name='Colombia', industry='Manual / supplied beta',
                    currency='COP', tax_basis='US federal')
    return scenario_document(x, settings, {'note': 'User-supplied illustrative example; not fetched market data.'}, [])


def apply_document(state, doc: dict, label: str) -> None:
    """Load frozen values as explicit overrides BEFORE Streamlit creates widgets."""
    x, settings = doc['inputs'], doc['settings']
    state['country'] = settings['country_slug']
    state['_last_country'] = settings['country_slug']
    state['import_country'] = dict(name=settings['country_name'], slug=settings['country_slug'],
        url=f"https://tradingeconomics.com/{settings['country_slug']}/government-bond-yield")
    state['industry'] = settings['industry']
    state['currency'] = settings['currency']
    state['tax_basis'] = settings['tax_basis']
    state['erp'] = x['erp']*100
    state['debt_share'] = x['debt_share']*100
    for field, val in [('beta',x['beta_u']), ('us_yield',x['rf_us']),
                       ('local_yield',x['rf_local']), ('us_spread',x['spread_us'])]:
        state[f'm_{field}'] = True
        state[f'v_{field}'] = val if field == 'beta' else val*100
        state[f'note_{field}'] = label
        state[f'date_{field}'] = ''
    for field in ('us_tax','local_tax'):
        state[f'm_{field}'] = True
        state[f'v_{field}'] = x['tax']*100
        state[f'note_{field}'] = label
        state[f'date_{field}'] = ''
    local_override = x.get('spread_local_override')
    state['m_local_spread'] = local_override is not None
    state['v_local_spread'] = None if local_override is None else local_override*100
    state['note_local_spread'] = label
    state['date_local_spread'] = ''
    state['loaded_note'] = label
    state.pop('bundle', None)


def reset_document(state) -> None:
    for key in list(state):
        if key not in ('offline', 'scenario_upload'):
            del state[key]


def input_control(st, field: str, label: str, obs: dict | None, *, beta=False, optional=False) -> tuple[float | None, dict]:
    """Show source independently of the active override. Blank manual values stay missing."""
    if obs:
        fetched = f"{obs['value']:.4f}" if beta else f"{obs['value']*100:.4f}%"
        st.caption(f"Source: {fetched} · observation {obs['observation_date']}")
    elif not optional:
        st.caption('Source unavailable · enter a manual value to use this input')
    else:
        st.caption('Automatic model spread is active unless you enable a manual override.')
    manual = st.checkbox(f'Manual {label.lower()}', key=f'm_{field}')
    if manual:
        val = st.number_input(label + ('' if beta else ' (%)'), value=None, step=.01,
                              format='%.4f', key=f'v_{field}')
        with st.expander('Entry date / note'):
            st.text_input('Observation date (YYYY-MM-DD; optional)', key=f'date_{field}')
            st.text_input('Source or assumption note', key=f'note_{field}')
        period = st.session_state.get(f'date_{field}', '').strip()
        if period:
            try:
                date.fromisoformat(period)
            except ValueError:
                st.error('Use YYYY-MM-DD for the manual observation date.')
                return None, {'provenance':'invalid manual date'}
        return (None if val is None else val if beta else val/100), dict(
            provenance='manual', observation_date=period or 'Not supplied', entered_at=utc_now(),
            note=st.session_state.get(f'note_{field}', '') or 'User override',
            fetched_reference=obs)
    if obs:
        return obs['value'], obs
    return None, dict(provenance='unavailable')


def style(st):
    st.markdown('''<style>
    .stApp {background:#f4f7fb;color:#152b45;font-family:ui-sans-serif,system-ui,sans-serif;}
    .block-container {max-width:1260px;padding-top:4.5rem;padding-bottom:3rem;}
    [data-testid="stSidebar"] {background:#fff;border-right:1px solid #e2e9f1;}
    h1,h2,h3 {color:#122b47;letter-spacing:-.035em;}
    .eyebrow {font-size:.74rem;font-weight:700;letter-spacing:.18em;color:#087f83;margin-bottom:.5rem;}
    .hero {padding:1.8rem 2rem;background:linear-gradient(115deg,#102b49,#145767);border-radius:20px;margin-bottom:1.3rem;color:white;}
    .hero h1 {color:white;font-size:2.3rem;margin:0 0 .5rem;}
    .hero p {color:#d5e8ee;max-width:780px;margin:0;line-height:1.6;}
    .rate-card {background:white;border:1px solid #dce6ee;border-top:4px solid #087f83;border-radius:16px;padding:1.5rem;margin:.5rem 0 1rem;}
    .rate-card.local {border-top-color:#ab7b39;}
    .rate-card .label {font-size:.8rem;color:#577086;letter-spacing:.06em;text-transform:uppercase;}
    .rate-card .number {font-size:3rem;font-weight:700;color:#102b49;letter-spacing:-.05em;}
    .rate-card .foot {font-size:.82rem;color:#577086;}
    [data-testid="stMetric"] {background:white;border:1px solid #e0e8ef;padding:.8rem;border-radius:12px;}
    .stButton>button[kind="primary"] {background:#087f83;border-color:#087f83;}
    [data-testid="stTabs"] {margin-top:1rem;}
    @media(max-width:640px){.hero{padding:1.2rem}.hero h1{font-size:1.7rem}.rate-card .number{font-size:2.5rem}}
    </style>''', unsafe_allow_html=True)


def run_ui():
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    st.set_page_config(page_title='International Cost of Capital', page_icon='🌐', layout='wide')
    style(st)

    @st.cache_resource
    def shared_cache():
        return SourceCache()
    cache = shared_cache()
    state = st.session_state
    state.setdefault('offline', False)
    state.setdefault('erp', 6.5)
    state.setdefault('debt_share', 30.0)
    state.setdefault('tax_basis', 'US federal')

    def load_example():
        apply_document(state, example_scenario(), 'User-supplied illustrative example; not current market data')

    def import_file():
        try:
            doc = parse_scenario(state['scenario_upload'].getvalue())
            apply_document(state, doc, 'Imported frozen scenario; original source metadata retained in export')
            state['import_original_provenance'] = doc['original_provenance']
            state.pop('import_error', None)
        except Exception as exc:
            state['import_error'] = str(exc)

    with st.sidebar:
        st.markdown('### 🌐 Capital Atlas')
        st.caption('INTERNATIONAL COST OF CAPITAL')
        st.toggle('Manual-only mode', key='offline', help='Skip all network calls. Enter your own dated assumptions or load the example.')
        st.button('Load agreed Colombia example', on_click=load_example, width='stretch')
        st.button('Reset assumptions', on_click=lambda: reset_document(state), width='stretch')
        st.file_uploader('Import scenario JSON', type='json', key='scenario_upload', on_change=import_file)
        if state.get('import_error'):
            st.error(state['import_error'])
        force = st.button('↻ Refresh source data', disabled=state['offline'], width='stretch')
        st.caption('Yields: 1-hour cache · other sources: 24 hours. Overrides survive refreshes.')

    sources = {}
    jobs = {'Industries': (load_betas, 86400), 'USD BBB spread': (load_spread, 86400),
            'Country coverage': (load_coverage, 86400), 'US federal tax': (lambda: load_tax('USA', True), 86400)}
    if not state['offline']:
        with st.spinner('Checking public source data…'):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {k: executor.submit(cache.get, k, fn, ttl, force) for k,(fn,ttl) in jobs.items()}
                sources = {k:f.result() for k,f in futures.items()}
    industries = sources.get('Industries', {}).get('data', {})
    coverage = sources.get('Country coverage', {}).get('data', {}).copy()
    # Metadata-only offline catalog: no embedded rates, dates, or industry betas.
    if not coverage:
        import pycountry
        for c in pycountry.countries:
            slug = re.sub(r'[^a-z]+','-', c.name.lower()).strip('-')
            coverage[slug] = dict(name=c.name, slug=slug,
                url=f'https://tradingeconomics.com/{slug}/government-bond-yield')
        for slug,name in [('united-states','United States'),('colombia','Colombia')]:
            coverage[slug] = dict(name=name, slug=slug, url=f'https://tradingeconomics.com/{slug}/government-bond-yield')
    if state.get('import_country'):
        c = state['import_country']; coverage[c['slug']] = c
    state.setdefault('country', 'united-states')
    if state['country'] not in coverage:
        state['country'] = 'united-states'
        coverage['united-states'] = dict(name='United States',slug='united-states',url='https://tradingeconomics.com/united-states/government-bond-yield')

    with st.sidebar:
        st.divider()
        st.markdown('#### 1 · Project assumptions')
        st.selectbox('Country', sorted(coverage, key=lambda k:coverage[k]['name']),
                     format_func=lambda k: coverage[k]['name'], key='country')
        country = coverage[state['country']]
        meta = country_metadata(country['name'])
        if state.get('_last_country') != country['slug']:
            if state.get('_last_country') is not None:
                for f in ('local_yield','local_tax','local_spread'):
                    for prefix in ('m_', 'v_', 'date_', 'note_'):
                        state.pop(prefix+f, None)
                state.pop('loaded_note', None)
                state.pop('import_original_provenance', None)
            state['currency'] = meta['currency']
            state['_last_country'] = country['slug']
        state.setdefault('currency', meta['currency'])
        st.text_input('Local currency code', key='currency', max_chars=3,
                      help='Confirm the three-letter currency of the government bond. Metadata is a convenience and is editable.')
        options = ['Choose an industry…'] + sorted(industries) + ['Manual / supplied beta']
        if state.get('industry') and state['industry'] not in options:
            options.append(state['industry'])
        st.selectbox('Industry', options, key='industry')
        st.number_input('USD equity risk premium (%)', min_value=0.0, step=.1, key='erp')
        st.number_input('Target debt financing (%)', min_value=0.0, max_value=99.99, step=1.0, key='debt_share')
        st.radio('Common project tax rate', ['US federal','Selected country'], key='tax_basis',
                 help='The chosen rate applies to BOTH currency scenarios.')

    us_country = dict(name='United States',slug='united-states',url='https://tradingeconomics.com/united-states/government-bond-yield')
    if not state['offline']:
        extra = {'US 10-year yield': (lambda: load_yield(us_country),3600)}
        if country['slug'] != 'united-states':
            extra[f"{country['name']} 10-year yield"] = (lambda:load_yield(country),3600)
            extra[f"{country['name']} tax"] = (lambda:load_tax(meta['iso3']),86400)
        with st.spinner('Checking selected-country observations…'):
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures={k:executor.submit(cache.get,k,fn,ttl,force) for k,(fn,ttl) in extra.items()}
                sources.update({k:f.result() for k,f in futures.items()})
    us_yield = sources.get('US 10-year yield',{}).get('data')
    local_yield = us_yield if country['slug']=='united-states' else sources.get(f"{country['name']} 10-year yield",{}).get('data')
    us_tax = sources.get('US federal tax',{}).get('data')
    local_tax = us_tax if country['slug']=='united-states' else sources.get(f"{country['name']} tax",{}).get('data')
    spread = sources.get('USD BBB spread',{}).get('data')

    st.markdown('''<div class="hero"><div class="eyebrow" style="color:#83d6cc">CAPITAL ATLAS / VALUATION WORKBENCH</div>
    <h1>One project. Two currency lenses.</h1>
    <p>Build a transparent cost of capital from industry risk, financing choices and government-yield proxies. Every input has a source—or an explicit assumption.</p></div>''',unsafe_allow_html=True)
    st.caption(f"{meta['flag']} {country['name']} · USD ↔ {state.get('currency') or 'local currency'} · Common tax, leverage and business-risk assumptions")
    if state.get('loaded_note'):
        st.info(state['loaded_note'])
    if not sources.get('Country coverage',{}).get('data'):
        st.caption('Country list is a metadata-only manual catalog; live Trading Economics coverage is unavailable or manual-only mode is enabled.')
    errors = [k for k,v in sources.items() if v.get('error')]
    if errors:
        st.warning('Some sources need attention: '+', '.join(errors)+'. Cached values, if any, remain available. See Data & sources.')
    stale = [k for k,v in sources.items() if v.get('error') and v.get('data') is not None]
    if stale:
        st.warning('Using last successful cached observations for: '+', '.join(stale))
    aged_yields = []
    for label, obs in [('US', us_yield), (country['name'], local_yield)]:
        if obs and (datetime.now(timezone.utc).date()-date.fromisoformat(obs['observation_date'])).days > 7:
            aged_yields.append(f"{label}: {obs['observation_date']}")
    if aged_yields:
        st.warning('Yield observations are more than seven calendar days old: '+', '.join(aged_yields)+'. Review or override before use.')

    with st.expander('2 · Review data & manual overrides', expanded=state.get('bundle') is None):
        cols = st.columns(3)
        with cols[0]:
            st.markdown('#### Business risk & tax')
            beta, p_beta = input_control(st,'beta','Unlevered beta',industries.get(state['industry']),beta=True)
            t_us, p_tus = input_control(st,'us_tax','US federal tax',us_tax)
            t_local, p_tlocal = input_control(st,'local_tax','Selected-country tax',local_tax)
        with cols[1]:
            st.markdown('#### Government yield proxies')
            rf_us, p_us = input_control(st,'us_yield','US government yield',us_yield)
            rf_local, p_local = input_control(st,'local_yield','Local government yield',local_yield)
        with cols[2]:
            st.markdown('#### Corporate borrowing spreads')
            s_us, p_sus = input_control(st,'us_spread','USD BBB spread',spread)
            st.caption('Local spread is calculated as USD spread × (1 + local yield)/(1 + US yield).')
            if s_us is not None and rf_us is not None and rf_local is not None and rf_us > -1:
                st.caption(f'Calculated local spread: {s_us*(1+rf_local)/(1+rf_us)*100:.4f}%')
            s_local, p_slocal = input_control(st,'local_spread','Local spread',None,optional=True)
            if not state.get('m_local_spread'):
                p_slocal = dict(provenance='calculated',formula='spread_us × q')
            st.caption('Spreads are percentage points above the government-yield proxy. Example: enter 1.5 for a 1.5 percentage-point spread.')

    tax = t_us if state['tax_basis']=='US federal' else t_local
    provenance = dict(beta=p_beta, us_tax=p_tus, local_tax=p_tlocal,
                      active_tax_basis=state['tax_basis'], us_yield=p_us, local_yield=p_local,
                      us_spread=p_sus, local_spread=p_slocal,
                      source_refresh={k:{a:v.get(a) for a in ('last_attempt','last_success','error')} for k,v in sources.items()})
    if state.get('import_original_provenance'):
        provenance['imported_original_provenance'] = state['import_original_provenance']
    settings = dict(country_slug=country['slug'],country_name=country['name'],industry=state['industry'],
                    currency=state['currency'].upper(),tax_basis=state['tax_basis'])
    values=dict(beta_u=beta,debt_share=state['debt_share']/100,tax=tax,erp=state['erp']/100,
                rf_us=rf_us,rf_local=rf_local,spread_us=s_us,spread_local_override=s_local)
    missing=[k for k,v in values.items() if v is None and k!='spread_local_override']
    if state.get('m_local_spread') and s_local is None:
        missing.append('local spread override')
    if settings['industry']=='Choose an industry…':
        missing.append('industry selection')
    if not re.fullmatch('[A-Z]{3}',settings['currency']):
        missing.append('three-letter local currency code')
    current_key = digest(dict(inputs=values,settings=settings))
    if st.button('Calculate cost of capital →',type='primary',width='stretch'):
        if missing:
            st.error('Complete these inputs: '+', '.join(missing)+'. Enable manual entry where a source is unavailable.')
        else:
            try:
                x=Inputs(**values).validated()
                rows=industry_comparison(industries,x)
                doc=scenario_document(x,settings,provenance,rows)
                state['bundle']=dict(document=doc,key=current_key)
            except (ValueError,TypeError) as exc:
                st.error(str(exc))
    if state.get('bundle') and state['bundle']['key']!=current_key:
        st.info('Assumptions changed. Calculate again to update results and downloads.')
    bundle = state.get('bundle')
    # Hide obsolete results and downloads rather than exporting a different visible scenario.
    if bundle and bundle['key']==current_key:
        render_results(st,pd,go,bundle['document'])
    elif not bundle:
        st.markdown('### Make the assumptions yours')
        st.write('Choose an industry, review the source values, and calculate. To explore without live data, load the agreed Colombia example from the sidebar.')

    render_sources(st,pd,sources)
    with st.expander('Financial methodology & approximations'):
        for a in ASSUMPTIONS:
            st.markdown('- '+a)
        st.write('For fixed perpetual debt, PV(tax shield) = τD and VL = VU + τD under restrictive assumptions. This app estimates rates; it does not value tax shields or optimize capital structure. Changing leverage while holding BBB credit quality fixed is a sensitivity, not an optimal-financing recommendation.')
        st.write('Two countries sharing a currency can have different government yields. Under this proxy model that produces different rates even though the currency is the same; do not interpret the difference as currency depreciation alone.')
    st.caption('Sources: Aswath Damodaran / NYU Stern · OECD Corporate Tax Statistics · Trading Economics. Model estimates depend on the disclosed assumptions.')


def render_results(st,pd,go,doc):
    x=Inputs(**doc['inputs']); r=doc['results']; settings=doc['settings']; code=settings['currency']
    st.markdown('### The cost of financing your project')
    a,b=st.columns(2)
    for col,title,rate,css in [(a,'🇺🇸 USD',r['wacc_us'],''),(b,f"{country_metadata(settings['country_name'])['flag']} {code}",r['wacc_local'],'local')]:
        with col:
            st.markdown(f'<div class="rate-card {css}"><div class="label">{html.escape(title)} · weighted average cost of capital</div><div class="number">{rate:.4%}</div><div class="foot">{x.debt_share:.0%} debt · {x.tax:.2%} common project tax</div></div>',unsafe_allow_html=True)
    if abs(x.rf_us)> .25 or abs(x.rf_local)>.25 or x.debt_share>.8 or x.tax>.6 or abs(x.beta_u)>5:
        st.warning('One or more assumptions are unusually large. Check percentage units and whether the model remains appropriate.')
    if any(r[k]<0 for k in ('wacc_us','wacc_local','rd_us','rd_local','re_us','re_local')):
        st.warning('Some calculated financing rates are negative. They have not been floored or silently changed; review the inputs.')
    if settings['country_slug']=='united-states' and (x.rf_us!=x.rf_local or x.spread_local_override is not None):
        st.info('US identity comparison has independent manual yield/spread assumptions. Equal yields and no local spread override are required for identical results.')
    if re.search(r'bank|insurance|financial|brokerage|investment',settings['industry'],re.I):
        st.warning('Financial-sector leverage has a different economic meaning. Standard corporate WACC/Hamada results require special care.')
    tabs=st.tabs(['Comparison','Calculation walkthrough','All industries','Sensitivity','Export scenario'])
    with tabs[0]:
        metrics=[('Government yield proxy',x.rf_us,x.rf_local),('Equity cost',r['re_us'],r['re_local']),
                 ('Corporate borrowing spread',x.spread_us,r['spread_local']),('Pretax debt cost',r['rd_us'],r['rd_local']),
                 ('After-tax debt cost',r['rd_after_us'],r['rd_after_local']),('WACC',r['wacc_us'],r['wacc_local'])]
        st.dataframe(pd.DataFrame([{'Metric':n,'USD':f'{u:.4%}',code+' (local)':f'{v:.4%}'} for n,u,v in metrics]),hide_index=True,width='stretch')
        c=st.columns(3)
        c[0].metric('Relevered beta',f"{r['beta_e']:.6f}")
        c[1].metric('Debt / equity',f"{r['de']:.6f}")
        c[2].metric('Currency adjustment q',f"{r['q']:.6f}")
        fig=go.Figure()
        fig.add_bar(name='Equity contribution',x=['USD',f'{code} · local'],y=[100*r['equity_contribution_us'],100*r['equity_contribution_local']],marker_color='#087f83')
        fig.add_bar(name='After-tax debt contribution',x=['USD',f'{code} · local'],y=[100*r['debt_contribution_us'],100*r['debt_contribution_local']],marker_color='#a3c8cf')
        fig.update_layout(barmode='relative',height=320,yaxis_title='Contribution to WACC (percentage points)',margin=dict(l=10,r=10,t=15,b=10),paper_bgcolor='rgba(0,0,0,0)',legend=dict(orientation='h',y=1.15))
        st.plotly_chart(fig,width='stretch')
        st.caption(f"USD ERP {x.erp:.2%} → implied local ERP {r['implied_local_erp']:.2%}. Local spread is {'manually entered' if x.spread_local_override is not None else 'a calculated synthetic estimate'}.")
    with tabs[1]:
        st.markdown('#### 1 · Convert financing weights and relever beta')
        st.latex(r'\frac{D}{E}=\frac{d}{1-d},\qquad \beta_E=\beta_U\left[1+(1-\tau)\frac{d}{1-d}\right]')
        st.code(f"D/E = {x.debt_share:.6f} / (1 − {x.debt_share:.6f}) = {r['de']:.6f}\nβE = {x.beta_u:.6f} × [1 + (1 − {x.tax:.6f}) × {r['de']:.6f}] = {r['beta_e']:.6f}")
        st.markdown('#### 2 · Estimate USD financing costs')
        st.latex(r'R_e^{USD}=R_f^{USD}+\beta_E ERP,\qquad R_d^{USD}=R_f^{USD}+s_{USD}')
        st.write(f"Equity: {x.rf_us:.4%} + {r['beta_e']:.6f} × {x.erp:.4%} = **{r['re_us']:.4%}**. Debt: {x.rf_us:.4%} + {x.spread_us:.4%} = **{r['rd_us']:.4%}**.")
        st.markdown('#### 3 · Translate equity and pretax debt costs')
        st.latex(r'q=\frac{1+R_f^{LC}}{1+R_f^{USD}},\qquad R_e^{LC}=(1+R_e^{USD})q-1')
        st.latex(r's_{LC}=q s_{USD},\qquad R_d^{LC}=R_f^{LC}+s_{LC}')
        st.write(f"q = (1 + {x.rf_local:.6f}) / (1 + {x.rf_us:.6f}) = {r['q']:.6f}. Local equity cost = **{r['re_local']:.4%}**. Automatic local spread = **{r['spread_local_auto']:.4%}**; active local spread = **{r['spread_local']:.4%}**.")
        st.markdown('#### 4 · Apply tax, then combine financing costs')
        st.latex(r'WACC=(1-d)R_e+dR_d(1-\tau)')
        for label,suffix in [('USD','us'),(code+' local','local')]:
            st.write(f"**{label}:** {1-x.debt_share:.2%} × {r['re_'+suffix]:.4%} + {x.debt_share:.2%} × {r['rd_'+suffix]:.4%} × (1 − {x.tax:.2%}) = **{r['wacc_'+suffix]:.4%}**")
        st.info('The tax adjustment is applied after converting pretax debt costs. The model does not convert aggregate WACC.')
    with tabs[2]:
        rows=doc['industry_comparison']
        if rows:
            frame=pd.DataFrame(rows)
            view=frame[['industry','beta_u','beta_e','wacc_us','wacc_local']].copy()
            view.columns=['Industry','Unlevered beta','Relevered beta','USD WACC','Local WACC']
            st.dataframe(view.style.format({'Unlevered beta':'{:.4f}','Relevered beta':'{:.4f}','USD WACC':'{:.4%}','Local WACC':'{:.4%}'}),hide_index=True,width='stretch')
            st.caption('Each row retains its fetched industry beta. Tax, leverage, yields, ERP and spreads are held common. Financial industries require special interpretation.')
        else:
            st.info('Industry source data are unavailable. The selected manual scenario still works; no comparison betas are invented.')
    with tabs[3]:
        st.caption('Hold yields, tax and BBB credit quality fixed. This is a scenario analysis, not an optimal leverage calculation.')
        debt_values=[i/100 for i in range(0,81,10)]
        erp_values=[max(0,x.erp-.02)+i*.005 for i in range(9)]
        z=[[calculate(replace(x,debt_share=d,erp=e))['wacc_local']*100 for d in debt_values] for e in erp_values]
        fig=go.Figure(go.Heatmap(x=[v*100 for v in debt_values],y=[v*100 for v in erp_values],z=z,colorscale='Teal',colorbar=dict(title='WACC %'),hovertemplate='Debt %{x:.0f}%<br>USD ERP %{y:.2f}%<br>Local WACC %{z:.4f}%<extra></extra>'))
        fig.update_layout(xaxis_title='Target debt share (%)',yaxis_title='USD equity risk premium (%)',height=400,margin=dict(l=20,r=20,t=15,b=20))
        st.plotly_chart(fig,width='stretch')
    with tabs[4]:
        st.caption('Downloads describe the calculated scenario, including decimal-rate units, active assumptions, source dates and manual provenance. Imported scenarios freeze resolved numeric inputs as explicit manual overrides.')
        st.download_button('Download results CSV',result_csv(doc),'cost_of_capital_results.csv','text/csv',width='stretch')
        st.download_button('Download scenario JSON',json.dumps(doc,indent=2,ensure_ascii=False,allow_nan=False),'cost_of_capital_scenario.json','application/json',width='stretch')
        if doc['industry_comparison']:
            frame=pd.DataFrame(doc['industry_comparison'])
            for k,v in doc['inputs'].items():
                if k!='beta_u':frame['input_'+k]=v
            frame['country']=settings['country_name'];frame['currency']=code
            frame['scenario_provenance_json']=json.dumps(doc['provenance'],ensure_ascii=False)
            st.download_button('Download all industries CSV',frame.to_csv(index=False).encode('utf-8-sig'),'all_industries.csv','text/csv',width='stretch')
        st.json({'inputs':doc['inputs'],'settings':doc['settings'],'provenance':doc['provenance']},expanded=False)


def render_sources(st,pd,sources):
    with st.expander('Data & sources · observation dates and refresh status'):
        rows=[]
        for name,record in sources.items():
            data=record.get('data')
            if data and isinstance(data,dict) and 'value' not in data:
                sample=next(iter(data.values()),{})
            else:sample=data or {}
            status='Stale cache · refresh failed' if record.get('error') and data is not None else 'Unavailable' if record.get('error') else 'Fetched / cached'
            rows.append({'Dataset':name,'Status':status,'Observation':sample.get('observation_date','Not applicable'),
                         'Publication/update':sample.get('publication_date') or 'Not separately supplied',
                         'Last success (UTC)':record.get('last_success','Never'),
                         'Last refresh attempt (UTC)':record.get('last_attempt','Never')})
            if record.get('error'):st.caption(f"{name}: {record['error']}")
        if rows:st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
        else:st.info('Manual-only mode: no network calls are made.')
        st.markdown(f'[US industry beta workbook]({BETA_URL}) · [BBB spread table]({RATING_URL}) · [OECD tax database]({TAX_SOURCE}) · [Trading Economics bonds]({BONDS_URL}) · [Country yield coverage]({COVERAGE_URL})')
        st.caption('Fetching today does not change the date of an annual observation. Cache lasts only for this server process; a restart may require refetching or manual entry. Country/currency metadata are labels, not market observations.')


# Embedded acceptance checks: fixtures are synthetic, not embedded source observations.
class ModelTests(unittest.TestCase):
    def setUp(self):
        self.x=Inputs(.8,.3,.35,.065,.04,.12,.015)

    def test_agreed_example(self):
        r=calculate(self.x)
        for k,v in dict(beta_e=1.022857142857143,re_us=.1064857142857143,
                         re_local=.1916,rd_local=.1361538461538462,
                         rd_after_local=.0885,wacc_us=.085265,wacc_local=.16067).items():
            self.assertAlmostEqual(r[k],v,12,k)
        self.assertNotAlmostEqual(r['wacc_local'],(1+r['wacc_us'])*r['q']-1,6)

    def test_zero_debt_and_tax(self):
        r=calculate(replace(self.x,debt_share=0))
        self.assertEqual(r['beta_e'],self.x.beta_u)
        self.assertEqual(r['wacc_local'],r['re_local'])
        r=calculate(replace(self.x,tax=0))
        self.assertEqual(r['rd_after_local'],r['rd_local'])
        self.assertAlmostEqual(r['wacc_local'],(1+r['wacc_us'])*r['q']-1)

    def test_identity_and_override(self):
        r=calculate(replace(self.x,rf_local=self.x.rf_us))
        self.assertAlmostEqual(r['wacc_us'],r['wacc_local'])
        self.assertAlmostEqual(r['re_us'],r['re_local'])
        o=calculate(replace(self.x,spread_local_override=.025))
        self.assertAlmostEqual(o['rd_local'],.145)
        self.assertEqual(o['re_local'],calculate(self.x)['re_local'])

    def test_validation_and_negative_yields(self):
        for kwargs in [dict(debt_share=1),dict(tax=1),dict(debt_share=-.1),dict(erp=float('nan')),
                       dict(rf_us=-1),dict(spread_us=-.01),dict(beta_u=True),dict(rf_local=float('inf'))]:
            with self.assertRaises(ValueError):calculate(replace(self.x,**kwargs))
        self.assertTrue(math.isfinite(calculate(replace(self.x,rf_local=-.01))['wacc_local']))

    def test_scenario_and_csv(self):
        doc=example_scenario()
        parsed=parse_scenario(json.dumps(doc).encode())
        self.assertEqual(calculate(Inputs(**parsed['inputs'])),doc['results'])
        rows=list(csv.reader(io.StringIO(result_csv(doc).decode('utf-8-sig'))))
        self.assertTrue(any(row[1]=='wacc_local' and abs(float(row[2])-.16067)<1e-10 for row in rows[1:]))
        for replacement in [dict(model='other'),dict(schema_version=999)]:
            with self.assertRaises(ValueError):parse_scenario(json.dumps(dict(doc,**replacement)).encode())

    def test_cache_retains_success_and_cools_failure(self):
        cache=SourceCache(); calls=[]
        def success():calls.append(1);return {'value':.04}
        def fail():calls.append(2);raise ValueError('Synthetic outage')
        a=cache.get('source',success,3600)
        b=cache.get('source',fail,3600,True)
        self.assertEqual(a['data'],b['data']);self.assertIn('Synthetic outage',b['error'])
        c=cache.get('source',success,3600)
        self.assertEqual(len(calls),2);self.assertEqual(c['data'],a['data'])
        self.assertNotIn('data',cache.get('missing',fail,3600))

    def test_import_and_override_persistence(self):
        state={}
        apply_document(state,example_scenario(),'Example')
        self.assertEqual(state['v_local_yield'],12.)
        self.assertTrue(state['m_beta'])
        self.assertFalse(state['m_local_spread'])
        before=copy.deepcopy(state)
        cache=SourceCache();cache.get('refresh',lambda:{'value':.99},3600,True)
        self.assertEqual(state,before)

    def test_industries_keep_own_beta(self):
        rows=industry_comparison({'A':{'value':.5,'observation_date':'2026','url':'fixture'},
                                  'B':{'value':1.2,'observation_date':'2026','url':'fixture'}},self.x)
        self.assertEqual([row['beta_u'] for row in rows],[.5,1.2])
        self.assertEqual(rows[0]['wacc_local'],calculate(replace(self.x,beta_u=.5))['wacc_local'])

    def test_source_parsers_fail_closed(self):
        for func,args in [(parse_spread,('<p>no data</p>',)),(parse_coverage,('<p>no data</p>',)),
                          (parse_yield,('<title>Colombia 2-Year</title>',{'name':'Colombia','url':'fixture'})),
                          (parse_tax_csv,('bad,columns\n1,2','USA',True,'fixture'))]:
            with self.assertRaises(ValueError):func(*args)

    def test_yield_parser_uses_current_metadata(self):
        text='''<title>Colombia 10-Year Government Bond Yield</title><p>Unit percent. Forecast 99%</p>
        <script>TEChartsMeta = [{"type":"Bond","name":"Colombia 10Y","last":12.0}];</script>
        <script type="application/ld+json">{"@type":"Dataset","name":"Colombia 10-Year",
        "temporalCoverage":"2000-01-01/2025-01-02","dateModified":"20250102"}</script>'''
        obs=parse_yield(text,{'name':'Colombia','url':'fixture'})
        self.assertEqual(obs['value'],.12);self.assertEqual(obs['observation_date'],'2025-01-02')
        with self.assertRaises(ValueError):parse_yield(text.replace('temporalCoverage','unknown'),{'name':'Colombia','url':'fixture'})

    def test_spread_and_coverage_parsers(self):
        text='''<p>For large non-financial service firms. Data as of January 2025</p>
        <table><tr><td>2.5</td><td>3</td><td>Baa2/BBB</td><td>1.50%</td><td>0.9</td><td>1.2</td><td>Baa2/BBB</td><td>9.99%</td></tr></table>'''
        self.assertEqual(parse_spread(text)['value'],.015)
        self.assertEqual(list(parse_coverage('<a href="/colombia/government-bond-yield">Colombia</a>')),['colombia'])

    def test_tax_exact_definition(self):
        header='REF_AREA,FREQ,MEASURE,TARGETING,UNIT_MEASURE,SECTOR,DIV_TREATMENT,RATE_STRUCTURE,TAX_BASE,TIME_PERIOD,OBS_VALUE,UNIT_MULT\n'
        text=header+'USA,A,CIT,ST,PT_INC_TAX,S1311,_Z,_Z,_Z,2025,21,0\nUSA,A,CIT_C,ST,PT_INC_TAX,S13,_Z,_Z,_Z,2025,27,0\n'
        self.assertEqual(parse_tax_csv(text,'USA',True,'fixture')['value'],.21)
        self.assertEqual(parse_tax_csv(text,'USA',False,'fixture')['value'],.27)
        with self.assertRaises(ValueError):parse_tax_csv(text,'COL',False,'fixture')


def check_live():
    checks={'industry_betas':load_betas,'bbb_spread':load_spread,'country_coverage':load_coverage,
            'us_federal_tax':lambda:load_tax('USA',True),'colombia_tax':lambda:load_tax('COL'),
            'us_yield':lambda:load_yield(dict(name='United States',url='https://tradingeconomics.com/united-states/government-bond-yield')),
            'colombia_yield':lambda:load_yield(dict(name='Colombia',url='https://tradingeconomics.com/colombia/government-bond-yield'))}
    report={}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures={name:ex.submit(fn) for name,fn in checks.items()}
        for name,future in futures.items():
            try:
                data=future.result()
                report[name]=dict(status='PASS',data=data if 'value' in data else {'count':len(data),'sample':dict(list(data.items())[:2])})
            except Exception as exc:report[name]=dict(status='FAIL',error=str(exc))
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 1 if any(v['status']=='FAIL' for v in report.values()) else 0


if __name__=='__main__':
    if '--self-test' in sys.argv:
        unittest.main(argv=[sys.argv[0]],verbosity=2)
    elif '--check-live' in sys.argv:
        raise SystemExit(check_live())
    else:
        run_ui()
