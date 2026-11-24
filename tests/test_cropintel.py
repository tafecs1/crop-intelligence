"""CropIntel v4.0 automated tests (no Earth Engine needed).

Run:  pip install -r requirements.txt pytest && pytest -q tests
The synthetic harness runs the real cells of crop_intel.py (2, 4-5b, 7b/7b2/7c definitions, 8-11b and the Excel part of 12) on
10 made-up tehsils, so every test below exercises production code, not a copy of it.
"""
import datetime as dt
import json
import os
import re

import pytest

from harness import build

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'crop_intel.py')


@pytest.fixture(scope='session')
def ns():
    return build(SRC)


@pytest.fixture(scope='session')
def T1(ns):
    return [dict(zip(ns['T1_HEADER'], r)) for r in ns['T1_ROWS']]


@pytest.fixture(scope='session')
def T8(ns):
    return [dict(zip(ns['T8_HEADER'], r)) for r in ns['T8_ROWS']]


def _dates(rows, cols):
    for r in rows:
        for c in cols:
            if r.get(c):
                yield c, str(r[c])[:10]


# TEST 1 – no epoch dates
def test_01_no_epoch_dates(ns, T1):
    cols = ['Stage Observation Date', 'Latest S2 Acquisition Date', 'Latest S1 Acquisition Date', 'Latest Usable Observation Date',
            'Sowing Date Estimated', 'Crop Stage Start Estimate']
    for c, v in _dates(T1, cols):
        assert not v.startswith('1970'), (c, v)
        assert dt.date.fromisoformat(v) >= dt.date(2014, 4, 3), (c, v)
    assert '1970' not in ' '.join(str(r['S2 Scenes / S1 Scenes']) for r in T1)
    assert ns['valid_obs_date'](-20000, ns['TODAY']) is None             # epoch-like offsets are rejected
    src = open(SRC).read()                                                 # root cause guard: masked S2 keeps its acquisition time
    mask_fn = src[src.index('def _mask_s2'):src.index('def s2_collection')]
    assert "copyProperties(img, ['system:time_start'" in mask_fn


# TEST 2 – no generic cropland-curve label as a final crop stage
def test_02_no_generic_stage(ns, T1):
    bad = ('cropland curve estimate', 'season calendar estimate', 'calendar estimate)', 'steady green canopy')
    for r in T1:
        assert not any(b in str(r['Final Crop Stage']) for b in bad), r['Final Crop Stage']
    for r in ns['IDX_ROWS']['NDVI']:
        assert not any(b in str(r[8]) for b in bad)


# TEST 3 – rice stages never on other crops; every non-rice stage is from that crop's own table
def test_03_no_rice_stage_on_other_crops(ns, T1):
    rice_names = {t[0] for t in ns['rice_stage_table'](110)}
    own = {m: {t[0] for t in d['stages']} for m, d in ns['CROP_MODELS'].items()}
    for r in T1:
        base = str(r['Final Crop Stage']).split(' (')[0].split(' →')[0]
        if not str(r['Crop']).startswith('Rice'):
            assert base not in rice_names - set().union(*own.values()), r
    # unit: an observed planting date + groundnut table can only give groundnut stage names
    c = dict(crop='Groundnut', group='Oilseeds', presence='Probable crop identification')
    out = ns['resolve_unconfirmed'](c, 'Groundnut', ns['CROP_MODELS']['Groundnut']['stages'], ns['TODAY'] - dt.timedelta(days=50),
                                    True, 130, 'GROUND', '', [], False, [])
    assert out[0].split(' (')[0] in own['Groundnut'] and out[2] == 'model'


# TEST 4 – perennials never get grain-filling (cereal) stages
def test_04_perennial_no_grain_stage(T1):
    for r in T1:
        if 'Perennial' in str(r['Crop']) or 'Sugarcane' in str(r['Crop']):
            assert not re.search(r'grain|milk|dough|heading|silking|tassel', str(r['Final Crop Stage']), re.I), r


# TEST 5 – unidentified cropland gets no crop-specific stage
def test_05_unidentified_no_stage(ns, T1):
    for r in T1:
        if r['Crop Presence Status'] in ('Active cropland, crop unknown', 'Candidate – flooded, no canopy rise yet'):
            assert r['Final Crop Stage'] == ns['U_IDENTITY'], r
    c = dict(crop='Active cropland – crop unknown', group='Unidentified', presence='Active cropland, crop unknown')
    assert ns['resolve_unconfirmed'](c, None, None, None, False, None, '', '', [], False, [])[0] == ns['U_IDENTITY']


# TEST 6 – every record has a stage or an explicit unresolved status (+ verification status)
def test_06_explicit_stage_status(ns, T1):
    for r in T1:
        assert str(r['Final Crop Stage']).strip() and r['Final Crop Stage'] != ns['UNC'], r
        assert r['Stage Verification Status'] in ('Satellite-observed (not field-verified)', 'Model-inferred (not satellite-confirmed)',
                                                  'Field-verified', 'Satellite-observed', 'Unresolved')
        if r['Stage Verification Status'] == 'Unresolved':
            assert r['Final Crop Stage'] in ns['UNRESOLVED'] and r['Stage Unresolved Reason / Next Action']


