"""End-to-end tests on generated (fake) STDF files - no real tester data."""
import os, random, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stdf_writer as w
from ate_log_parser import profile as prof
from ate_log_parser.report import build
from ate_log_parser.lot_trace import trace, stage_totals
from ate_log_parser.stdf_reader import read_stdf, add_fingerprint

PROFILE = dict(prof.DEFAULT, name='test', readback_test='Read_ID',
               blown_bit_regex=r'ID_BITS:BIT(?P<bit>\d+)_(?P<un>UN)?SET', compare_suites=['POST'],
               device_regex=r'(DEV\d)')
T0 = 1_700_000_000


def make_lot(d):
    rnd = random.Random(7)
    words = [0b10110001, 0b01110010, 0b11100100, 0b10011000, 0b00111001]
    posts = [[rnd.uniform(-5, 5) for _ in range(8)] for _ in words]
    # part spread for the distance scale: add 15 other good parts with their own words/values
    extra_words = [0b1000000 + i for i in range(15)]
    extra_posts = [[rnd.uniform(-5, 5) for _ in range(8)] for _ in extra_words]
    room = []
    for i, (wd, pv) in enumerate(zip(words, posts), 1):
        if i == 3:   # part 3 fails, tester retests it straight away (same Part ID), then passes
            room.append(w.part(i, 0, wd, pv, ok=False)); room.append(w.part(i, wd, wd, pv, ok=True, retest=True))
        else:
            room.append(w.part(i, 0, wd, pv))
    room.append(w.part(6, None, 0, [], early_fail=True))          # contact fail, never programmed
    for j, (wd, pv) in enumerate(zip(extra_words, extra_posts), 7):
        room.append(w.part(j, 0, wd, pv))
    w.write(os.path.join(d, 'LOTX_DEV1_ROOM_1.stdf'), 'LOTX', 'PROG_DEV1_Room', T0, room)
    rt = [w.part(1, None, 0, [], early_fail=True),                      # the contact fail again (anonymous retest)
          w.part(2, words[1], words[1], [v + 0.001 for v in posts[1]])]  # part 2 had PASSED - tested again
    w.write(os.path.join(d, 'LOTX_DEV1_ROOM_RETEST.stdf'), 'LOTX', 'PROG_DEV1_Room', T0 + 3600, rt)
    shutil.copy(os.path.join(d, 'LOTX_DEV1_ROOM_1.stdf'), os.path.join(d, 'copy_of_room.stdf'))
    hot = [w.part(k, wd, wd, [v * 1.1 for v in pv]) for k, (wd, pv) in enumerate(zip(words, posts), 1)]
    w.write(os.path.join(d, 'LOTX_DEV1_HOT_1.stdf'), 'LOTX', 'PROG_DEV1_Hot', T0 + 7200, hot)
    return words


def test_reader_and_fingerprint(tmp_path):
    words = make_lot(str(tmp_path))
    f = add_fingerprint(read_stdf(os.path.join(str(tmp_path), 'LOTX_DEV1_ROOM_1.stdf')), PROFILE)
    assert len(f['runs']) == 22                       # 20 parts + 1 tester retest row + 1 contact fail
    assert f['runs'][0]['id_read'] == 0.0 and f['runs'][0]['word'] == words[0]
    assert f['runs'][6]['id_read'] is None               # contact fail never reached the ID read


def test_lot_tracing_counts(tmp_path):
    make_lot(str(tmp_path))
    files = [add_fingerprint(read_stdf(os.path.join(str(tmp_path), n)), PROFILE)
             for n in ('LOTX_DEV1_ROOM_1.stdf', 'LOTX_DEV1_ROOM_RETEST.stdf', 'LOTX_DEV1_HOT_1.stdf')]
    FL, units, role = trace(files, PROFILE)
    tot = {t['stage']: t for t in stage_totals(FL, units)}
    assert tot['Room']['parts'] == 21                 # 20 good + 1 contact fail, nothing double counted
    assert tot['Room']['good_max'] == 20
    assert tot['Room']['pass_runs'] == 21             # adding up files would count part 2 twice
    assert tot['Room']['confirmed'] == 1              # part 2 recognised as a passed part tested again
    assert tot['Hot']['parts'] == 5 and tot['Hot']['good_max'] == 5
    anon = [u for u in units if u.cls.startswith('anonymous retest')]
    assert len(anon) == 1


