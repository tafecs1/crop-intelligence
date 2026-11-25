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


def test_22_extraction_gate_blocks_missing_maps():
    """v4.2: if crop-map / trend extraction failed, the run must stop – never publish tehsils as fallow."""
    import pathlib, pytest
    src = pathlib.Path(__file__).resolve().parents[1].joinpath('crop_intel.py').read_text()
    a = src.index('# v4.2 extraction completeness gate')
    b = src.index("'the Earth Engine error, fix it and re-run (completed batches are cached).')") + len("'the Earth Engine error, fix it and re-run (completed batches are cached).')")
    gate = src[a:b]
    import os as _os, tempfile
    tmp = tempfile.mkdtemp()
    base = dict(os=_os, WORK_DIR=tmp, TODAY='2026-11-30', BUDGET_HIT=False, MISSING_SHARDS=0, _batches=[['1', '2', '3']], TREND_STEPS=12, MIN_EXTRACT_COVERAGE=0.95,
                SAT_MONTHLY={'1': {}, '2': {}, '3': {}}, SAT_CUR={k: {'crop_share': 0.5} for k in '123'}, SAT_WX={})
    ok = dict(base, SAT_MAPS={k: {'active_frac': 0.4} for k in '123'}, SAT_TREND={k: {'NDVI_T00': 0.3} for k in '123'})
    exec(gate, dict(ok))                                         # complete → passes
    bad = dict(base, SAT_MAPS={}, SAT_TREND={})
    with pytest.raises(SystemExit):
        exec(gate, dict(bad))                                    # maps/trend missing → stops
    assert not _os.path.exists(_os.path.join(tmp, 'RESUME_NEEDED'))   # an Earth Engine error is NOT auto-resumed
    with pytest.raises(SystemExit):
        exec(gate, dict(bad, BUDGET_HIT=True))                   # v4.5: time budget used → stops AND asks to resume
    assert _os.path.exists(_os.path.join(tmp, 'RESUME_NEEDED'))
    _os.remove(_os.path.join(tmp, 'RESUME_NEEDED'))
    with pytest.raises(SystemExit):
        exec(gate, dict(bad, MISSING_SHARDS=2))                  # v4.7: a crashed shard → stop AND resume
    assert _os.path.exists(_os.path.join(tmp, 'RESUME_NEEDED'))


def test_24_rice_layers_run_at_field_scale():
    """v4.3: every paddy layer must be forced onto the RICE_SCALE_M grid (100 m averaging hid most delta paddy),
    the cache signature must include the scale, and an upper-bound reference must never read as 'within reference'."""
    import pathlib, re
    src = pathlib.Path(__file__).resolve().parents[1].joinpath('crop_intel.py').read_text()
    assert re.search(r'^RICE_SCALE_M\s*=\s*20\b', src, re.M)
    body = src[src.index('def rice_layers('):src.index('IDX6 = ')]
    ret = body[body.index('    return dict('):]
    for k in ('est', 'recent', 'harv', 'pend', 'ft', 'ft_recent', 'ft_harv', 'obs', 'v30'):
        assert re.search(rf'\b{k}=_fs\(', ret), k
    assert 'bins=[_fs(b) for b in bins]' in ret
    assert 'return _fs(est_all.Or(' in body                      # calibration layers too
    assert 'RICE_SCALE_M' in src[src.index('RICE_SIG = '):src.index('GEO_AREA_HA = ')]
    assert "'within reference'" not in src.replace("bare 'within reference'", '')


def test_25_shards_split_batches_within_one_state():
    """v4.4: a one-state run must spread its batches over the shards, and the shards together must equal the full batch list."""
    import pathlib, pandas as pd, os as _os
    src = pathlib.Path(__file__).resolve().parents[1].joinpath('crop_intel.py').read_text()
    a = src.index('# GitHub: batches are built per state')
    b = src.index("print(f'Shard {SHARD_INDEX + 1}/{SHARD_COUNT}: {len(_batches)} batches")
    code = src[a:src.index('\n', b) + 1]
    teh = pd.DataFrame({'state': ['TAMIL NADU'] * 291, 'lgd': [str(i) for i in range(291)]})
    def run(count, idx):
        _os.environ['SHARD_COUNT'], _os.environ['SHARD_INDEX'] = str(count), str(idx)
        ns = dict(os=_os, TEH=teh, BATCH_SIZE=100); exec(code, ns); return ns['_batches']
    full = run(1, 0)
    parts = [run(3, k) for k in range(3)]
    assert all(len(p) == 1 for p in parts)                          # 3 batches → one per shard
    assert sorted(map(tuple, sum(parts, []))) == sorted(map(tuple, full))
    _os.environ['SHARD_COUNT'], _os.environ['SHARD_INDEX'] = '1', '0'


