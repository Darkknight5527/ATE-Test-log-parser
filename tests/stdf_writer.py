"""Tiny STDF V4 writer for the tests: fake datalogs with known chips, fuse words and retests (no real data)."""
import struct


def _cn(s):
    b = s.encode('latin1')
    return bytes([len(b)]) + b


def _rec(typ, sub, body):
    return struct.pack('<HBB', len(body), typ, sub) + body


def far():
    return _rec(0, 10, bytes([2, 4]))


def mir(lot, job, start, part='DEMO_PART'):
    body = struct.pack('<IIB', start, start, 1) + b'P' + b' ' + b' ' + struct.pack('<H', 0xFFFF) + b' '
    for s in (lot, part, 'node1', 'DEMO', job, 'A', '', 'op', 'SIM', '1.0', 'FT', '25C'):
        body += _cn(s)
    return _rec(1, 10, body)


def ptr(num, name, value, lo=None, hi=None, unit='V', failed=False):
    flg = 0x80 if failed else 0
    body = struct.pack('<IBBBBf', num, 1, 1, flg, 0, value) + _cn(name)
    opt = 0x0E | (0x40 if lo is None else 0) | (0x80 if hi is None else 0)
    body += _cn('') + bytes([opt]) + struct.pack('<bbbff', 0, 0, 0, lo or 0.0, hi or 0.0) + _cn(unit)
    return _rec(15, 10, body)


def prr(pid, hb, sb, failed, ntest, tt=1000):
    flag = 0x08 if failed else 0
    body = struct.pack('<BBBHHHhhI', 1, 1, flag, ntest, hb, sb, -32768, -32768, tt) + _cn(str(pid))
    return _rec(5, 20, body)


def sbr(num, pf, name):
    return _rec(1, 50, struct.pack('<BBHI', 255, 0, num, 0) + pf.encode() + _cn(name))


def chip(pid, read, word, post, ok=True, early_fail=False, bits=12):
    """One PIR..PRR block. read: None = the ID read is not reached, 0 = not programmed yet, else the fuse word.
    word: the fuse word written as per-bit resistances (blown = 5000 ohm, intact = 50 ohm). post: >= 20 values."""
    out = [_rec(5, 10, bytes([1, 1])), ptr(1, 'CONT_open', 0.5, -1.0, 1.0, failed=early_fail)]
    n = 1
    if early_fail:
        return b''.join(out + [prr(pid, 3, 3, True, n)])
    out.append(ptr(2, 'ID_Read_ID', float(read or 0), 0, 1e15, unit=''))
    n += 1
    for b in range(bits):
        out.append(ptr(10 + b, 'IDBITS_BIT%d' % b, 5000.0 if (word >> b) & 1 else 50.0, unit='ohm'))
        n += 1
    for i, v in enumerate(post):
        out.append(ptr(100 + i, 'POST_meas%d' % i, v, -100, 100, failed=(not ok and i == 0)))
        n += 1
    return b''.join(out + [prr(pid, 1 if ok else 4, 1 if ok else 4, not ok, n)])


def write(path, lot, job, start, blocks):
    data = far() + mir(lot, job, start) + b''.join(blocks)
    data += sbr(1, 'P', 'PASS') + sbr(3, 'F', 'CONT_FAIL') + sbr(4, 'F', 'POST_FAIL')
    data += _rec(1, 20, struct.pack('<I', start + 600))
    with open(path, 'wb') as fh:
        fh.write(data)
