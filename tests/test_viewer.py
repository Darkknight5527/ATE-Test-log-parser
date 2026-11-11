"""Tests on generated (fake) STDF files only - no real tester data."""
import contextlib, io, os, random, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import pytest
import stdf_writer as w
import profiles
import stdf_lot_yield_summary as S

PROFILE = dict(name='Test device', readback_test='ID_Read_ID', fuse_bits_prefix='IDBITS',
               fuse_bit_regex=r'BIT(?P<bit>\d+)', match_tests=['POST_'], device_regex=r'(DEV\d)')
T0 = 1_700_000_000


@pytest.fixture(autouse=True)
def _profile():
    profiles.apply(PROFILE)
    yield
    profiles.apply({})


def make_lot(d, n=10, hot_skip=None, cold=None):
    """n chips at Room (all pass); Hot tests all but `hot_skip`; Cold (optional) tests the chips in `cold`."""
    rnd = random.Random(5)
    words = [rnd.randrange(1, 4096) for _ in range(n)]
    posts = [[rnd.uniform(-50, 50) for _ in range(24)] for _ in range(n)]
    room = [w.chip(i + 1, 0, words[i], posts[i]) for i in range(n)]
    w.write(os.path.join(d, 'LOT1_DEV1_ROOM_T1.stdf'), 'LOT1', 'PROG_DEV1', T0, room)
    hot = [w.chip(k + 1, words[i], words[i], [v * 1.01 for v in posts[i]])
           for k, i in enumerate(x for x in range(n) if x != hot_skip)]
    w.write(os.path.join(d, 'LOT1_DEV1_HOT_T1.stdf'), 'LOT1', 'PROG_DEV1_HOT', T0 + 3600, hot)
    if cold:
        cc = [w.chip(k + 1, words[i], words[i], [v * 0.99 for v in posts[i]]) for k, i in enumerate(cold)]
        w.write(os.path.join(d, 'LOT1_DEV1_COLD_T1.stdf'), 'LOT1', 'PROG_DEV1_COLD', T0 + 7200, cc)
    return words, posts


def analyze(d):
    with contextlib.redirect_stdout(io.StringIO()):
        return S.analyze(S.collect_stdf_files(d), S.Options(), log=lambda *a: None)


def test_profile_reads_fuse_word(tmp_path):
    words, _ = make_lot(str(tmp_path))
    f = S.read_stdf(os.path.join(str(tmp_path), 'LOT1_DEV1_ROOM_T1.stdf'), S.Options())
    assert [r.word for r in f.runs] == words
    assert S.device_variant(f) == 'DEV1'


def test_generic_profile_has_no_fuse_id(tmp_path):
    make_lot(str(tmp_path))
    profiles.apply({})
    f = S.read_stdf(os.path.join(str(tmp_path), 'LOT1_DEV1_ROOM_T1.stdf'), S.Options())
    assert all(r.word is None and not r.post for r in f.runs)
    assert S.device_variant(f) == '?'


def test_passed_chip_tested_again_counts_once(tmp_path):
    d = str(tmp_path)
    words, posts = make_lot(d)
    again = [w.chip(1, words[3], words[3], [v + 0.0001 for v in posts[3]])]      # chip 4 had passed - tested again
    w.write(os.path.join(d, 'LOT1_DEV1_ROOM_RETEST1.stdf'), 'LOT1', 'PROG_DEV1', T0 + 1800, again)
    tr, fin, files, sk = analyze(d)
    assert fin['lot_size'] == 10 and fin['good'] == 10


def test_stage_gaps_on_hold_and_skipped(tmp_path):
    d = str(tmp_path)
    make_lot(d, hot_skip=6, cold=list(range(10)))       # chip 7 never went to Hot, but was tested at Cold
    tr, fin, files, sk = analyze(d)
    assert fin['hold'] == 1
    gaps = S.summary_dict(tr, fin, files, sk)['gaps']
    assert len(gaps) == 1 and gaps[0]['kind'] == 'missing' and gaps[0]['stage'] == 'Hot'
    assert gaps[0]['title'].startswith('Skipped Hot') and 'Cold' in gaps[0]['detail']
    assert any('Skipped Hot' in str(r) for r in tr.exc.rows)


def test_failed_chip_still_tested_later(tmp_path):
    d = str(tmp_path)
    rnd = random.Random(9)
    words = [rnd.randrange(1, 4096) for _ in range(10)]
    posts = [[rnd.uniform(-50, 50) for _ in range(24)] for _ in range(10)]
    room = [w.chip(i + 1, 0, words[i], posts[i], ok=(i != 2)) for i in range(10)]     # chip 3 fails Room
    w.write(os.path.join(d, 'LOT1_DEV1_ROOM_T1.stdf'), 'LOT1', 'PROG_DEV1', T0, room)
    hot = [w.chip(i + 1, words[i], words[i], posts[i]) for i in range(10)]             # ... yet goes to Hot
    w.write(os.path.join(d, 'LOT1_DEV1_HOT_T1.stdf'), 'LOT1', 'PROG_DEV1_HOT', T0 + 3600, hot)
    tr, fin, files, sk = analyze(d)
    gaps = [g for g in S.summary_dict(tr, fin, files, sk)['gaps'] if g['kind'] == 'after_fail']
    assert len(gaps) == 1 and gaps[0]['stage'] == 'Hot'
    assert fin['good'] == 9


def test_report_with_per_file_sheets(tmp_path):
    import openpyxl
    import file_sheets
    d = str(tmp_path)
    make_lot(d)
    tr, fin, files, sk = analyze(d)
    out = os.path.join(d, 'report.xlsx')
    with contextlib.redirect_stdout(io.StringIO()):
        S.export_multi_stage_excel(out, tr, fin, files + sk, sk,
                                   extra_sheets=lambda wb: file_sheets.add_file_sheets(wb, files, log=lambda *a: None))
    wb = openpyxl.load_workbook(out)
    per_file = [n for n in wb.sheetnames if n[:3] in ('01_', '02_')]
    assert per_file == ['01_Room_1114_2213', '02_Hot_1114_2313']
    ws = wb[per_file[1]]
    texts = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith('chip H')]
    assert len(texts) == 10 and all('(from R' in t for t in texts)


def test_profile_check_and_signature():
    assert profiles.check({'fuse_bit_regex': '('}) .startswith('fuse_bit_regex')
    assert 'bit' in profiles.check({'fuse_bit_regex': r'BIT(\d+)'})
    assert profiles.check(PROFILE) == ''
    assert profiles.signature(PROFILE) != profiles.signature({})