def test_26_quota_savers():
    """v4.4: S1 paddy work is masked to cropland; the v3.0 diagnostic runs only in calibration runs; extraction has a time budget."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath('crop_intel.py').read_text()
    body = src[src.index('def rice_layers('):src.index('IDX6 = ')]
    assert ".updateMask(CROPLAND)" in body and 'if not RICE_CALIBRATE:' in body
    assert "EXTRACT_BUDGET_MIN = float(os.environ.get('EXTRACT_BUDGET_MIN'" in src and 'BUDGET_HIT = True' in src


def test_27_resume_workflow_wiring():
    """v4.5: the workflow re-dispatches itself (same run date, capped) only when the build job reports RESUME_NEEDED."""
    import pathlib, yaml
    wf = yaml.safe_load(pathlib.Path(__file__).resolve().parents[1].joinpath('.github/workflows/cropintel.yml').read_text())
    inputs = wf[True]['workflow_dispatch']['inputs']
    assert {'run_date', 'resume_attempt'} <= set(inputs)
    assert wf['jobs']['build']['outputs']['resume'] == '${{ steps.chk.outputs.resume }}'
    r = wf['jobs']['resume']
    assert "needs.build.outputs.resume == 'true'" in r['if'] and '< 3' in r['if']
    assert r['permissions'] == {'actions': 'write'}
    cmd = r['steps'][0]['run']
    assert 'gh workflow run cropintel.yml' in cmd and 'run_date="${{ needs.setup.outputs.run_date }}"' in cmd
    setup = wf['jobs']['setup']['steps'][0]['run']
    assert 'github.event.inputs.run_date' in setup


def test_28_monthly_cache_reuse_and_pruning():
    """v4.6: half-year groups are calendar-aligned (finished half-years keep their cache key next month); pruning keeps
    same-day files and in-window half-years, drops other run dates, out-of-window months and other runs' resume flags."""
    import pathlib, re, datetime as dt, tempfile, os as _os, itertools
    src = pathlib.Path(__file__).resolve().parents[1].joinpath('crop_intel.py').read_text()
    g = src[src.index('_mgroups = '):].split('\n', 1)[0]
    def add_months(d, k):
        y, m = divmod(d.month - 1 + k, 12); return dt.date(d.year + y, m + 1, 1)
    keys = []
    for cur in (dt.date(2026, 11, 1), dt.date(2026, 12, 1)):
        ns = dict(MONTHS=[add_months(cur, -i) for i in range(23, -1, -1)], __import__=__import__)
        exec(g, ns); keys.append({f'{a[0]:%Y%m}_{a[-1]:%Y%m}' for a in ns['_mgroups']})
    assert {'202501_202506', '202507_202512', '202601_202606'} <= keys[0] & keys[1]
    body = src[src.index('def prune_cache():'):src.index('prune_cache()\n', src.index('def prune_cache():'))]
    d = tempfile.mkdtemp()
    names = {'M_202501_202506_aaaaaaaaaaaa.json': True, 'M_202412_202412_bbbbbbbbbbbb.json': True,
             'M_202301_202306_cccccccccccc.json': False, 'M_202607_202611_2026-11-25_dddddddddddd.json': True,
             'TREND6_2026-11-25_ab12cd34_eeeeeeeeeeee.json': True, 'TREND6_2026-11-18_ab12cd34_ffffffffffff.json': False,
             'WX_2026-10-02_000000000000.json': False, '_budget_hit_99_0.flag': False, '_budget_hit_42_1.flag': True}
    for n in names:
        open(_os.path.join(d, n), 'w').write('[]')
    ns = dict(os=_os, re=re, CACHE_DIR=d, TODAY=dt.date(2026, 11, 25), RUN_TAG='42', MONTHS_BACK=24,
              MONTHS=[add_months(dt.date(2026, 11, 1), -i) for i in range(23, -1, -1)])
    exec(body, ns); ns['prune_cache']()
    left = set(_os.listdir(d))
    assert left == {n for n, keep in names.items() if keep}, left ^ {n for n, keep in names.items() if keep}


