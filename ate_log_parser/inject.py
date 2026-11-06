"""Add a values-only sheet at the end of an existing .xlsx WITHOUT re-saving it through an Excel library,
so drawn shapes, logos, external links and dynamic-array metadata stay exactly as they are."""
import re, zipfile
from xml.sax.saxutils import escape

FONTS = {'n': '<font><sz val="10"/><name val="Arial"/><family val="2"/></font>',
         'b': '<font><b/><sz val="10"/><name val="Arial"/><family val="2"/></font>',
         'w': '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Arial"/><family val="2"/></font>',
         't': '<font><b/><sz val="13"/><color rgb="FF104861"/><name val="Arial"/><family val="2"/></font>',
         's': '<font><b/><sz val="11"/><color rgb="FF104861"/><name val="Arial"/><family val="2"/></font>',
         'i': '<font><i/><sz val="9"/><color rgb="FF595959"/><name val="Arial"/><family val="2"/></font>'}
FILLS = {'none': None, 'hdr': 'FF104861', 'grn': 'FFC6EFCE', 'red': 'FFF8CBAD', 'amb': 'FFFFE699', 'gry': 'FFEDEDED', 'blu': 'FFDDEBF7'}
STYLES = {}
for _f in FONTS:
    for _fl in FILLS:
        STYLES['%s_%s' % (_f, _fl)] = (_f, _fl, 0, False, False)
STYLES.update({'hdrcell': ('w', 'hdr', 0, True, True), 'pct': ('n', 'none', 10, False, False),
               'wrap': ('n', 'none', 0, True, False), 'wrap_amb': ('n', 'amb', 0, True, False),
               'wrap_red': ('n', 'red', 0, True, False), 'wrap_grn': ('n', 'grn', 0, True, False),
               'wrap_gry': ('n', 'gry', 0, True, False), 'i_wrap': ('i', 'none', 0, True, False)})


def col(n):
    s = ''
    while n: n, r = divmod(n - 1, 26); s = chr(65 + r) + s
    return s


def add_sheet(src, dst, sheet_name, cells, widths, freeze=None, heights=None):
    """cells: {(row, col): (value, style name)}. Writes a copy of src with the new sheet appended to dst."""
    zin = zipfile.ZipFile(src)
    order = zin.namelist()
    files = {n: zin.read(n) for n in order}
    zin.close()
    if sheet_name in files['xl/workbook.xml'].decode('utf8'):
        raise ValueError('The traveller already has a sheet called "%s" - remove it first.' % sheet_name)
    st = files['xl/styles.xml'].decode('utf8')

    def append(tag, items):
        nonlocal st
        m = re.search(r'<%s count="(\d+)"[^>]*>' % tag, st)
        base = int(m.group(1))
        pos = st.index('</%s>' % tag, m.end())
        st = st[:pos] + ''.join(items) + st[pos:]
        st = st[:m.start()] + m.group(0).replace('count="%d"' % base, 'count="%d"' % (base + len(items))) + st[m.end():]
        return base
    fkeys = list(FONTS); f0 = append('fonts', [FONTS[k] for k in fkeys])
    flkeys = [k for k in FILLS if FILLS[k]]
    fl0 = append('fills', ['<fill><patternFill patternType="solid"><fgColor rgb="%s"/><bgColor indexed="64"/></patternFill></fill>' % FILLS[k] for k in flkeys])
    names = list(STYLES); xfs = []
    for nm in names:
        fk, flk, nf, wrap, center = STYLES[nm]
        fid = f0 + fkeys.index(fk); flid = fl0 + flkeys.index(flk) if FILLS[flk] else 0
        al = ''
        if wrap or center:
            al = '<alignment%s%s vertical="%s"/>' % (' horizontal="center"' if center else '', ' wrapText="1"' if wrap else '', 'center' if center else 'top')
        xfs.append('<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="0" xfId="0" applyFont="1"%s%s%s>%s</xf>' % (
            nf, fid, flid, ' applyFill="1"' if flid else '', ' applyNumberFormat="1"' if nf else '', ' applyAlignment="1"' if al else '', al))
    x0 = append('cellXfs', xfs)
    sid = {nm: x0 + i for i, nm in enumerate(names)}
    files['xl/styles.xml'] = st.encode('utf8')
    rows = {}
    for (r, c), (v, s) in cells.items(): rows.setdefault(r, {})[c] = (v, s)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
           '<sheetViews><sheetView workbookViewId="0" zoomScale="90">']
    if freeze:
        fr, fc = freeze
        out.append('<pane%s%s topLeftCell="%s%d" activePane="bottomRight" state="frozen"/>' % (
            ' xSplit="%d"' % (fc - 1) if fc > 1 else '', ' ySplit="%d"' % (fr - 1) if fr > 1 else '', col(fc), fr))
    out.append('</sheetView></sheetViews><sheetFormatPr defaultRowHeight="13.2"/>')
    out.append('<cols>' + ''.join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i, i, w) for i, w in enumerate(widths, 1)) + '</cols><sheetData>')
    for r in sorted(rows):
        ht = (heights or {}).get(r)
        out.append('<row r="%d"%s>' % (r, ' ht="%s" customHeight="1"' % ht if ht else ''))
        for c in sorted(rows[r]):
            v, s = rows[r][c]; ref = '%s%d' % (col(c), r)
            if v is None or v == '':
                out.append('<c r="%s" s="%d"/>' % (ref, sid[s]))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append('<c r="%s" s="%d"><v>%s</v></c>' % (ref, sid[s], repr(v) if isinstance(v, float) else v))
            else:
                out.append('<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, sid[s], escape(str(v))))
        out.append('</row>')
    out.append('</sheetData><pageMargins left="0.5" right="0.5" top="0.5" bottom="0.5" header="0.3" footer="0.3"/></worksheet>')
    n = 1
    while 'xl/worksheets/sheet%d.xml' % n in files: n += 1
    path = 'xl/worksheets/sheet%d.xml' % n
    files[path] = '\n'.join(out).encode('utf8')
    rels = files['xl/_rels/workbook.xml.rels'].decode('utf8')
    rid = 'rId%d' % (max(int(x) for x in re.findall(r'Id="rId(\d+)"', rels)) + 1)
    files['xl/_rels/workbook.xml.rels'] = rels.replace('</Relationships>', '<Relationship Id="%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/></Relationships>' % (rid, n)).encode('utf8')
    wb = files['xl/workbook.xml'].decode('utf8')
    sids = [int(x) for x in re.findall(r'<sheet [^>]*sheetId="(\d+)"', wb)]
    files['xl/workbook.xml'] = wb.replace('</sheets>', '<sheet name="%s" sheetId="%d" r:id="%s"/></sheets>' % (escape(sheet_name), max(sids) + 1, rid)).encode('utf8')
    ct = files['[Content_Types].xml'].decode('utf8')
    files['[Content_Types].xml'] = ct.replace('</Types>', '<Override PartName="/%s" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>' % path).encode('utf8')
    zout = zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED)
    for name in order: zout.writestr(name, files[name])
    zout.writestr(path, files[path])
    zout.close()
