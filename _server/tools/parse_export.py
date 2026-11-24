# -*- coding: utf-8 -*-
"""解析问卷网导出的 xlsx（原始数据）为 JSON，便于核对结构与取值。
用法: python parse_export.py <xlsx路径> [--json 输出路径] [--head N]
"""
import sys, re, json, zipfile, io

def colnum(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n

def parse_xlsx(path):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    shared = []
    if 'xl/sharedStrings.xml' in names:
        x = z.read('xl/sharedStrings.xml').decode('utf-8')
        for si in re.findall(r'<si>(.*?)</si>', x, re.S):
            shared.append(''.join(re.findall(r'<t[^>]*>(.*?)</t>', si, re.S)))
    sheet_name = 'xl/worksheets/sheet1.xml'
    sh = z.read(sheet_name).decode('utf-8')
    rows = []
    for r in re.findall(r'<row[^>]*>(.*?)</row>', sh, re.S):
        cells = {}
        for c in re.findall(r'<c[^>]*>.*?</c>|<c[^>]*/>', r, re.S):
            m = re.search(r'r="([A-Z]+)(\d+)"', c)
            if not m:
                continue
            t = re.search(r't="(\w+)"', c)
            v = re.search(r'<v>(.*?)</v>', c, re.S)
            if v is None:
                val = ''
            elif t and t.group(1) == 's':
                val = shared[int(v.group(1))]
            else:
                val = v.group(1)
            cells[colnum(m.group(1))] = val
        rows.append(cells)
    return rows

def main():
    path = sys.argv[1]
    rows = parse_xlsx(path)
    hdr = rows[0]
    maxc = max(hdr.keys())
    header = [hdr.get(i, '') for i in range(1, maxc + 1)]
    print('列数:', len(header))
    for i, h in enumerate(header, 1):
        print('  col%-3d %s' % (i, h[:60]))
    print()
    print('数据行数:', len(rows) - 1)
    for r in rows[1:]:
        vals = [r.get(i, '') for i in range(1, maxc + 1)]
        print('--- SEQ %s | 得分 %s ---' % (vals[0], vals[1]))
        for i, v in enumerate(vals):
            if v != '':
                print('   %-3d %-40s = %s' % (i + 1, header[i][:38], v[:70]))
    if '--json' in sys.argv:
        out = sys.argv[sys.argv.index('--json') + 1]
        data = []
        for r in rows[1:]:
            vals = [r.get(i, '') for i in range(1, maxc + 1)]
            data.append(dict(zip(header, vals)))
        io.open(out, 'w', encoding='utf-8').write(json.dumps(data, ensure_ascii=False, indent=1))
        print('\n已写出:', out)

if __name__ == '__main__':
    main()
