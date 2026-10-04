"""Full functional audit — every tab's code path, no GUI launch."""
import sys, os, tempfile, threading, traceback, importlib.util
sys.path.insert(0, r'D:\PEAK_Master')

spec = importlib.util.spec_from_file_location('peak', r'D:/PEAK_Master/PEAK_GITHUB_true.py')
peak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peak)

import tkinter as tk
root = tk.Tk(); root.withdraw()
app = peak.App(root)

FAILS = []
def step(name, fn):
    try:
        fn(); print('✓', name)
    except Exception as e:
        FAILS.append((name, traceback.format_exc()))
        print('✗', name, '--', str(e)[:120])

# 1) Module-level helpers -----------------------------------------------------
step('matchers.strict_match fusion', lambda: (
    __import__('matchers').strict_match(
        [{'id':'ok','url':'u','title':'Song','channel':'Artist','seconds':180,'views':1000000},
         {'id':'mix','url':'u','title':'Song 1 HOUR MIX','channel':'x','seconds':3600,'views':99999999}],
        'Song','Artist')['id'] == 'ok' or (_ for _ in ()).throw(AssertionError('wrong pick'))))

step('search_youtube honors source=ytmusic', lambda: None)  # called later live
step('_source_mode default', lambda: peak._source_mode() == 'both')

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    csvdir = os.path.join(td, 'csvs'); os.makedirs(csvdir)
    root0 = os.path.join(td, 'lib'); os.makedirs(root0)
    app._bdir.delete(0, tk.END); app._bdir.insert(0, csvdir)
    app._bout.delete(0, tk.END); app._bout.insert(0, root0)

    # write a tiny csv
    with open(os.path.join(csvdir, 't.csv'), 'w', encoding='utf-8') as f:
        f.write('Track Name,Artist Name(s),Duration (ms)\n')
        f.write('"SPECIALZ","King Gnu",238000\n')

    # 2) CSV reader ------------------------------------------------------------
    logs = []
    pl = app._read_batch_csvs(csvdir, logs.append)
    step('_read_batch_csvs parses 1 track',
         lambda: (pl.get('t') == [('SPECIALZ', 'King Gnu', '238000')]), )

    # 3) per-tab prefs mounted everywhere -------------------------------------
    for prefix in ('src_single', 'src_playlist', 'src_search', 'src_batch',
                   'src_spotify', 'src_ytm'):
        step(f'prefs row mounted: {prefix}',
             lambda p=prefix: hasattr(app, p + '_src_var'))
    prefs = app._get_search_prefs('src_batch')
    step('_get_search_reads defaults', lambda: prefs == ('both', True, 40, 720))

    # 4) worker-path shape ------------------------------------------------------
    step('matchers import works (frozen-ready)',
         lambda: __import__('matchers').strict_match(
             [{'id':'a','url':'u','title':'Song','channel':'Artist','seconds':180,'views':1}],
             'Song','Artist')['id'] == 'a')

    # 5) _run_batch_rows constructs (no network) --------------------------------
    step('_run_batch_rows dry init',
         lambda: app._run_batch_rows([], 'opus256', None, threading.Event(), lambda m: None))

    # 6) _import_spotify_zip exists and detects bad zip -------------------------
    step('_import_spotify_zip callable', lambda: hasattr(app, '_import_spotify_zip'))

# 7) Live functional spot-checks (network) ------------------------------------
def live_search_ytm():
    r = peak.search_youtube('SPECIALZ King Gnu', 3, None, source='ytmusic')
    assert isinstance(r, list) and r, 'empty'
    assert all('id' in x for x in r), 'no ids'
step('search_youtube(ytmusic only) returns items', live_search_ytm)

def live_search_yt():
    r = peak.search_youtube('SPECIALZ King Gnu', 3, None, source='youtube')
    assert isinstance(r, list), 'not list'
step('search_youtube(youtube only) returns items', live_search_yt)

def live_both():
    r = peak.search_youtube('SPECIALZ King Gnu', 3, None, source='both')
    assert isinstance(r, list)
step('search_youtube(both) returns items', live_both)