def test_full_report_and_duplicate_skip(tmp_path):
    make_lot(str(tmp_path))
    out = os.path.join(str(tmp_path), 'report.xlsx')
    outs, lines = build([str(tmp_path)], out, PROFILE, log=lambda s: None)
    assert os.path.exists(out)
    assert any('identical copy' in l for l in lines)
    import openpyxl
    wb = openpyxl.load_workbook(out)
    assert {'Summary', 'Lot Summary', 'Exceptions', 'Part Journeys', 'Unit Ledger', 'Files', 'Method'} <= set(wb.sheetnames)
    ex = wb['Exceptions']
    types = [ex.cell(r, 9).value for r in range(4, ex.max_row + 1)]
    assert 'Passed part tested again' in types


def test_generic_profile_runs_without_ids(tmp_path):
    make_lot(str(tmp_path))
    outs, lines = build([os.path.join(str(tmp_path), 'LOTX_DEV1_ROOM_1.stdf')], os.path.join(str(tmp_path), 'g.xlsx'),
                        prof.load(), log=lambda s: None)
    assert os.path.exists(outs[0])


def test_shared_id_word_at_hot_is_a_new_part_not_a_retest(tmp_path):
    """Two parts share one ID word. At Hot the first one fails; the second one arrives in a later initial (fresh) Hot file.
    It must be counted as the second part's first Hot test - not as a retest of the first part's fail."""
    d = str(tmp_path); rnd = random.Random(3)
    shared = 0b10101010
    others = [0b1000000 + i for i in range(15)]
    pa = [rnd.uniform(-5, 5) for _ in range(8)]; pb = [rnd.uniform(-5, 5) for _ in range(8)]
    op = [[rnd.uniform(-5, 5) for _ in range(8)] for _ in others]
    room = [w.part(1, 0, shared, pa), w.part(2, 0, shared, pb)] + [w.part(3 + i, 0, wd, pv) for i, (wd, pv) in enumerate(zip(others, op))]
    w.write(os.path.join(d, 'LOTY_DEV1_ROOM_1.stdf'), 'LOTY', 'PROG_DEV1_Room', T0, room)
    hot1 = [w.part(1, shared, shared, [v * 1.3 + 0.7 for v in pa], ok=False)] + \
           [w.part(2 + i, wd, wd, pv) for i, (wd, pv) in enumerate(zip(others, op))]
    w.write(os.path.join(d, 'LOTY_DEV1_HOT_1.stdf'), 'LOTY', 'PROG_DEV1_Hot', T0 + 3600, hot1)
    hot2 = [w.part(1, shared, shared, [v * 1.3 - 0.9 for v in pb])]
    w.write(os.path.join(d, 'LOTY_DEV1_HOT_2.stdf'), 'LOTY', 'PROG_DEV1_Hot', T0 + 7200, hot2)
    files = [add_fingerprint(read_stdf(os.path.join(d, n)), PROFILE)
             for n in ('LOTY_DEV1_ROOM_1.stdf', 'LOTY_DEV1_HOT_1.stdf', 'LOTY_DEV1_HOT_2.stdf')]
    FL, units, role = trace(files, PROFILE)
    tot = {t['stage']: t for t in stage_totals(FL, units)}
    assert tot['Hot']['parts'] == 17                  # every Room part reached Hot once
    assert tot['Hot']['good_max'] == 16               # the shared-word part that failed is still a fail
    last_hot = [u for u in units if u.stage == 'Hot'][-1]
    assert last_hot.cls.startswith('first test at this stage')


def test_setup_files_are_listed_not_counted(tmp_path):
    d = str(tmp_path)
    make_lot(d)
    w.write(os.path.join(d, 'LOTX_DEV1_HOT_SANITY.stdf'), 'LOTX', 'PROG_DEV1_Hot', T0 + 9000, [w.part(1, 0b10110001, 0b10110001, [0] * 8)])
    s = {}
    outs, lines = build([d], os.path.join(d, 'r.xlsx'), PROFILE, log=lambda x: None, summary=s)
    assert any('SANITY' in x['why'] for x in s['skipped'])
    assert {t['stage']: t for t in s['stages']}['Hot']['parts'] == 5