# TEST 7 – no invented sowing / harvest dates
def test_07_no_invented_dates(ns, T1, T8):
    for r in T1:
        if 'Recommended sowing window only' in str(r['Sowing Date Evidence']):
            assert r['Sowing Date Estimated'] == '' and r['Sowing Date Confidence'] == ''
    for r in T8:                                     # district calendar: observed fields only from tehsils with an observed planting
        if not r['Tehsils with Observed Planting']:
            assert r['Observed Planting Date (district median)'] == '' and r['Expected Harvest (from observed planting)'] == ''
    c = dict(crop='Maize', group='Cereals', presence='Probable crop identification')
    out = ns['resolve_unconfirmed'](c, 'Maize', ns['CROP_MODELS']['Maize']['stages'], ns['TODAY'] - dt.timedelta(days=30),
                                    False, 110, 'MAIZE', '', [], False, [])
    assert out[0] == ns['U_NOSOW']                                          # calendar-only planting → no stage


# TEST 8 – area overlap / double counting
def test_08_no_double_counting(ns):
    for a in ns['AREA_ROWS']:
        assert a['Crop-specific identified ha'] <= a['Active cultivated ha'] + max(1, 0.01 * a['Active cultivated ha']), a


# TEST 9 – monthly index consistency
def test_09_monthly_index_consistency(ns):
    H = ns['IDX_HEADER']
    for idx in ns['IDX6']:
        for r in ns['IDX_ROWS'][idx]:
            d = dict(zip(H, r))
            last = d[f"{ns['MONTH_LABELS'][-1]} Value"]
            assert d['Latest Value'] == last
            prev = d[f"{ns['MONTH_LABELS'][-2]} Value"]
            if last is not None and prev is not None:
                assert abs(d['Latest MoM Δ (index points)'] - round(last - prev, 4)) < 1e-3
            assert d['Valid Months (out of 24)'] <= 24


# TEST 10 – no misleading % changes
def test_10_no_invalid_percent(ns):
    assert ns['pct_idx'](-0.40, -0.50) is None and ns['pct_idx'](0.5, 0.01) is None and ns['pct_idx'](0.55, 0.5) == 10.0
    H = ns['IDX_HEADER']
    for idx in ns['IDX6']:
        for r in ns['IDX_ROWS'][idx]:
            for k in range(1, len(ns['MONTH_LABELS'])):
                v, prev = r[ns['VAL_COL'][k] + 1], r[ns['VAL_COL'][k - 1]]
                if v is not None:
                    assert prev is not None and prev >= 0.05


# TEST 11 – observation dates match acquisition records
def test_11_obs_dates_match(T1):
    for r in T1:
        if r['Stage Observation Date']:
            assert r['Latest Usable Observation Date'] and str(r['Stage Observation Date']) <= str(r['Latest Usable Observation Date'])
        if r['Latest Usable Observation Date']:
            assert int(r['Days Since Observation']) >= 0


# TEST 12 – ideal windows consistent with calendar + stage model
def test_12_ideal_windows(T8):
    for r in T8:
        sw, hw = r['Ideal Sowing Window (current / next cycle)'], r['Ideal Harvest Window']
        if ' to ' in sw and ' to ' in hw:
            s0 = dt.datetime.strptime(sw.split(' to ')[0], '%d-%b-%Y').date()
            h0 = dt.datetime.strptime(hw.split(' to ')[0], '%d-%b-%Y').date()
            dur = r['Ideal Crop Duration Days']
            assert h0 > s0, r
            if '-' in dur:
                assert (h0 - s0).days == int(dur.split('-')[0]), r     # harvest = sowing + minimum duration
        assert r['Calendar Source'] and r['Source Confidence (High / Medium / Low)'] in ('High', 'Medium', 'Low')


# TEST 13 – machinery only for a valid operation, crop-specific
def test_13_machinery(ns, T8):
    assert ns['machinery_for'](None, 'Harvest') == ('None', '', '')
    cat, op, eq = ns['machinery_for']('RICE', 'Harvest')
    assert cat == 'Harvesting' and 'combine' in eq.lower()
    assert 'groundnut' in ns['machinery_for']('Groundnut', 'Harvest')[2].lower()
    for r in T8:
        m = r['Machinery Need – Next Stage (Tillage / Seeding / Spraying / Harvesting / None)']
        if m != 'None':
            assert r['Machinery Recommendation (crop & operation)'] and r['Next Operation']


# TEST 14 – consistent geographic keys across sheets
def test_14_geo_keys(ns):
    keys = [{r[5] for r in ns['T1_ROWS']}] + [{r[5] for r in ns['IDX_ROWS'][i]} for i in ns['IDX6']]
    assert all(k == keys[0] for k in keys)
    assert {r[3] for r in ns['T8_ROWS']} <= set(ns['TEH'].dlgd)          # Tab 8 is district level