def test_29_shard_handoff_before_exit_and_schedule_guards():
    """v4.6: the time-budget flag is written BEFORE the extract-only exit (v4.5 bug), shards upload only used files, and the
    schedule option (monthly default = all India, weekly/both via CROPINTEL_SCHEDULE) gates every job that does work."""
    import pathlib, yaml
    root = pathlib.Path(__file__).resolve().parents[1]
    src = root.joinpath('crop_intel.py').read_text()
    assert src.index('_budget_hit_{RUN_TAG}_') < src.index("print(f'Extraction-only shard finished")
    assert 'shutil.copy2(p_, _out)' in src and 'CACHE_USED.add(p)' in src
    wf = yaml.safe_load(root.joinpath('.github/workflows/cropintel.yml').read_text())
    crons = [c['cron'] for c in wf[True]['schedule']]
    assert crons == ['30 20 1 * *', '30 20 * * 0']
    j = wf['jobs']
    for k in ('setup', 'tests'):
        assert "vars.CROPINTEL_SCHEDULE != 'weekly'" in j[k]['if'] and "vars.CROPINTEL_SCHEDULE == 'both'" in j[k]['if']
    assert "needs.setup.result == 'success'" in j['build']['if']
    assert "github.event.schedule == '30 20 1 * *' && 'ALL'" in wf['env']['STATE_FILTER']
    up = [s for s in j['extract']['steps'] if s.get('uses', '').startswith('actions/upload-artifact')][0]
    assert up['with']['path'] == 'work/shard_out'
    assert any(s.get('uses', '').startswith('actions/cache/save') for s in j['build']['steps'])
    assert j['keepalive']['if'] == "${{ github.event.schedule == '30 20 * * 0' }}"


def test_30_earth_engine_429_waits_and_crashed_shards_resume():
    """v4.7: Earth Engine 'Too Many Requests / concurrency limit' is retried with back-off (other errors still raise), and a
    shard without a done-flag makes the build job resume instead of extracting itself."""
    import pathlib, types, time as _t, os as _os, tempfile
    src = pathlib.Path(__file__).resolve().parents[1].joinpath('crop_intel.py').read_text()
    blk = src[src.index('import random as _rnd'):src.index('ee.data.computeValue = _compute_value_patient')]
    calls = {'n': 0}
    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] < 3:
            raise RuntimeError('Too Many Requests: Exceeded Earth Engine concurrency limit. Your project is in Restricted Mode.')
        return 'ok'
    ee = types.SimpleNamespace(data=types.SimpleNamespace(computeValue=flaky))
    ns = dict(os=_os, time=types.SimpleNamespace(time=_t.time, sleep=lambda s: None), ee=ee)
    exec(blk, ns)
    assert ns['_compute_value_patient']() == 'ok' and calls['n'] == 3
    def broken(*a, **k):
        raise RuntimeError('Image.reduceRegions: bad band')
    ns['_ee_compute_value'] = broken
    with pytest.raises(RuntimeError):
        ns['_compute_value_patient']()
    d = tempfile.mkdtemp()
    for k in (0, 2):
        open(_os.path.join(d, f'_shard_done_77_{k}.flag'), 'w').write('x')
    a = src.index('def _done_flags():'); b = src.index('SHARD_FLAGGED = bool(_budget_flags())')
    env = dict(_os.environ); _os.environ['SHARD_TOTAL'] = '3'
    try:
        ns2 = dict(os=_os, CACHE_DIR=d, RUN_TAG='77'); exec(src[a:b], ns2)
    finally:
        _os.environ.clear(); _os.environ.update(env)
    assert ns2['MISSING_SHARDS'] == 1