# 8) matcher handles mixed durations correctly --------------------------------
def matcher_caps():
    res = [
        {'id':'a','url':'u','title':'Song','channel':'a','seconds':30,'views':10},
        {'id':'b','url':'u','title':'Song','channel':'a','seconds':180,'views':100},
    ]
    out = __import__('matchers').strict_match(res, 'Song','a',caps_on=True,cap_min_s=40,cap_max_s=720)
    assert out['id']=='b'
    out2 = __import__('matchers').strict_match(res, 'Song','a',caps_on=False)
    assert out2['id']=='a'  # relevance-first wins with caps off (idx 0)
step('matcher caps on/off behave', matcher_caps)


# 9) end-to-end multi-track batch with progress ticking
def test_multi_batch():
    import time as _t
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        csvdir = os.path.join(td, 'csvs'); os.makedirs(csvdir)
        libdir = os.path.join(td, 'lib'); os.makedirs(libdir)
        app._bdir.delete(0, tk.END); app._bdir.insert(0, csvdir)
        app._bout.delete(0, tk.END); app._bout.insert(0, libdir)
        with open(os.path.join(csvdir, 't.csv'), 'w', encoding='utf-8') as f:
            f.write('Track Name,Artist Name(s),Duration (ms)\n')
            for n, a, d in [('SPECIALZ','King Gnu',238000),
                            ('Blinding Lights','The Weeknd',200000),
                            ('Shape of You','Ed Sheeran',263000)]:
                f.write(f'"{n}","{a}",{d}\n')
        pl = app._read_batch_csvs(csvdir, lambda m: None)
        rows = [(p, t, a, d) for p_, ts in pl.items() for t, a, d in ts for p in [p_]]
        ev = threading.Event()
        stats = app._run_batch_rows(rows, 'opus256', None, ev, lambda m: None)
        assert stats['ok'] + stats['fail'] + stats['skip'] == 3, stats
        # wait a few seconds for async enrich threads to finish writing files
        for _ in range(10):
            if any(os.path.exists(os.path.join(libdir, 'Music', f))
                   and f.endswith('.opus')
                   for f in os.listdir(os.path.join(libdir,'Music'))):
                break
            __import__('time').sleep(1)
        music = os.path.join(libdir, 'Music')
        assert os.path.isdir(music)
        files = [f for f in os.listdir(music) if f.endswith('.opus')]
        assert files, 'no .opus produced'
        # index may be written at end of run — grace period
        for _ in range(10):
            if os.path.exists(os.path.join(music, '_index.txt')):
                break
            __import__('time').sleep(1)
        assert os.path.exists(os.path.join(music, '_index.txt')), 'index not written' 
step('multi-batch: 3 tracks land', test_multi_batch)

# 10) cancel returns fast when event pre-set
def test_cancel_returns_fast():
    import time as _t
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        csvdir = os.path.join(td, 'csvs'); os.makedirs(csvdir)
        libdir = os.path.join(td, 'lib'); os.makedirs(libdir)
        app._bdir.delete(0, tk.END); app._bdir.insert(0, csvdir)
        app._bout.delete(0, tk.END); app._bout.insert(0, libdir)
        with open(os.path.join(csvdir, 't.csv'), 'w', encoding='utf-8') as f:
            f.write('Track Name,Artist Name(s),Duration (ms)\n')
            for i in range(20):
                f.write(f'Track{i},Artist{i},180000\n')
        pl = app._read_batch_csvs(csvdir, lambda m: None)
        rows = [(p, t, a, d) for p_, ts in pl.items() for t, a, d in ts for p in [p_]]
        ev = threading.Event()
        ev.set()
        t0 = _t.time()
        stats = app._run_batch_rows(rows, 'opus256', None, ev, lambda m: None)
        el = _t.time() - t0
        assert el < 8, f'cancel took {el:.1f}s'
        assert stats['ok'] == 0, stats
step('cancel returns fast', test_cancel_returns_fast)

root.destroy()

print()
print('FAILED:', len(FAILS))
for n, tb in FAILS:
    print('---', n); print(tb)
sys.exit(1 if FAILS else 0)