# TEST 15 – model version + evidence on every record
def test_15_traceability(ns, T1):
    for r in T1:
        assert r['Model Version'] == ns['MODEL_VERSION'] and r['Source'] and r['Stage Estimation Method'] and r['Run ID']


# Regression – baseline workbook structure (v3.5 "Crop Intelligence (1).xlsx")
def test_16_regression_headers(ns):
    base = json.load(open(os.path.join(os.path.dirname(__file__), 'baseline_headers.json')))
    assert ns['T1_HEADER'][:len(base['Crop_Intelligence_Current'])] == base['Crop_Intelligence_Current']
    assert 'Tehsil LGD Code' not in ns['T8_HEADER'] and ns['T8_HEADER'][:4] == base['Ideal_Crop_Window'][:4]   # v4.1 district level
    assert ns['IDX_HEADER'][:len(base['NDVI_MoM'])] == base['NDVI_MoM']
    assert [t[0] for t in ns['TABS']] == base['_tabs']


def test_17_in_pipeline_acceptance_all_pass(ns):
    assert not any(ns['ACCEPT'].values()), {k: v for k, v in ns['ACCEPT'].items() if v}


# v4.0.1 – regressions for the issues found by the independent audit of RUN-20261124-073719
def test_18_stage_far_from_days_after_planting_is_rejected(ns):
    tab = ns['CROP_MODELS']['Maize']['stages']
    names = [t[0] for t in tab]
    k_silk = next(k for k, n in enumerate(names) if n.startswith('Silking'))
    dap = 30                                     # early vegetative
    assert ns['_dap_inconsistent'](tab, names[k_silk], dap)
    assert not ns['_dap_inconsistent'](tab, names[ns['stage_index_at'](tab, dap)], dap)


def test_19_no_duplicate_tehsil_crop(ns):
    from collections import Counter
    assert not [k for k, n in Counter((x[5], x[7]) for x in ns['T1_ROWS']).items() if n > 1]


def test_20_unidentified_area_not_double_counted(ns):
    for lgd in {x[5] for x in ns['T1_ROWS']}:
        rows = [x for x in ns['T1_ROWS'] if x[5] == lgd]
        bal = next(a for a in ns['AREA_ROWS'] if str(a['Tehsil LGD Code']) == str(lgd))
        tot = sum(x[12] or 0 for x in rows if x[9] != 'Candidate – flooded, no canopy rise yet')
        assert tot <= bal['Active cultivated ha'] + max(1, 0.01 * bal['Active cultivated ha']), (lgd, tot, bal)


def test_21_harvest_follows_observed_stage(ns):
    H8 = ns['T8T_HEADER']
    for x in ns['T8T_ROWS']:
        d = dict(zip(H8, x))
        if d['Expected Harvest Date (adjusted)'] and str(d['Expected Harvest Date (adjusted)']) < str(ns['TODAY']):
            assert any(w in str(d['Final Crop Stage (current)']) for w in ('Harvest', 'aturity', 'unresolved'))


# v4.1 – district crop calendar
def test_22_calendar_file_integrity(ns):
    import pandas as pd
    cal = pd.read_csv(os.path.join(ROOT, 'data', 'tn_district_crop_calendar.csv'), dtype=str, keep_default_na=False)
    for r in cal.itertuples():
        assert 1 <= int(r.sow_month_start) <= 12 and 1 <= int(r.sow_month_end) <= 12
        assert r.source and r.source_confidence in ('High', 'Medium', 'Low')
        if r.duration_min:
            assert int(r.duration_min) <= int(r.duration_max)
        if r.districts not in ('ALL', 'ALL_RICE') and not r.districts.startswith('ALL_EXCEPT:'):
            for d in r.districts.split(';'):
                assert ns['_dk'](d) in ns['TN_DISTRICTS'], d
        if r.source_confidence != 'High':
            assert r.note, r                                   # every non-High row says why


def test_23_district_rows_and_observations(ns, T8):
    rice_thj = [r for r in T8 if r['District'] == 'Thanjavur' and r['Crop'] == 'Rice']
    assert {r['Season (Pattam)'] for r in rice_thj} >= {'Kuruvai', 'Samba', 'Late Samba / Thaladi / Pishanam'}
    assert not [r for r in T8 if r['District'] == 'Thanjavur' and r['Crop'] == 'Rice' and r['Season (Pattam)'] == 'Sornavari']
    obs = [o for o in ns['OBS_TEH'] if o['crop'] == 'Rice']
    shown = sum(int(r['Tehsils with Observed Planting'] or 0) for r in T8 if r['Crop'] == 'Rice')
    assert shown <= len(obs)
    for r in T8:
        if r['Tehsils with Observed Planting']:
            assert r['Observed Planting Date (district median)'] and r['Overall Timing Status (Early / On Time / Late)'] in ('Early', 'On Time', 'Late')
