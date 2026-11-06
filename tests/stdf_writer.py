"""Tiny STDF V4 writer for tests: makes fake datalogs with known parts, retests and IDs (no real data)."""
import struct


def _cn(s):
    b = s.encode('latin1'); return bytes([len(b)]) + b


def _rec(typ, sub, body):
    return struct.pack('<HBB', len(body), typ, sub) + body


def far():
    return _rec(0, 10, bytes([2, 4]))


def mir(lot, job, start, part='DEMO_PART', oper='tester'):
    body = struct.pack('<IIB', start, start, 1) + b'P' + b' ' + b' ' + struct.pack('<H', 0xFFFF) + b' '
    for s in (lot, part, 'node1', 'DEMO', job, 'A', '', oper, 'SIM', '1.0', 'FT', '25C'):
        body += _cn(s)
    return _rec(1, 10, body)


def pir():
    return _rec(5, 10, bytes([1, 1]))


def ptr(num, name, value, lo=None, hi=None, unit='V', failed=False, first=True):
    flg = 0x80 if failed else 0
    body = struct.pack('<IBBBBf', num, 1, 1, flg, 0, value) + _cn(name + '[1]')
    if first:
        opt = 0x0E | (0x40 if lo is None else 0) | (0x80 if hi is None else 0)
        body += _cn('') + bytes([opt]) + struct.pack('<bbbff', 0, 0, 0, lo or 0.0, hi or 0.0) + _cn(unit)
    return _rec(15, 10, body)


def prr(pid, hb, sb, failed, ntest, tt=1000, retest=False):
    flag = (0x08 if failed else 0) | (0x01 if retest else 0)
    body = struct.pack('<BBBHHHhhI', 1, 1, flag, ntest, hb, sb, -32768, -32768, tt) + _cn(str(pid))
    return _rec(5, 20, body)


def hbr(num, cnt, pf, name):
    return _rec(1, 40, struct.pack('<BBHI', 255, 0, num, cnt) + pf.encode() + _cn(name))


def mrr(finish):
    return _rec(1, 20, struct.pack('<I', finish))


def part(pid, readback, word, post, ok=True, early_fail=False, retest=False, bits=8):
    """One PIR..PRR block. readback: None (fails before the read), 0 (not programmed) or a number."""
    out = [pir(), ptr(100, 'CONT:contact', 0.5, -1.0, 1.0, failed=early_fail)]
    n = 1
    if early_fail:
        return b''.join(out + [prr(pid, 3, 3, True, n, retest=retest)])
    out.append(ptr(200, 'ID:Read_ID', float(readback), 0, 1e15, unit='')); n += 1
    for b in range(bits):
        un = '' if (word >> b) & 1 else 'UN'
        out.append(ptr(300 + b, 'ID_BITS:BIT%d_%sSET' % (b, un), 1.0, unit='')); n += 1
    for i, v in enumerate(post):
        out.append(ptr(400 + i, 'POST:meas%d' % i, v, -100, 100, failed=(not ok and i == 0))); n += 1
    return b''.join(out + [prr(pid, 1 if ok else 4, 1 if ok else 4, not ok, n, retest=retest)])


def write(path, lot, job, start, blocks, finish=None, hbins=None):
    data = far() + mir(lot, job, start) + b''.join(blocks)
    for num, (cnt, pf, name) in (hbins or {}).items():
        data += hbr(num, cnt, pf, name)
    data += mrr(finish or start + 600)
    with open(path, 'wb') as fh:
        fh.write(data)
