"""
bidir_report.py — build the EXFO bidirectional cable-test results workbook.

Reusable builder used by fms_pull.py (live API) or standalone (manual data).
Given the per-fibre unidirectional results from each end, it writes a workbook with:
  Cover · Summary · Bidir E2E Loss Report (butterfly + bidir average) · Raw A-B · Raw B-A · Config
and computes the bidirectional average loss (mean of the two one-way system losses) plus
a PASS / FAIL / CHECK verdict per fibre.

A "row" is a dict:
  {fid, date, loss, length, star, w1310, w1550, w1625}
Wavelength fields may be None (populated once the §2b measurement-detail call is wired in);
loss and length come from baselineMeasurement in searchOpticalRouteByRtu.
"""
import re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule
from openpyxl.comments import Comment

FNAME = 'Arial'


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return x if x not in ('', None) else None



def fibre_ids(fid, settings=None):
    """(binder, ribbon, fibre_no) for a fibre id or number.

    Ribbons are 12 fibres; binders group ribbons. The binder size is a setting because it
    is a cable-build convention rather than anything the API tells us."""
    st = settings or {}
    per_r = int(st.get('per_ribbon', 12) or 12)
    per_b = int(st.get('per_binder', 6) or 6)
    n = None
    if isinstance(fid, int):
        n = fid
    else:
        m = re.search(r'F(\d{1,4})\b', str(fid or ''))
        if m:
            n = int(m.group(1))
    if not n:
        return (None, None, None)
    rib = (n - 1) // per_r + 1
    return ((rib - 1) // per_b + 1, rib, n)


def fibre_label(fid, settings=None, sep='  '):
    """'B1  R03  F025' - binder, then ribbon, then fibre."""
    b, r, n = fibre_ids(fid, settings)
    if n is None:
        return str(fid or '')
    return f"B{b}{sep}R{r:02d}{sep}F{n:03d}"


def _remedial_status(splices, meta, settings, threshold=0.15, wl=1550):
    """Tag each splice NEW / EXISTING / IMPROVED / WORSE against previous runs, and return
    the splices that were flagged before but are not this time (cleared).

    History is kept in remedial_history.json beside the tool, keyed by cable, so remedials
    can be followed across runs to see whether work is improving them or new ones appear."""
    import os, json, datetime
    nominal = settings.get('nominal', 0.02)
    CHANGE_TH = 0.03

    def cur_avg(s):
        a, b = s.get(f'a{wl}'), s.get(f'b{wl}')
        if a is None and b is None:
            return None
        return ((a if a is not None else nominal) + (b if b is not None else nominal)) / 2

    cable = str(meta.get('cable') or 'cable')
    date = str(meta.get('generated') or datetime.date.today().isoformat())[:19]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, 'remedial_history.json')
    try:
        hist = json.load(open(path)) if os.path.isfile(path) else {}
    except Exception:
        hist = {}
    ch = hist.get(cable, {})
    seen = set()

    for s in splices:
        v = cur_avg(s)
        fid = str(s.get('fid'))
        pos = s.get('position')
        key = f"{fid}@{round(pos)}" if pos is not None else fid
        rec = ch.get(key)
        if rec is None and pos is not None:            # match nearest within 25 m, same fibre
            best = None
            for k, rv in ch.items():
                if not k.startswith(fid + '@'):
                    continue
                try:
                    kp = float(k.split('@')[1])
                except Exception:
                    continue
                if abs(kp - pos) <= 25 and (best is None or abs(kp - pos) < best[0]):
                    best = (abs(kp - pos), k, rv)
            if best:
                key, rec = best[1], best[2]
        prev = rec.get('avg') if rec else None
        s['prev'] = prev
        s['change'] = (round(v - prev, 3) if (v is not None and prev is not None) else None)
        if prev is None:
            s['status'] = 'NEW'
        elif v is None:
            s['status'] = 'EXISTING'
        elif v < prev - CHANGE_TH:
            s['status'] = 'IMPROVED'
        elif v > prev + CHANGE_TH:
            s['status'] = 'WORSE'
        else:
            s['status'] = 'EXISTING'
        runs = (rec.get('runs') if rec else None) or []
        if v is not None:
            runs.append([date, round(v, 4)])
        ch[key] = {'avg': (round(v, 4) if v is not None else prev), 'date': date,
                   'fid': fid, 'pos': pos, 'runs': runs[-20:]}
        seen.add(key)

    cleared = []
    for k in list(ch.keys()):
        if k in seen:
            continue
        rv = ch[k]
        last = rv.get('avg')
        if last is not None and last >= threshold:
            cleared.append({'fid': rv.get('fid'), 'position': rv.get('pos'),
                            'prev': last, 'date': rv.get('date')})
            del ch[k]          # report a cleared splice once, then drop it

    hist[cable] = ch
    try:
        json.dump(hist, open(path, 'w'), indent=0)
    except Exception:
        pass
    return cleared


def _add_remedial_tracking(wb, splices, cleared, meta, settings, threshold=0.15):
    """A 'Remedial Tracking' sheet: run-over-run movement of the remedial splices -
    what is new, improved, worse, still outstanding, and what has cleared."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    ws = wb.create_sheet('Remedial Tracking')
    ws.sheet_view.showGridLines = False
    navy = PatternFill('solid', fgColor='1F4E79')
    border = Border(*[Side(style='thin', color='BFBFBF')] * 4)
    ctr = Alignment(horizontal='center', vertical='center')
    left = Alignment(horizontal='left', vertical='center')

    ws.merge_cells('A1:H1')
    ws['A1'] = f"Remedial Tracking - {meta.get('cable','')}"
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = ctr
    ws['A2'] = f"Run: {meta.get('generated','')}   |   compares this run's remedials (>= {threshold} dB) with previous runs"
    ws['A2'].font = Font(name=FN, size=9, italic=True, color='595959')

    from collections import Counter
    tally = Counter(s.get('status', 'NEW') for s in splices)
    order = ['NEW', 'WORSE', 'EXISTING', 'IMPROVED']
    cfill = {'NEW': 'BDD7EE', 'WORSE': 'FFC7CE', 'EXISTING': 'F2F2F2', 'IMPROVED': 'C6EFCE', 'CLEARED': 'E2EFDA'}
    r = 4
    ws.cell(r, 1, 'Summary').font = Font(name=FN, size=11, bold=True, color='1F4E79'); r += 1
    for st in order + (['CLEARED'] if cleared else []):
        n = len(cleared) if st == 'CLEARED' else tally.get(st, 0)
        lc = ws.cell(r, 1, st); lc.fill = PatternFill('solid', fgColor=cfill[st])
        lc.font = Font(name=FN, size=10, bold=True); lc.alignment = left; lc.border = border
        vc = ws.cell(r, 2, n); vc.font = Font(name=FN, size=10); vc.alignment = ctr; vc.border = border
        r += 1

    r += 1
    hdr = ['Fiber ID', 'Position (m)', 'Status', 'Prev @1550', 'Now @1550', 'Change', 'Last seen']
    for c, h in enumerate(hdr, 1):
        cc = ws.cell(r, c, h); cc.font = Font(name=FN, size=9, bold=True, color='FFFFFF')
        cc.fill = navy; cc.alignment = ctr; cc.border = border
    r += 1

    def now_avg(s):
        nominal = settings.get('nominal', 0.02)
        a, b = s.get('a1550'), s.get('b1550')
        if a is None and b is None:
            return None
        return round(((a if a is not None else nominal) + (b if b is not None else nominal)) / 2, 3)

    # changed first (NEW / WORSE / IMPROVED), then EXISTING, then CLEARED
    rank = {'NEW': 0, 'WORSE': 1, 'IMPROVED': 2, 'EXISTING': 3}
    for s in sorted(splices, key=lambda x: (rank.get(x.get('status'), 9), str(x.get('fid')))):
        st = s.get('status', 'NEW')
        vals = [s.get('fid'), round(s['position']) if s.get('position') is not None else None,
                st, s.get('prev'), now_avg(s), s.get('change'), meta.get('generated', '')[:10]]
        for c, v in enumerate(vals, 1):
            cc = ws.cell(r, c, v); cc.font = Font(name=FN, size=9); cc.border = border
            cc.alignment = left if c == 1 else ctr
            if c in (4, 5, 6):
                cc.number_format = '+0.000;-0.000' if c == 6 else '0.000'
        ws.cell(r, 3).fill = PatternFill('solid', fgColor=cfill.get(st, 'FFFFFF'))
        r += 1
    for cl in cleared:
        vals = [cl.get('fid'), round(cl['position']) if cl.get('position') is not None else None,
                'CLEARED', cl.get('prev'), None, None, str(cl.get('date', ''))[:10]]
        for c, v in enumerate(vals, 1):
            cc = ws.cell(r, c, v); cc.font = Font(name=FN, size=9); cc.border = border
            cc.alignment = left if c == 1 else ctr
            if c == 4:
                cc.number_format = '0.000'
        ws.cell(r, 3).fill = PatternFill('solid', fgColor=cfill['CLEARED'])
        r += 1

    for c, w in enumerate([24, 12, 11, 11, 11, 9, 12], 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = 'A{}'.format(4)


def build_workbook(out_path, meta, settings, rowsA, rowsB,
                   devices=None, sections=None, resultsets=None,
                   loss_label='Loss @1550', splices=None, splice_threshold=0.15, splice_wl=1550,
                   odf=None, include_e2e=True, cable_view=None, view_wls=(1550,),
                   all_events=None, remedials=None, bs_remed=None, verify=None,
                   latest=None, otdr_measured=None, otdr_diag=None,
                   client_verify=None, client_info=None):
    """
    meta: dict(customer, cable, partner, siteA, siteB, ribbons, generated, device, resultset)
    settings: dict(budget_sys, budget_1550, max_km, star_floor)
    rowsA / rowsB: lists of per-fibre dicts for End A (A->B) and End B (B->A).
    """
    BLUE = Font(name=FNAME, size=10, color='0000FF')
    BLACK = Font(name=FNAME, size=10, color='000000')
    HDRW = Font(name=FNAME, size=10, bold=True, color='FFFFFF')
    TITLE = Font(name=FNAME, size=18, bold=True, color='FFFFFF')
    BOLD = Font(name=FNAME, size=10, bold=True)
    LBL = Font(name=FNAME, size=10, bold=True)

    fill_A = PatternFill('solid', fgColor='1F4E79')
    fill_B = PatternFill('solid', fgColor='7C3A00')
    fill_mid = PatternFill('solid', fgColor='548235')
    fill_hdrA = PatternFill('solid', fgColor='2E75B6')
    fill_hdrB = PatternFill('solid', fgColor='C55A11')
    fill_hdrMid = PatternFill('solid', fgColor='70AD47')
    fill_input = PatternFill('solid', fgColor='FFF2CC')
    fill_yellow = PatternFill('solid', fgColor='FFFF00')
    fill_pass = PatternFill('solid', fgColor='C6EFCE')
    fill_fail = PatternFill('solid', fgColor='FFC7CE')
    fill_check = PatternFill('solid', fgColor='FFEB9C')
    fill_cover = PatternFill('solid', fgColor='1F4E79')

    thin = Side(style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ctr = Alignment(horizontal='center', vertical='center')
    ctrw = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center')

    N = max(len(rowsA), len(rowsB))
    devices = devices or ['FTBx-730C', 'FTBx-735C', 'FTB-720C', 'MAX-730C', 'MAX-720C']
    sections = sections or [meta.get('cable', 'Section')]
    resultsets = resultsets or [meta.get('resultset', 'Bidir')]

    wb = openpyxl.Workbook()
    try:
        wb.calculation.fullCalcOnLoad = True   # Excel recalculates every formula on open
    except AttributeError:
        from openpyxl.workbook.properties import CalcProperties
        wb.calculation = CalcProperties(fullCalcOnLoad=True)

    # ---------- COVER ----------
    cov = wb.active; cov.title = 'Cover'
    cov.sheet_view.showGridLines = False
    for c in range(1, 10):
        cov.column_dimensions[get_column_letter(c)].width = 16
    cov.column_dimensions['C'].width = 46; cov.column_dimensions['D'].width = 4
    cov.column_dimensions['E'].width = 46; cov.column_dimensions['F'].width = 6
    cov.row_dimensions[1].height = 44
    _place_logo(cov, 'B1', height_px=54)
    cov.merge_cells('C3:H4'); cov['C3'] = 'Cable Bidirectional Testing Report'
    cov['C3'].font = TITLE; cov['C3'].fill = fill_cover; cov['C3'].alignment = ctr
    for r in range(3, 5):
        for c in range(3, 9):
            cov.cell(r, c).fill = fill_cover

    meta_rows = [('Customer Name:', meta.get('customer', '')),
                 ('Cable / Section:', meta.get('cable', '')),
                 ('Build Partner:', meta.get('partner', '')),
                 ('Site A (End A):', meta.get('siteA', '')),
                 ('Site B (End B):', meta.get('siteB', '')),
                 ('Ribbons:', meta.get('ribbons', '')),
                 ('Generated on:', meta.get('generated', '')),
                 ('Generated by (FMS user):', meta.get('fms_user', ''))]
    r0 = 7
    for i, (lab, val) in enumerate(meta_rows):
        cov.cell(r0 + i, 3, lab).font = LBL; cov.cell(r0 + i, 3).alignment = left
        vc = cov.cell(r0 + i, 5, val); vc.font = BLUE; vc.fill = fill_input; vc.alignment = left; vc.border = border

    s0 = r0 + len(meta_rows) + 2
    cov.cell(s0, 3, 'ANALYSIS SETTINGS').font = Font(name=FNAME, size=11, bold=True, color='1F4E79')
    setrows = [('Loss budget - System (dB):', settings.get('budget_sys', 7.5)),
               ('Loss budget @1550 (dB):', settings.get('budget_1550', 9.0)),
               ('Max loss per km (dB/km):', settings.get('max_km', 0.25)),
               ('Star rating floor:', settings.get('star_floor', 3.0)),
               ('Splice nominal (dB) - used where a direction found no splice (edit me):', settings.get('nominal', 0.02)),
               ('Good splice limit (dB):', settings.get('good_splice', 0.15)),
               ('Medium splice limit (dB):', settings.get('medium_splice', 0.1875)),
               ('Hydra length (km):', settings.get('hydra_length', 10)),
               ('Wavelengths shown:', ', '.join(str(w) for w in (view_wls or (1550,))))]
    for i, (lab, val) in enumerate(setrows):
        rr = s0 + 1 + i
        cov.cell(rr, 3, lab).font = LBL; cov.cell(rr, 3).alignment = left
        vc = cov.cell(rr, 5, val); vc.font = Font(name=FNAME, size=10, bold=True, color='0000FF')
        vc.fill = fill_yellow; vc.alignment = ctr; vc.border = border
        vc.number_format = '0.000' if 'nominal' in lab else '0.00'
    BUDGET_SYS = f"Cover!$E${s0+1}"; MAXKM = f"Cover!$E${s0+3}"; NOMINAL = f"Cover!$E${s0+5}"
    settings = dict(settings); settings['_nominal_ref'] = NOMINAL
    settings['_nominal_cell'] = f"Cover E{s0+5}"

    d0 = s0 + len(setrows) + 2
    cov.cell(d0, 3, 'DATA SOURCE (SELECTABLE)').font = Font(name=FNAME, size=11, bold=True, color='1F4E79')
    selrows = [('Test device / OTDR:', meta.get('device', devices[0])),
               ('Cable / Section:', meta.get('cable', sections[0])),
               ('Result set:', meta.get('resultset', resultsets[0]))]
    # name the exact distance schedule revision, so a report can always be traced back
    # to the joint distances it was built on
    if meta.get('schedule'):
        selrows.append(('Distance schedule:', meta.get('schedule')))
    for i, (lab, val) in enumerate(selrows):
        rr = d0 + 1 + i
        cov.cell(rr, 3, lab).font = LBL; cov.cell(rr, 3).alignment = left
        vc = cov.cell(rr, 5, val); vc.font = BLUE; vc.fill = fill_input; vc.alignment = left; vc.border = border
    DEV_CELL = f"$E${d0+1}"; SEC_CELL = f"$E${d0+2}"; RES_CELL = f"$E${d0+3}"

    _v = settings.get('_vintage')
    if _v:
        vr = d0 + len(selrows) + 2
        cov.cell(vr, 3, 'DATA VINTAGE - READ THIS BEFORE ACTING ON FAILS').font = Font(
            name=FNAME, size=11, bold=True, color='9C0006')
        for i, t in enumerate([
            f"Splice detail in this workbook comes from the analysed test of {_v['used'].replace('T',' ')}.",
            f"{_v['n']} of {_v['total']} fibres have a newer {_v['type']} test dated {_v['newest'].replace('T',' ')}.",
            "A raw OTDR trace carries no event table through the API, so remedial work done after",
            "the first date CANNOT appear in the splice tabs. A fail shown here may already be fixed.",
            "See the 'Latest Test Check' tab for whether each fibre improved, and re-analyse those",
            "traces as iOLM in FMS to bring the post-remedial splice detail back through the API."]):
            cc = cov.cell(vr + 1 + i, 3, t)
            cc.font = Font(name=FNAME, size=9, italic=True, color='9C0006')
        d0 = vr + 7

    lg = d0 + len(selrows) + 2
    cov.cell(lg, 3, 'Legend:').font = BOLD
    leg = [('Yellow = key assumption you can change', 'FFFF00'),
           ('Light yellow / blue text = value pulled from FMS', 'FFF2CC'),
           ('Black text = calculated automatically (do not edit)', 'FFFFFF')]
    for i, (txt, col) in enumerate(leg):
        cc = cov.cell(lg + 1 + i, 3, txt); cc.font = Font(name=FNAME, size=9, italic=True)
        sw = cov.cell(lg + 1 + i, 2); sw.fill = PatternFill('solid', fgColor=col); sw.border = border

    # ---------- CONFIG ----------
    cfg = wb.create_sheet('Config')
    cfg['A1'] = 'Devices'; cfg['B1'] = 'Sections'; cfg['C1'] = 'Result sets'
    for x in cfg['A1':'C1'][0]:
        x.font = BOLD
    for i, d in enumerate(devices): cfg.cell(2 + i, 1, d)
    for i, s in enumerate(sections): cfg.cell(2 + i, 2, s)
    for i, s in enumerate(resultsets): cfg.cell(2 + i, 3, s)
    cfg.sheet_state = 'hidden'
    dv_dev = DataValidation(type='list', formula1=f"=Config!$A$2:$A${1+len(devices)}", allow_blank=True)
    dv_sec = DataValidation(type='list', formula1=f"=Config!$B$2:$B${1+len(sections)}", allow_blank=True)
    dv_res = DataValidation(type='list', formula1=f"=Config!$C$2:$C${1+len(resultsets)}", allow_blank=True)
    cov.add_data_validation(dv_dev); cov.add_data_validation(dv_sec); cov.add_data_validation(dv_res)
    dv_dev.add(cov[DEV_CELL]); dv_sec.add(cov[SEC_CELL]); dv_res.add(cov[RES_CELL])

    # ---------- RAW SHEETS ----------
    raw_cols = ['Fiber ID', 'Test Date', f'{loss_label} (dB)', 'Length (m)', 'Star Rating',
                'Loss@1310 (dB)', 'Loss@1550 (dB)', 'Loss@1625 (dB)']

    def build_raw(title, band_fill, hdr_fill, data):
        ws = wb.create_sheet(title)
        ws.sheet_view.showGridLines = False
        ws.merge_cells('A1:H1')
        ws['A1'] = f'{title}  -  unidirectional results (pulled from FMS)'
        ws['A1'].font = HDRW; ws['A1'].fill = band_fill; ws['A1'].alignment = ctr
        for c, h in enumerate(raw_cols, 1):
            cc = ws.cell(2, c, h); cc.font = HDRW; cc.fill = hdr_fill; cc.alignment = ctrw; cc.border = border
        for i, d in enumerate(data):
            rr = 3 + i
            vals = [d.get('fid'), d.get('date'), _num(d.get('loss')), _num(d.get('length')),
                    _num(d.get('star')), _num(d.get('w1310')), _num(d.get('w1550')), _num(d.get('w1625'))]
            for c, v in enumerate(vals, 1):
                cc = ws.cell(rr, c, v); cc.font = BLUE; cc.fill = fill_input; cc.border = border
                cc.alignment = left if c <= 2 else ctr
                if c in (3, 6, 7, 8): cc.number_format = '0.000'
                if c in (4, 5): cc.number_format = '0.0'
        for c, w in enumerate([26, 26, 15, 12, 10, 13, 13, 13], 1):
            ws.column_dimensions[get_column_letter(c)].width = w
        ws.freeze_panes = 'A3'

    build_raw('Raw A-B (End A)', fill_A, fill_hdrA, rowsA)
    build_raw('Raw B-A (End B)', fill_B, fill_hdrB, rowsB)
    RA = "'Raw A-B (End A)'"; RB = "'Raw B-A (End B)'"

    # ---------- BIDIR E2E + SUMMARY (optional) ----------
    if include_e2e:
        e = wb.create_sheet('Bidir E2E Loss Report')
        e.sheet_view.showGridLines = False
        hdr_left = ['Fiber ID', 'Test Date', loss_label, 'Length (m)', 'Star', 'Loss@1310', 'Loss@1550', 'Loss@1625']
        hdr_mid = ['Bidir Avg Loss', 'Avg@1310', 'Avg@1550', 'Avg@1625', 'Verdict']
        hdr_right = ['Loss@1625', 'Loss@1550', 'Loss@1310', 'Star', 'Length (m)', loss_label, 'Test Date', 'Fiber ID']
        allh = hdr_left + hdr_mid + hdr_right
        e.merge_cells('A1:H2'); e['A1'] = f"{meta.get('siteA','End A')}  (End A  ->  End B)"
        e['A1'].font = Font(name=FNAME, size=14, bold=True, color='FFFFFF'); e['A1'].fill = fill_A; e['A1'].alignment = ctr
        e.merge_cells('I1:M2'); e['I1'] = 'BIDIRECTIONAL AVERAGE'
        e['I1'].font = Font(name=FNAME, size=14, bold=True, color='FFFFFF'); e['I1'].fill = fill_mid; e['I1'].alignment = ctr
        e.merge_cells('N1:U2'); e['N1'] = f"{meta.get('siteB','End B')}  (End B  ->  End A)"
        e['N1'].font = Font(name=FNAME, size=14, bold=True, color='FFFFFF'); e['N1'].fill = fill_B; e['N1'].alignment = ctr
        for c, h in enumerate(allh, 1):
            cc = e.cell(3, c, h); cc.font = Font(name=FNAME, size=9, bold=True, color='FFFFFF')
            cc.alignment = ctrw; cc.border = border
            cc.fill = fill_hdrA if c <= 8 else (fill_hdrMid if c <= 13 else fill_hdrB)

        for i in range(N):
            rr = 4 + i; raw = 3 + i
            for col, src in zip(range(1, 9), 'ABCDEFGH'):
                e.cell(rr, col, f"={RA}!{src}{raw}")
            m = f"MATCH($A{rr},{RB}!$A$3:$A${2+N},0)"
            for col, src in [(14, 'H'), (15, 'G'), (16, 'F'), (17, 'E'), (18, 'D'), (19, 'C'), (20, 'B'), (21, 'A')]:
                e.cell(rr, col, f"=IFERROR(INDEX({RB}!${src}$3:${src}${2+N},{m}),\"\")")

            def bidir(a, b):
                return (f'=IF(AND({a}{rr}>0,{b}{rr}>0),AVERAGE({a}{rr},{b}{rr}),'
                        f'IF({a}{rr}>0,{a}{rr},IF({b}{rr}>0,{b}{rr},"")))')
            e.cell(rr, 9, bidir('C', 'S')); e.cell(rr, 10, bidir('F', 'P'))
            e.cell(rr, 11, bidir('G', 'O')); e.cell(rr, 12, bidir('H', 'N'))
            e.cell(rr, 13, (f'=IF(OR(C{rr}=0,S{rr}=0,D{rr}=0,N(I{rr})=0),"CHECK",'
                            f'IF(AND(I{rr}<={BUDGET_SYS},(I{rr}/(D{rr}/1000))<={MAXKM}),"PASS","FAIL"))'))
            for c in range(1, 22):
                cc = e.cell(rr, c); cc.border = border; cc.font = BLACK
                cc.alignment = left if c in (1, 2, 20, 21) else ctr
                if c in (3, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 19): cc.number_format = '0.000'
                if c in (4, 5, 17, 18): cc.number_format = '0.0'
            e.cell(rr, 9).font = Font(name=FNAME, size=10, bold=True, color='000000')
            e.cell(rr, 13).font = BOLD
            e.cell(rr, 13).fill = PatternFill('solid', fgColor='F2F2F2')

        for c, w in enumerate([26, 20, 11, 10, 6, 10, 10, 10, 12, 10, 10, 10, 9, 10, 10, 10, 6, 10, 11, 20, 26], 1):
            e.column_dimensions[get_column_letter(c)].width = w
        e.freeze_panes = 'A4'
        last = 3 + N
        if N > 0:
            e.conditional_formatting.add(f'M4:M{last}', CellIsRule(operator='equal', formula=['"PASS"'], fill=fill_pass, font=Font(name=FNAME, size=10, bold=True, color='006100')))
            e.conditional_formatting.add(f'M4:M{last}', CellIsRule(operator='equal', formula=['"FAIL"'], fill=fill_fail, font=Font(name=FNAME, size=10, bold=True, color='9C0006')))
            e.conditional_formatting.add(f'M4:M{last}', CellIsRule(operator='equal', formula=['"CHECK"'], fill=fill_check, font=Font(name=FNAME, size=10, bold=True, color='9C6500')))
            e.conditional_formatting.add(f'I4:I{last}', CellIsRule(operator='greaterThan', formula=[BUDGET_SYS], fill=fill_fail))
        if last < 4:
            last = 4  # keep Summary ranges valid when there is no data

        # ---------- SUMMARY ----------
        sm = wb.create_sheet('Summary')
        sm.sheet_view.showGridLines = False
        sm.column_dimensions['B'].width = 34
        for c in ['C', 'D', 'E']:
            sm.column_dimensions[c].width = 14
        sm.merge_cells('B2:E2'); sm['B2'] = 'Bidirectional Analysis - Summary'
        sm['B2'].font = Font(name=FNAME, size=14, bold=True, color='FFFFFF'); sm['B2'].fill = fill_cover; sm['B2'].alignment = ctr
        E = "'Bidir E2E Loss Report'"
        rng = lambda col: f"{E}!${col}$4:${col}${last}"
        stats = [('Fibers analysed', f"=COUNT({rng('I')})"),
                 ('Fibers PASS', f'=COUNTIF({rng("M")},"PASS")'),
                 ('Fibers FAIL', f'=COUNTIF({rng("M")},"FAIL")'),
                 ('Fibers CHECK (incomplete)', f'=COUNTIF({rng("M")},"CHECK")'),
                 ('Pass rate', '=IF(C4=0,0,C5/C4)'),
                 ('Avg bidir loss (dB)', f"=IFERROR(AVERAGE({rng('I')}),0)"),
                 ('Min bidir loss (dB)', f"=IFERROR(MIN({rng('I')}),0)"),
                 ('Max bidir loss (dB)', f"=IFERROR(MAX({rng('I')}),0)"),
                 ('Avg bidir @1550 (dB)', f"=IFERROR(AVERAGE({rng('K')}),0)"),
                 ('Avg fibre length (m)', f"=IFERROR(AVERAGE({rng('D')}),0)")]
        r = 4
        for lab, f in stats:
            sm.cell(r, 2, lab).font = LBL; sm.cell(r, 2).alignment = left; sm.cell(r, 2).border = border
            cc = sm.cell(r, 3, f); cc.font = BLACK; cc.alignment = ctr; cc.border = border
            if 'rate' in lab: cc.number_format = '0.0%'
            elif 'length' in lab: cc.number_format = '0.0'
            elif 'dB' in lab: cc.number_format = '0.000'
            else: cc.number_format = '0'
            r += 1
        sm.cell(r + 1, 2, 'Bidir loss = mean of the two one-way system losses (End A->B and End B->A).').font = Font(name=FNAME, size=9, italic=True)
        sm.cell(r + 2, 2, 'Verdict = PASS when bidir system loss <= budget AND loss/km <= max (see Cover).').font = Font(name=FNAME, size=9, italic=True)

    # ---------- SPLICES > threshold  (client layout) ----------
    if splices is not None:
        sp = wb.create_sheet(f'Splices > {splice_threshold}dB')
        sp.sheet_view.showGridLines = False
        # Tag each remedial NEW / EXISTING / IMPROVED / WORSE vs previous runs, and get the
        # list of splices that were flagged before but are no longer (cleared).
        cleared = _remedial_status(splices, meta, settings,
                                   threshold=splice_threshold, wl=splice_wl)
        cols = ['Fiber ID', 'Event', 'Position (m)', 'Loss @1310 (dB)', 'Loss @1550 (dB)',
                'Loss @1625 (dB)', 'Fiber ID', 'DOR Ref', 'DOR Comment',
                'Status', 'Prev @1550', 'Change']
        for c, h in enumerate(cols, 1):
            cc = sp.cell(1, c, h); cc.font = HDRW; cc.fill = fill_hdrMid
            cc.alignment = ctrw; cc.border = border
        statfill = {'NEW': 'BDD7EE', 'WORSE': 'FFC7CE', 'IMPROVED': 'C6EFCE', 'EXISTING': 'F2F2F2'}
        statfont = {'NEW': '1F4E79', 'WORSE': '9C0006', 'IMPROVED': '375623', 'EXISTING': '595959'}
        for i, s in enumerate(splices):
            r = 2 + i
            sp.cell(r, 1, s.get('fid'))
            sp.cell(r, 2, 'Splice' if s.get('type') in ('Splice', 'Group') else s.get('type'))
            pc = sp.cell(r, 3, round(s['position']) if s.get('position') is not None else None)
            pc.number_format = '0'
            # hidden one-way helpers N..S feed the averaged Loss columns (nominal-editable)
            for k, key in enumerate(['a1310', 'a1550', 'a1625', 'b1310', 'b1550', 'b1625']):
                hc = sp.cell(r, 14 + k, s.get(key))
                hc.number_format = '0.000'
            sp.cell(r, 4, f'=(IF(N{r}="",{NOMINAL},N{r})+IF(Q{r}="",{NOMINAL},Q{r}))/2')
            sp.cell(r, 5, f'=(IF(O{r}="",{NOMINAL},O{r})+IF(R{r}="",{NOMINAL},R{r}))/2')
            sp.cell(r, 6, f'=(IF(P{r}="",{NOMINAL},P{r})+IF(S{r}="",{NOMINAL},S{r}))/2')
            sp.cell(r, 7, s.get('fid'))
            for c in range(1, 13):
                cc = sp.cell(r, c); cc.border = border; cc.font = BLACK
                cc.alignment = left if c in (1, 7, 9) else ctr
                if c in (4, 5, 6):
                    cc.number_format = '0.000'
            # tracking columns
            st = s.get('status', 'NEW')
            stc = sp.cell(r, 10, st)
            stc.fill = PatternFill('solid', fgColor=statfill.get(st, 'FFFFFF'))
            stc.font = Font(name=FNAME, size=10, bold=True, color=statfont.get(st, '000000'))
            stc.alignment = ctr
            if s.get('prev') is not None:
                pcx = sp.cell(r, 11, round(s['prev'], 3)); pcx.number_format = '0.000'
            if s.get('change') is not None:
                chg = sp.cell(r, 12, round(s['change'], 3)); chg.number_format = '+0.000;-0.000'
        for c, w in enumerate([24, 8, 13, 13, 13, 13, 24, 8, 40, 11, 11, 9], 1):
            sp.column_dimensions[get_column_letter(c)].width = w
        for col in ('N', 'O', 'P', 'Q', 'R', 'S'):
            sp.column_dimensions[col].hidden = True
        sp.freeze_panes = 'A2'
        _add_remedial_tracking(wb, splices, cleared, meta, settings, splice_threshold)

    # ---------- ODF CONNECTION  (client butterfly) ----------
    if odf is not None:
        od = wb.create_sheet('ODF Connection')
        od.sheet_view.showGridLines = False
        od.merge_cells('A1:I2'); od['A1'] = meta.get('siteA', 'End A')
        od['A1'].font = Font(name=FNAME, size=14, bold=True, color='FFFFFF'); od['A1'].fill = fill_A; od['A1'].alignment = ctr
        od.merge_cells('N1:V2'); od['N1'] = meta.get('siteB', 'End B')
        od['N1'].font = Font(name=FNAME, size=14, bold=True, color='FFFFFF'); od['N1'].fill = fill_B; od['N1'].alignment = ctr
        hdr = ['Fiber ID', 'SW Loss @1550', 'Direction', 'Position', 'Event', 'Reflection',
               'Loss@1310 (dB)', 'Loss@1550 (dB)', 'Loss@1625 (dB)', '', '', '', '',
               'Loss@1625 (dB)', 'Loss@1550 (dB)', 'Loss@1310 (dB)', 'Reflection', 'Event',
               'Position', 'Direction', 'SW Loss @1550', 'Fiber ID']
        for c, h in enumerate(hdr, 1):
            if not h:
                continue
            cc = od.cell(3, c, h); cc.font = Font(name=FNAME, size=9, bold=True, color='FFFFFF')
            cc.fill = fill_hdrA if c <= 9 else fill_hdrB; cc.alignment = ctrw; cc.border = border

        for i, o in enumerate(odf):
            r = 4 + i
            vals = {1: o.get('fid'), 2: o.get('a_sw'), 3: 'AB', 4: o.get('a_pos'),
                    5: o.get('a_event'), 6: o.get('a_refl'),
                    7: o.get('a_w1310'), 8: o.get('a_w1550'), 9: o.get('a_w1625'),
                    14: o.get('b_w1625'), 15: o.get('b_w1550'), 16: o.get('b_w1310'),
                    17: o.get('b_refl'), 18: o.get('b_event'), 19: o.get('b_pos'),
                    20: 'BA', 21: o.get('b_sw'), 22: o.get('fid')}
            for c, v in vals.items():
                cc = od.cell(r, c, v); cc.font = BLACK; cc.border = border
                cc.alignment = left if c in (1, 22) else ctr
                if c in (2, 7, 8, 9, 14, 15, 16, 21):
                    cc.number_format = '0.000'
                if c in (4, 19):
                    cc.number_format = '0.0'
                if c in (6, 17):
                    cc.number_format = '0.0'
        widths = [25, 11, 8, 13, 11, 9, 10, 8, 10, 3, 3, 3, 3, 10, 8, 13, 11, 9, 10, 8, 11, 25]
        for c, w in enumerate(widths, 1):
            od.column_dimensions[get_column_letter(c)].width = w
        od.freeze_panes = 'A4'

    if all_events:
        _add_all_events(wb, all_events, meta, settings, wls=view_wls)

    if verify:
        _add_verify(wb, verify, meta, settings)

    if remedials:
        _add_remedials(wb, remedials, meta, settings)

    if bs_remed:
        _add_backsplice_remedials(wb, bs_remed, meta, settings)

    if otdr_measured:
        _add_otdr_measured(wb, otdr_measured, otdr_diag, meta, settings)

    if client_verify:
        _add_client_verify(wb, client_verify, meta, settings, client_info)

    if latest:
        _add_latest_check(wb, latest, meta, settings)

    if cable_view:
        _add_cable_views(wb, cable_view, meta, settings, wls=view_wls, odf=odf)

    order = ['Cover', 'Client Verify', 'Verify', 'Summary', 'Latest Test Check', 'OTDR Measured', 'Remedials', 'Remedial Tracking', 'Backsplice Remedials', 'All Events', 'Bidir E2E Loss Report', 'Cable_View', 'Detailed Cable View',
             f'Splices > {splice_threshold}dB',
             'ODF Connection', 'Raw A-B (End A)', 'Raw B-A (End B)', 'Config']
    order = [o for o in order if o in [s.title for s in wb._sheets]]
    wb._sheets.sort(key=lambda s: order.index(s.title))
    wb.active = 0
    _polish(wb, settings)
    wb.save(out_path)
    return out_path


def _add_cable_views(wb, cv, meta, settings, wls=(1550,), odf=None):
    """Client-format 'Cable_View' and 'Detailed Cable View', built for readability:
    only the chosen wavelengths, colour-banded headers, and a clear break between each
    joint/location block."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    B = Font(name=FN, size=9, bold=True)
    N9 = Font(name=FN, size=9)
    HW = Font(name=FN, size=9, bold=True, color='FFFFFF')
    HD = Font(name=FN, size=10, bold=True, color='FFFFFF')

    navy = PatternFill('solid', fgColor='1F4E79')
    teal = PatternFill('solid', fgColor='2E75B6')
    slate = PatternFill('solid', fgColor='44546A')
    band1 = PatternFill('solid', fgColor='DCE6F1')      # alternating joint tints
    band2 = PatternFill('solid', fgColor='EDF2F9')
    fibfill = PatternFill('solid', fgColor='F2F2F2')
    RED = PatternFill('solid', fgColor='DC3545')        # >= medium splice
    AMBER = PatternFill('solid', fgColor='FFC107')      # >= good splice
    RAW = Font(name=FN, size=9, color='062F96')         # one-way readings, blue
    AVGF = Font(name=FN, size=9, bold=True, color='000000')
    SUMF = Font(name=FN, size=10, bold=True, color='1F4E79')
    blk = Side(style='medium', color='000000')

    def paint(cell, v):
        # solid fill carries the verdict; text stays black so it prints and reads plainly
        if v is None:
            return
        if v >= medium:
            cell.fill = RED
        elif v >= good:
            cell.fill = AMBER

    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')
    hair = Side(style='thin', color='D9D9D9')
    edge = Side(style='medium', color='1F4E79')
    cell_b = Border(left=hair, right=hair, top=hair, bottom=hair)

    joints = cv['joints']
    matrix = cv['matrix']
    # The two ODF columns show the ODF connection itself, the same reading as the ODF
    # Connection sheet: A end = the A-B reading only, B end = the B-A reading only.
    # Without this the A-end column picked up the launch group at ~-7 m, which lumps the
    # RTU switch in with the connector (e.g. 0.681 where the ODF is really 0.275).
    if odf and joints:
        L = cv.get('length') or 0
        odf_by = {o.get('fid'): o for o in odf}
        matrix = {f: dict(m) for f, m in matrix.items()}
        ja, jb = joints[0], joints[-1]
        use_a = abs(ja) <= 150
        use_b = len(joints) > 1 and (not L or abs(L - jb) <= 150)
        for f in dict.fromkeys(list(cv['fibres']) + list(matrix)):
            o = odf_by.get(f)
            if not o:
                continue
            m = matrix.setdefault(f, {})
            if use_a:
                rec = {f'a{w}': o.get(f'a_w{w}') for w in (1310, 1550, 1625)}
                rec.update({f'b{w}': None for w in (1310, 1550, 1625)})
                m[ja] = rec if any(v is not None for v in rec.values()) else None
            if use_b:
                rec = {f'b{w}': o.get(f'b_w{w}') for w in (1310, 1550, 1625)}
                rec.update({f'a{w}': None for w in (1310, 1550, 1625)})
                m[jb] = rec if any(v is not None for v in rec.values()) else None
    names = cv.get('names') or {}      # {joint distance: scheduled physical location}
    fibres = list(dict.fromkeys(cv['fibres']))
    length = cv.get('length')
    ths = settings.get('nominal', 0.02)
    good = settings.get('good_splice', 0.15)
    medium = settings.get('medium_splice', 0.1875)
    WLS = tuple(wls) or (1550,)
    PERJ = len(WLS) * 2          # two sub-columns (A-B / B-A) per wavelength

    def avg(rec, w):
        a, b = rec.get(f'a{w}'), rec.get(f'b{w}')
        if a is None and b is None:
            return None
        return ((a if a is not None else ths) + (b if b is not None else ths)) / 2

    def jlabel(j, idx):
        if idx == 0 or idx == len(joints) - 1:
            return f'ODF-{j}'
        return j

    # ===================== Cable_View =====================
    ws = wb.create_sheet('Cable_View')
    ws.sheet_view.showGridLines = False
    # The tab is already named 'Cable_View', so there is no in-sheet title row: the data
    # starts at A1. Settings (Hydra length, splice limits, wavelengths) live on the Cover.
    LROW, HROW, WROW, SCROW, MMROW, AVROW, DROW, FIRST = 1, 2, 3, 4, 5, 6, 7, 9
    lc = ws.cell(LROW, 5, 'Location'); lc.font = HD; lc.fill = slate
    ws.cell(HROW, 5, f'Joints: {len(joints)}').font = HD
    ws.cell(HROW, 5).fill = slate
    ws.cell(WROW, 5, 'Wavelength').font = B
    for rr, lab in ((SCROW, 'Splice count'), (MMROW, 'Min / Max (one-way)'),
                    (AVROW, 'Avg loss (bidir)')):
        lc_ = ws.cell(rr, 5, lab); lc_.font = SUMF; lc_.alignment = lft
    ws.cell(DROW, 5, 'Direction').font = B
    ws.cell(FIRST - 1, 5, 'Fibre').font = HD
    ws.cell(FIRST - 1, 5).fill = slate

    for ji, j in enumerate(joints):
        c0 = 6 + ji * PERJ
        fill = band1 if ji % 2 == 0 else band2
        ws.merge_cells(start_row=LROW, start_column=c0, end_row=LROW, end_column=c0 + PERJ - 1)
        nc = ws.cell(LROW, c0, names.get(j) or '(unscheduled)')
        nc.font = Font(name=FN, size=8, bold=bool(names.get(j)),
                       italic=not names.get(j), color='FFFFFF')
        nc.fill = navy if ji % 2 == 0 else teal
        nc.alignment = Alignment(horizontal='center', vertical='bottom', wrap_text=True)
        ws.merge_cells(start_row=HROW, start_column=c0, end_row=HROW, end_column=c0 + PERJ - 1)
        hc = ws.cell(HROW, c0, jlabel(j, ji))
        hc.font = HD; hc.fill = navy if ji % 2 == 0 else teal; hc.alignment = ctr
        for wi, w in enumerate(WLS):
            cw = c0 + wi * 2
            ws.merge_cells(start_row=WROW, start_column=cw, end_row=WROW, end_column=cw + 1)
            wc = ws.cell(WROW, cw, f'{w} nm'); wc.font = HW; wc.fill = teal; wc.alignment = ctr
            for k, lab in enumerate(('A-B', 'B-A')):
                dc = ws.cell(DROW, cw + k, lab)
                dc.font = B; dc.fill = fill; dc.alignment = ctr; dc.border = cell_b
            # per-joint summary for this wavelength, over every fibre in the view
            recs = [matrix.get(f, {}).get(j) for f in fibres]
            recs = [x for x in recs if x]
            ones = [x.get(f'{d}{w}') for x in recs for d in ('a', 'b')]
            ones = [x for x in ones if x is not None]
            avs = [avg(x, w) for x in recs]
            avs = [x for x in avs if x is not None]
            ws.merge_cells(start_row=SCROW, start_column=cw, end_row=SCROW, end_column=cw + 1)
            sc_ = ws.cell(SCROW, cw, len(avs)); sc_.font = SUMF; sc_.alignment = ctr
            if ones:
                for k, v in enumerate((min(ones), max(ones))):
                    mc = ws.cell(MMROW, cw + k, round(v, 3))
                    mc.font = RAW; mc.number_format = '0.000'; mc.alignment = ctr
            ws.merge_cells(start_row=AVROW, start_column=cw, end_row=AVROW, end_column=cw + 1)
            if avs:
                v = sum(avs) / len(avs)
                ac_ = ws.cell(AVROW, cw, round(v, 4))
                ac_.font = AVGF; ac_.number_format = '0.000'; ac_.alignment = ctr
                paint(ac_, v)
            for rr in (SCROW, MMROW, AVROW):
                for k in range(2):
                    ws.cell(rr, cw + k).border = cell_b

    r = FIRST
    last_rib = object()
    rib_groups = []          # [header row, first data row, last data row]
    blocks = []              # first row of each two-row fibre block
    for fi, fid in enumerate(fibres):
        rib = fibre_ids(fid, settings)[1]
        if rib != last_rib:
            if rib_groups:
                rib_groups[-1][2] = r - 1
            rc = ws.cell(r, 5, f'Ribbon {rib}' if rib else 'Ribbon -')
            rc.font = HD; rc.fill = slate; rc.alignment = lft
            for c in range(6, 6 + len(joints) * PERJ):
                ws.cell(r, c).fill = slate
            rib_groups.append([r, r + 1, None])
            last_rib = rib
            r += 1
        ws.merge_cells(start_row=r, start_column=5, end_row=r + 1, end_column=5)
        fc = ws.cell(r, 5, fid)
        fc.font = Font(name=FN, size=10, bold=True, color='1F4E79')
        fc.fill = fibfill; fc.alignment = Alignment(horizontal='center', vertical='center')
        blocks.append(r)
        for ji, j in enumerate(joints):
            c0 = 6 + ji * PERJ
            rec = matrix.get(fid, {}).get(j)
            for wi, w in enumerate(WLS):
                cw = c0 + wi * 2
                for k in range(2):
                    for rr in (r, r + 1):
                        cc = ws.cell(rr, cw + k); cc.border = cell_b
                        if ji % 2 == 1:
                            cc.fill = band2
                if not rec:
                    continue
                a, b = rec.get(f'a{w}'), rec.get(f'b{w}')
                if a is not None:
                    ca = ws.cell(r, cw, round(a, 3)); ca.font = RAW; ca.number_format = '0.000'
                    ca.alignment = ctr
                if b is not None:
                    cb = ws.cell(r, cw + 1, round(b, 3)); cb.font = RAW; cb.number_format = '0.000'
                    cb.alignment = ctr
                v = avg(rec, w)
                if v is not None:
                    ws.merge_cells(start_row=r + 1, start_column=cw, end_row=r + 1, end_column=cw + 1)
                    cv_ = ws.cell(r + 1, cw, round(v, 4))
                    cv_.number_format = '0.000'; cv_.alignment = ctr; cv_.font = AVGF
                    paint(cv_, v)
        r += 2
    if rib_groups:
        rib_groups[-1][2] = r - 1

    # heavy line under each fibre block so the one-way row and its average read as a pair
    lastc = 5 + len(joints) * PERJ
    for r0 in blocks:
        for c in range(5, lastc + 1):
            cc = ws.cell(r0 + 1, c); ex = cc.border
            cc.border = Border(left=ex.left, right=ex.right, top=ex.top, bottom=blk)
        cc = ws.cell(r0, 5); ex = cc.border
        cc.border = Border(left=blk, right=blk, top=ex.top, bottom=ex.bottom)
        cc = ws.cell(r0 + 1, 5); ex = cc.border
        cc.border = Border(left=blk, right=blk, top=ex.top, bottom=blk)

    # heavy edge at each joint boundary so blocks read as blocks
    lastr = max(r - 1, FIRST)
    for ji in range(len(joints)):
        c0 = 6 + ji * PERJ
        for rr in range(LROW, lastr + 1):
            cc = ws.cell(rr, c0)
            ex = cc.border
            cc.border = Border(left=edge, right=ex.right, top=ex.top, bottom=ex.bottom)

    # collapsible ribbon groups, the way the EXFO report does it: the [-] control sits
    # on the ribbon header row, not below the block
    ws.sheet_properties.outlinePr.summaryBelow = False
    for hdr, r0, r1 in rib_groups:
        if r1 is None or r1 < r0:
            continue
        for rr in range(r0, r1 + 1):
            ws.row_dimensions[rr].outlineLevel = 1
            ws.row_dimensions[rr].hidden = False
    ws.row_dimensions[LROW].height = 46
    ws.column_dimensions['A'].width = 20
    ws.column_dimensions['E'].width = 30
    for rr in range(FIRST, r):
        ws.row_dimensions[rr].height = 15
    for c in range(6, 6 + len(joints) * PERJ):
        ws.column_dimensions[get_column_letter(c)].width = 9
    ws.freeze_panes = ws.cell(FIRST, 6)

    # ================= Detailed Cable View =================
    inner = [j for idx, j in enumerate(joints) if 0 < idx < len(joints) - 1]
    dv = wb.create_sheet('Detailed Cable View')
    dv.sheet_view.showGridLines = False
    dv.merge_cells('A1:F1')
    dv['A1'] = f"Detailed Cable View - {meta.get('cable','')}   (avg of both directions, {', '.join(str(w) for w in WLS)} nm)"
    dv['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    dv['A1'].fill = navy; dv['A1'].alignment = lft

    LRD, JH, JW, FIRSTD = 2, 3, 4, 5
    heads = ['Fibre', 'Bad SPs', 'SP Count', 'Avg/Fibre']
    for c, h in enumerate(heads, 1):
        cc = dv.cell(JW, c, h); cc.font = HD; cc.fill = slate; cc.alignment = ctr; cc.border = cell_b
    dv.merge_cells(start_row=JH, start_column=1, end_row=JH, end_column=4)
    sc = dv.cell(JH, 1, 'Per fibre'); sc.font = HD; sc.fill = navy; sc.alignment = ctr
    lcd = dv.cell(LRD, 1, 'Location'); lcd.font = HD; lcd.fill = slate; lcd.alignment = lft

    for ji, j in enumerate(inner):
        c0 = 5 + ji * len(WLS)
        if len(WLS) > 1:
            dv.merge_cells(start_row=LRD, start_column=c0, end_row=LRD,
                           end_column=c0 + len(WLS) - 1)
        nc = dv.cell(LRD, c0, names.get(j) or '(unscheduled)')
        nc.font = Font(name=FN, size=8, bold=bool(names.get(j)),
                       italic=not names.get(j), color='FFFFFF')
        nc.fill = navy if ji % 2 == 0 else teal
        nc.alignment = Alignment(horizontal='center', vertical='bottom', wrap_text=True)
        dv.merge_cells(start_row=JH, start_column=c0, end_row=JH, end_column=c0 + len(WLS) - 1)
        hc = dv.cell(JH, c0, j); hc.font = HD; hc.alignment = ctr
        hc.fill = navy if ji % 2 == 0 else teal
        for wi, w in enumerate(WLS):
            wc = dv.cell(JW, c0 + wi, f'{w} nm'); wc.font = HW; wc.alignment = ctr
            wc.fill = teal if ji % 2 == 0 else slate; wc.border = cell_b

    r = FIRSTD
    last_rib = object()
    dv_groups = []
    for fi, fid in enumerate(fibres):
        rib = fibre_ids(fid, settings)[1]
        if rib != last_rib:
            if dv_groups:
                dv_groups[-1][2] = r - 1
            rc = dv.cell(r, 1, f'Ribbon {rib}' if rib else 'Ribbon -')
            rc.font = HD; rc.fill = slate
            for c in range(2, 5 + len(inner) * len(WLS)):
                dv.cell(r, c).fill = slate
            grp = [f for f in fibres if fibre_ids(f, settings)[1] == rib]
            vals = [avg(matrix[f][j], WLS[0]) for f in grp for j in inner
                    if matrix.get(f, {}).get(j) and avg(matrix[f][j], WLS[0]) is not None]
            if vals:
                gc = dv.cell(r, 4, round(sum(vals) / len(vals), 3))
                gc.font = Font(name=FN, size=9, bold=True, color='FFFFFF')
                gc.number_format = '0.000'
            dv_groups.append([r, r + 1, None])
            last_rib = rib
            r += 1
        cells = matrix.get(fid, {})
        fc = dv.cell(r, 1, fid); fc.font = N9; fc.alignment = lft; fc.border = cell_b
        allv = [avg(cells[j], WLS[0]) for j in inner if j in cells and avg(cells[j], WLS[0]) is not None]
        bad = sum(1 for v in allv if v >= good)
        bc = dv.cell(r, 2, bad if bad else None); bc.font = N9; bc.alignment = ctr; bc.border = cell_b
        cc = dv.cell(r, 3, len(allv)); cc.font = N9; cc.alignment = ctr; cc.border = cell_b
        if allv:
            ac = dv.cell(r, 4, round(sum(allv) / len(allv), 3))
            ac.font = N9; ac.number_format = '0.000'; ac.alignment = ctr; ac.border = cell_b
        for ji, j in enumerate(inner):
            c0 = 5 + ji * len(WLS)
            rec = cells.get(j)
            for wi, w in enumerate(WLS):
                cell = dv.cell(r, c0 + wi); cell.border = cell_b; cell.alignment = ctr
                if ji % 2 == 1:
                    cell.fill = band2
                if not rec:
                    continue
                v = avg(rec, w)
                if v is None:
                    continue
                if abs(v) < ths:
                    cell.value = f'<{ths}'
                    cell.font = Font(name=FN, size=8, color='808080')
                else:
                    cell.value = round(v, 3)
                    cell.number_format = '0.000'
                    if v >= good:
                        cell.font = AVGF
                        paint(cell, v)
                    else:
                        cell.font = N9
        r += 1
    if dv_groups:
        dv_groups[-1][2] = r - 1
    for ji in range(len(inner)):
        c0 = 5 + ji * len(WLS)
        for rr in range(LRD, r):
            cc = dv.cell(rr, c0)
            ex = cc.border
            cc.border = Border(left=edge, right=ex.right, top=ex.top, bottom=ex.bottom)
    dv.sheet_properties.outlinePr.summaryBelow = False
    for hdr, r0, r1 in dv_groups:
        if r1 is None or r1 < r0:
            continue
        for rr in range(r0, r1 + 1):
            dv.row_dimensions[rr].outlineLevel = 1
            dv.row_dimensions[rr].hidden = False
    dv.row_dimensions[LRD].height = 62
    dv.column_dimensions['A'].width = 30
    for c in range(2, 5):
        dv.column_dimensions[get_column_letter(c)].width = 10
    for c in range(5, 5 + len(inner) * len(WLS)):
        dv.column_dimensions[get_column_letter(c)].width = 12
    dv.freeze_panes = dv.cell(FIRSTD, 5)


def _polish(wb, settings):
    """Readability pass over every data sheet: freeze headers, autofilter, banded/zebra
    rows off, colour-scale the loss columns, tidy widths, and colour-code splice losses
    against the Good/Medium thresholds so problems jump out."""
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
    from openpyxl.utils import get_column_letter
    good = settings.get('good_splice', 0.15)
    med = settings.get('medium_splice', 0.1875)
    green = PatternFill('solid', fgColor='C6EFCE')
    amber = PatternFill('solid', fgColor='FFEB9C')
    red = PatternFill('solid', fgColor='FFC7CE')
    fgreen = Font(name=FNAME, size=10, color='006100')
    famber = Font(name=FNAME, size=10, color='9C6500')
    fred = Font(name=FNAME, size=10, bold=True, color='9C0006')

    def band(ws, rng):
        ws.conditional_formatting.add(rng, CellIsRule(operator='greaterThanOrEqual',
                                      formula=[str(med)], fill=red, font=fred))
        ws.conditional_formatting.add(rng, CellIsRule(operator='greaterThanOrEqual',
                                      formula=[str(good)], fill=amber, font=famber))
        ws.conditional_formatting.add(rng, CellIsRule(operator='lessThan',
                                      formula=[str(good)], fill=green, font=fgreen))

    # Splices tab: colour the three averaged loss columns
    for nm in wb.sheetnames:
        if nm.startswith('Splices >'):
            ws = wb[nm]
            last = ws.max_row
            if last >= 2:
                band(ws, f'D2:F{last}')
            ws.auto_filter.ref = f'A1:L{max(last,1)}'
            ws.freeze_panes = 'A2'

    # (Cable_View / Detailed Cable View are coloured cell-by-cell at build time,
    #  so only the AVERAGED values are highlighted - not the raw one-way readings.)

    # E2E: colour scale on the bidir average column
    if 'Bidir E2E Loss Report' in wb.sheetnames:
        ws = wb['Bidir E2E Loss Report']
        if ws.max_row >= 4:
            ws.conditional_formatting.add(f'I4:I{ws.max_row}',
                ColorScaleRule(start_type='min', start_color='C6EFCE',
                               mid_type='percentile', mid_value=50, mid_color='FFEB9C',
                               end_type='max', end_color='FFC7CE'))

    # Raw tabs: filter + freeze
    for nm in ('Raw A-B (End A)', 'Raw B-A (End B)'):
        if nm in wb.sheetnames:
            ws = wb[nm]
            ws.auto_filter.ref = f'A2:H{max(ws.max_row,2)}'
            ws.freeze_panes = 'A3'

    # ODF: filter + freeze
    if 'ODF Connection' in wb.sheetnames:
        ws = wb['ODF Connection']
        ws.freeze_panes = 'A4'

    # every sheet: sensible print setup so a printed/PDF copy is readable
    for ws in wb.worksheets:
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        try:
            from openpyxl.worksheet.properties import PageSetupProperties
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        except Exception:
            pass
        ws.print_title_rows = '1:1'

def _add_all_events(wb, events, meta, settings, wls=(1550,)):
    """Flat 'All Events' sheet: every fibre x every event, with location and loss.
    Filterable and sortable - the single place to review results."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    HD = Font(name=FN, size=10, bold=True, color='FFFFFF')
    N9 = Font(name=FN, size=10)
    navy = PatternFill('solid', fgColor='1F4E79')
    grey = PatternFill('solid', fgColor='F2F2F2')
    hair = Side(style='thin', color='D9D9D9')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')

    good = settings.get('good_splice', 0.15)
    med = settings.get('medium_splice', 0.1875)
    ths = settings.get('nominal', 0.02)
    single = bool(events and events[0].get('single'))
    WLS = tuple(wls) or (1550,)

    ws = wb.create_sheet('All Events')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:F1')
    ws['A1'] = f"All Events - {meta.get('cable','')}   ({len(events)} events)"
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    ws['A2'] = ("Single-ended test: loss is the one-way reading." if single else
                "Tested from both ends: Loss = average of the two directions; the one-way "
                "readings are alongside.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)

    cols = ['Binder', 'Ribbon', 'Fibre', 'Description', 'Test date', 'Event #',
            'Position (m)', 'Position (km)', 'Type']
    for w in WLS:
        cols.append(f'Loss @{w} (dB)')
    if not single:
        for w in WLS:
            cols += [f'A-B @{w}', f'B-A @{w}']
        cols.append('Dirs')
    HROW = 4
    for c, h in enumerate(cols, 1):
        cc = ws.cell(HROW, c, h); cc.font = HD; cc.fill = navy; cc.alignment = ctr; cc.border = bd

    def eff(e, w):
        a, b = e.get(f'a{w}'), e.get(f'b{w}')
        if e.get('single'):
            return a
        if a is None and b is None:
            return None
        return ((a if a is not None else ths) + (b if b is not None else ths)) / 2

    r = HROW + 1
    last_fid, idx = None, 0
    for e in events:
        if e['fid'] != last_fid:
            last_fid, idx = e['fid'], 0
        idx += 1
        pos = e.get('position')
        _b, _rib, _n = fibre_ids(e.get('fid'), settings)
        vals = [(f"B{_b}" if _b else None), (f"R{_rib:02d}" if _rib else None),
                e.get('fid'), e.get('desc'), (str(e.get('date') or '')[:19]).replace('T', ' '),
                idx, (round(pos) if pos is not None else None),
                (round(pos / 1000.0, 4) if pos is not None else None), e.get('type')]
        for c, v in enumerate(vals, 1):
            cc = ws.cell(r, c, v); cc.font = N9; cc.border = bd
            cc.alignment = lft if c in (3, 4, 5, 9) else ctr
            if c == 8:
                cc.number_format = '0.0000'
        c = len(vals)
        for w in WLS:
            c += 1
            v = eff(e, w)
            cc = ws.cell(r, c, None if v is None else round(v, 3))
            cc.border = bd; cc.alignment = ctr; cc.number_format = '0.000'
            if v is None:
                cc.font = N9
            elif v >= med:
                cc.fill = PatternFill('solid', fgColor='FFC7CE')
                cc.font = Font(name=FN, size=10, bold=True, color='9C0006')
            elif v >= good:
                cc.fill = PatternFill('solid', fgColor='FFEB9C')
                cc.font = Font(name=FN, size=10, bold=True, color='9C6500')
            else:
                cc.font = Font(name=FN, size=10, color='006100')
        if not single:
            for w in WLS:
                for key in (f'a{w}', f'b{w}'):
                    c += 1
                    v = e.get(key)
                    cc = ws.cell(r, c, None if v is None else round(v, 3))
                    cc.font = N9; cc.border = bd; cc.alignment = ctr
                    cc.number_format = '0.000'
            c += 1
            cc = ws.cell(r, c, e.get('dirs')); cc.font = N9; cc.border = bd; cc.alignment = ctr
        if idx % 2 == 0:
            for cc_ in range(1, len(cols) + 1):
                if ws.cell(r, cc_).fill.patternType is None:
                    ws.cell(r, cc_).fill = grey
        r += 1

    widths = [7, 7, 30, 16, 19, 8, 12, 12, 13] + [12] * len(WLS)
    if not single:
        widths += [10] * (2 * len(WLS)) + [7]
    for c, wd in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = wd
    ws.freeze_panes = ws.cell(HROW + 1, 6)
    ws.auto_filter.ref = f"A{HROW}:{get_column_letter(len(cols))}{max(r-1, HROW)}"

def _lossfont(v, good, med, FN='Arial', size=10):
    from openpyxl.styles import Font, PatternFill
    if v is None:
        return Font(name=FN, size=size), None
    if v >= med:
        return Font(name=FN, size=size, bold=True, color='9C0006'), PatternFill('solid', fgColor='FFC7CE')
    if v >= good:
        return Font(name=FN, size=size, bold=True, color='9C6500'), PatternFill('solid', fgColor='FFEB9C')
    return Font(name=FN, size=size, color='006100'), None


def _add_remedials(wb, rows, meta, settings):
    """Splice-loss work list, grouped by location in route order (A end first).

    Each location's rows sit together, sorted by ribbon then worst loss, with a live
    summary row underneath (splice count, ribbon count, worst loss). Each group is an
    outline level so a location can be collapsed to its summary line."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    navy = PatternFill('solid', fgColor='1F4E79')
    HD = Font(name=FN, size=10, bold=True, color='FFFFFF')
    N10 = Font(name=FN, size=10)
    hair = Side(style='thin', color='D9D9D9')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')
    # summary row style
    S_FONT = Font(name=FN, size=10, bold=True, color='1F4E79')
    S_FILL = PatternFill('solid', fgColor='DDEBF7')
    s_line = Side(style='thin', color='1F4E79')
    S_BD = Border(top=s_line, bottom=s_line)
    S_LFT = Alignment(horizontal='left', vertical='center', wrap_text=False)
    S_CTR = Alignment(horizontal='center', vertical='center', wrap_text=False)
    NO_LOC = '(No location)'
    good = settings.get('good_splice', 0.15)
    med = settings.get('medium_splice', 0.1875)

    ws = wb.create_sheet('Remedials')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:H1')
    # count splice rows only, never the summary rows
    ws['A1'] = f"Remedials - {meta.get('cable','')}   ({len(rows)} splices at or over {good} dB)"
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    ws['A2'] = ("Grouped by location in route order (A end first), then by ribbon, worst loss "
                "first within each ribbon. Use the 1/2 outline buttons to collapse/expand. "
                "Tick off in the Action / Status columns as remediation is done.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)

    cols = ['Binder', 'Ribbon', 'Fibre', 'Description', 'Location', 'Leg', 'Measured (m)',
            'Schedule (m)', 'Delta (m)', f"Loss (dB)", 'A-B', 'B-A', 'Action', 'Status']
    NC = len(cols)
    HROW = 4
    for c, h in enumerate(cols, 1):
        cc = ws.cell(HROW, c, h); cc.font = HD; cc.fill = navy; cc.alignment = ctr; cc.border = bd
    wl = rows[0].get('wl', 1550) if rows else 1550

    # ---- group by location, order groups by route position ----
    groups = {}
    for x in rows:
        loc = str(x.get('location') or '').strip()
        groups.setdefault(loc, []).append(x)

    def _route_pos(items):
        pts = []
        for x in items:
            p = x.get('sched_m')
            if p is None:
                p = x.get('position')
            if p is not None:
                pts.append(p)
        return min(pts) if pts else float('inf')

    named = sorted((k for k in groups if k), key=lambda k: (_route_pos(groups[k]), k))
    order = named + ([''] if '' in groups else [])

    def _row_key(x):
        _b, _rib, _n = fibre_ids(x.get('fid'), settings)
        loss = x.get('loss')
        return (_rib if _rib is not None else 10 ** 6,
                -(loss if loss is not None else float('-inf')),
                _n if _n is not None else 10 ** 6)

    r = HROW + 1
    for loc in order:
        items = sorted(groups[loc], key=_row_key)
        first = r
        for x in items:
            _b, _rib, _n = fibre_ids(x.get('fid'), settings)
            vals = [(f"B{_b}" if _b else None), (f"R{_rib:02d}" if _rib else None),
                    x.get('fid'), x.get('desc'), x.get('location'), x.get('leg'),
                    (round(x['position']) if x.get('position') is not None else None),
                    (round(x['sched_m']) if x.get('sched_m') is not None else None),
                    x.get('delta_m'), x.get('loss'),
                    x.get(f"a{wl}"), x.get(f"b{wl}"), None, None]
            for c, v in enumerate(vals, 1):
                cc = ws.cell(r, c, v); cc.border = bd
                cc.alignment = lft if c in (3, 4, 5, 13, 14) else ctr
                cc.font = N10
                if c in (10, 11, 12):
                    cc.number_format = '0.000'
                if c == 10:
                    f, fl = _lossfont(v, good, med)
                    cc.font = f
                    if fl:
                        cc.fill = fl
            ws.row_dimensions[r].outline_level = 1
            r += 1
        last = r - 1

        # ---- summary row, directly below the group, live formulas ----
        C = f"C{first}:C{last}"
        B = f"B{first}:B{last}"
        nrib = f"SUMPRODUCT(1/COUNTIF({B},{B}))"
        for c in range(1, NC + 1):
            cc = ws.cell(r, c)
            cc.font = S_FONT; cc.fill = S_FILL; cc.border = S_BD
            cc.alignment = S_CTR if c in (9, 10) else S_LFT
        ws.cell(r, 1, loc or NO_LOC)
        ws.cell(r, 5, f'=COUNTA({C})&" splice"&IF(COUNTA({C})=1,"","s")&" · "'
                      f'&{nrib}&" ribbon"&IF({nrib}=1,"","s")')
        ws.cell(r, 9, 'Worst')
        jc = ws.cell(r, 10, f"=MAX(J{first}:J{last})")
        jc.number_format = '0.000'
        ws.row_dimensions[r].height = 15
        r += 1

    for c, wd in enumerate([7, 7, 30, 15, 22, 10, 12, 12, 10, 11, 9, 9, 30, 12], 1):
        ws.column_dimensions[get_column_letter(c)].width = wd
    ws.freeze_panes = ws.cell(HROW + 1, 1)
    ws.auto_filter.ref = f"A{HROW}:{get_column_letter(NC)}{max(r-1, HROW)}"
    # summary rows sit below their group (Excel default); save expanded at level 2
    ws.sheet_properties.outlinePr.summaryBelow = True
    ws.sheet_format.outlineLevelRow = 1 if rows else 0


def _add_backsplice_remedials(wb, bs, meta, settings):
    """Grouped by location: A-B and B-A side by side with their average, under one merged
    location header carrying the distances. Reading a joint means reading three adjacent
    cells rather than scanning across three separate blocks of the sheet."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.comments import Comment
    FN = 'Arial'
    navy = PatternFill('solid', fgColor='1F4E79')
    teal = PatternFill('solid', fgColor='2E75B6')
    rust = PatternFill('solid', fgColor='C55A11')
    green = PatternFill('solid', fgColor='548235')
    band = [PatternFill('solid', fgColor='1F4E79'), PatternFill('solid', fgColor='2F5597')]
    HD = Font(name=FN, size=9, bold=True, color='FFFFFF')
    N9 = Font(name=FN, size=9)
    hair = Side(style='thin', color='D9D9D9')
    edge = Side(style='medium', color='808080')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')
    good = settings.get('good_splice', 0.15)
    med = settings.get('medium_splice', 0.1875)

    order, rows = bs['order'], bs['rows']
    nom, wl = bs.get('nominal', 0.06), bs.get('wl', 1550)
    NOMREF = settings.get('_nominal_ref')
    DIST = (bs.get('dist') or {}).get('sched') or {}
    FOUND = (bs.get('dist') or {}).get('found') or {}
    n = len(order)
    if n == 0 or not rows:
        ws = wb.create_sheet('Backsplice Remedials')
        ws.sheet_view.showGridLines = False
        ws['A1'] = 'Backsplice Remedials - no locations available'
        ws['A1'].font = Font(name='Arial', size=13, bold=True, color='9C0006')
        ws['A3'] = 'No events could be tied to a location, so the loop could not be folded.'
        ws['A4'] = ('Check Distances.xlsx has a sheet for this cable, or that the pull '
                    'actually returned splice events.')
        for a in ('A3', 'A4'):
            ws[a].font = Font(name='Arial', size=10)
        ws.column_dimensions['A'].width = 110
        return

    ws = wb.create_sheet('Backsplice Remedials')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:J1')
    ws['A1'] = f"Backsplice Remedials - {meta.get('cable','')}   ({wl} nm)"
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    ws['A2'] = (f"One row per PHYSICAL fibre. Every location shows A-B, B-A and their average, "
                f"and no cell is ever left blank under a populated average. Black = measured "
                f"event. Blue italic = no event flagged, so the step was measured off the raw "
                f"trace. Grey italic on a shaded cell = the Cover nominal ({nom} dB) standing in. "
                f"All three are live formulas, so changing the nominal updates the sheet. "
                f"Amber headers are joints the distance schedule does not list. Only the "
                f"average is colour-coded, red at or over the pass threshold; a one-way "
                f"reading on its own is not a pass or a fail.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)
    _v = settings.get('_vintage')
    if _v:
        ws['A3'] = (f"DATA VINTAGE: splice detail is from {_v['used'].replace('T',' ')}; "
                    f"{_v['n']} of {_v['total']} fibres have a newer {_v['type']} test dated "
                    f"{_v['newest'].replace('T',' ')} with no event table.")
        ws['A3'].font = Font(name=FN, size=9, bold=True, color='9C0006')

    LOCROW, HROW, FIRST = 5, 6, 7
    for c, lab in ((1, 'Binder'), (2, 'Ribbon'), (3, 'Fibre')):
        hc = ws.cell(LOCROW, c, lab); hc.font = HD; hc.fill = navy
        hc.alignment = ctr; hc.border = bd
        ws.cell(HROW, c, 'physical' if c == 3 else '').font = Font(name=FN, size=8, italic=True)
    for i, loc in enumerate(order):
        c0 = 4 + i * 3
        d = DIST.get(loc) or {}
        om, rm = d.get('out'), d.get('ret')
        dtxt = " / ".join(f"{round(x):,}" for x in (om, rm) if x is not None)
        ws.merge_cells(start_row=LOCROW, start_column=c0, end_row=LOCROW, end_column=c0 + 2)
        unsched = '(unscheduled)' in str(loc)
        hc = ws.cell(LOCROW, c0, f"{loc}\n{dtxt} m" if dtxt and not unsched else loc)
        hc.font = HD
        hc.fill = PatternFill('solid', fgColor='7F6000') if unsched else band[i % 2]
        hc.alignment = ctr; hc.border = bd
        if unsched:
            hc.comment = Comment(
                "This joint is not in the distance schedule. It is a real event grouped "
                "with the same distance on other fibres, shown so nothing is dropped.\n"
                "Add it to Distances.xlsx to give it a name.",
                'FMS Test Results Report', height=90, width=280)
        f = FOUND.get(loc) or {}
        pts = (f.get('out') or []) + (f.get('ret') or [])
        if pts:
            note = f"Events actually found between {round(min(pts)):,} and {round(max(pts)):,} m"
            if om is not None:
                note += f"\nSchedule: {round(om):,} m outbound"
            if rm is not None:
                note += f", {round(rm):,} m return"
            hc.comment = Comment(note, 'FMS Test Results Report', height=90, width=280)
        for j, (lab, fill) in enumerate((('A-B', teal), ('B-A', rust), ('Avg', green))):
            cc = ws.cell(HROW, c0 + j, lab)
            cc.font = HD; cc.fill = fill; cc.alignment = ctr; cc.border = bd

    r = FIRST
    last_rib = None
    ncols = 3 + 3 * len(order)
    for rec in rows:
        b_id, rib, fno = fibre_ids(rec.get('phys') or rec.get('fid'), settings)
        # a medium rule wherever the ribbon changes, so ribbons read as blocks
        newrib = (last_rib is not None and rib != last_rib)
        last_rib = rib
        for c, v in ((1, f"B{b_id}" if b_id else None),
                     (2, f"R{rib:02d}" if rib else None),
                     (3, f"F{fno:03d}" if fno else rec.get('fid'))):
            cc = ws.cell(r, c, v)
            cc.font = Font(name=FN, size=9, bold=(c == 3))
            cc.alignment = ctr if c < 3 else lft
            cc.border = bd
        asm = rec.get('assumed', {})
        cur = rec.get('curve', {})
        for i, loc in enumerate(order):
            c0 = 4 + i * 3
            for j, key in enumerate(('out', 'ret')):
                v = rec.get(key, {}).get(loc)
                if v is None and NOMREF:
                    # never leave the cell empty under a populated average: show the
                    # nominal that the average is actually built from, as a live
                    # reference so editing the Cover updates it here too
                    cc = ws.cell(r, c0 + j, f'={NOMREF}')
                else:
                    cc = ws.cell(r, c0 + j, None if v is None else round(v, 3))
                cc.border = bd; cc.alignment = ctr; cc.number_format = '0.000'
                # one-way readings are not a pass/fail judgement: a gainer in one
                # direction is normal, so only the average gets colour
                cc.font = Font(name=FN, size=9)
                if key in (cur.get(loc) or []):
                    cc.font = Font(name=FN, size=9, italic=True, color='1F4E79')
                    cc.comment = Comment(
                        "No event was flagged in this direction, so this is the step "
                        "measured off the raw trace at the scheduled distance.",
                        'FMS Test Results Report', height=80, width=260)
                elif v is None:
                    cc.font = Font(name=FN, size=9, italic=True, color='808080')
                    cc.fill = PatternFill('solid', fgColor='F2F2F2')
                    cc.comment = Comment(
                        "No event found in this direction and no usable trace reading, "
                        "so the Cover nominal stands in. Shown rather than left blank so "
                        "the average always has two visible numbers behind it.",
                        'FMS Test Results Report', height=95, width=290)
            a = get_column_letter(c0) + str(r)
            b = get_column_letter(c0 + 1) + str(r)
            av = ws.cell(r, c0 + 2)
            if NOMREF:
                av.value = f'=(IF({a}="",{NOMREF},{a})+IF({b}="",{NOMREF},{b}))/2'
            else:
                av.value = rec.get('bidir', {}).get(loc)
            av.border = bd; av.alignment = ctr; av.number_format = '0.000'
            vv = rec.get('bidir', {}).get(loc)
            if isinstance(vv, (int, float)) and vv >= good:
                av.font = Font(name=FN, size=9, bold=True, color='9C0006')
                av.fill = PatternFill('solid', fgColor='FFC7CE')
            else:
                av.font = Font(name=FN, size=9, bold=True)
        if newrib:
            for c in range(1, ncols + 1):
                ex = ws.cell(r, c).border
                ws.cell(r, c).border = Border(left=ex.left, right=ex.right,
                                              top=Side(style='medium', color='595959'),
                                              bottom=ex.bottom)
        r += 1
    for i in range(len(order)):
        c0 = 4 + i * 3
        for rr in range(LOCROW, max(r, FIRST)):
            cc = ws.cell(rr, c0)
            ex = cc.border
            cc.border = Border(left=edge, right=ex.right, top=ex.top, bottom=ex.bottom)
    for c, wd in ((1, 8), (2, 9), (3, 9)):
        ws.column_dimensions[get_column_letter(c)].width = wd
    for c in range(4, 4 + 3 * len(order)):
        ws.column_dimensions[get_column_letter(c)].width = 8.5
    ws.row_dimensions[LOCROW].height = 30
    ws.freeze_panes = ws.cell(FIRST, 4)


def _add_verify(wb, rows, meta, settings):
    """Side-by-side check: your hand-built figures vs what the pull found in FMS,
    with the event actually used and how far it sat from the stated distance."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    navy = PatternFill('solid', fgColor='1F4E79')
    teal = PatternFill('solid', fgColor='2E75B6')
    rust = PatternFill('solid', fgColor='C55A11')
    green = PatternFill('solid', fgColor='548235')
    ok = PatternFill('solid', fgColor='C6EFCE')
    warn = PatternFill('solid', fgColor='FFEB9C')
    bad = PatternFill('solid', fgColor='FFC7CE')
    HD = Font(name=FN, size=9, bold=True, color='FFFFFF')
    N9 = Font(name=FN, size=9)
    hair = Side(style='thin', color='D9D9D9')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')

    ws = wb.create_sheet('Verify')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:H1')
    ws['A1'] = f"Verify - your sheet vs FMS   ({len(rows)} rows)"
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    ws['A2'] = ("Distances come from YOUR sheet, so the location schedule plays no part here. "
                "If 'FMS A-B minus pre' is near zero, the pull is reading your PRE-remedial test. "
                "'Found at' is the event the pull actually used and 'gap' is how far it sat from "
                "your stated distance. MATCH = within 0.001 dB.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)

    groups = [('From your sheet', navy, ['Joint', 'Location', 'Ribbon', 'Fibre',
                                         'Dist A-B', 'Dist B-A', 'Your A-B', 'Your B-A', 'Your Avg']),
              ('From FMS', teal, ['Test date', 'Events', 'A-B found at', 'gap m', 'FMS A-B',
                                  'B-A found at', 'gap m', 'FMS B-A', 'FMS Avg']),
              ('Difference', green, ['d A-B', 'd B-A', 'd Avg', 'Status']),
              ('Cross-check', rust, ['Your pre-remedial', 'FMS A-B minus pre'])]
    BROW, HROW, FIRST = 4, 5, 6
    c = 1
    for title, fill, cols in groups:
        ws.merge_cells(start_row=BROW, start_column=c, end_row=BROW, end_column=c + len(cols) - 1)
        hc = ws.cell(BROW, c, title); hc.font = HD; hc.fill = fill; hc.alignment = ctr
        for h in cols:
            cc = ws.cell(HROW, c, h); cc.font = HD; cc.fill = fill
            cc.alignment = ctr; cc.border = bd
            c += 1
    r = FIRST
    for v in rows:
        vals = [v.get('joint'), v.get('location'), v.get('ribbon'), v.get('fibre'),
                v.get('dist_ab'), v.get('dist_ba'), v.get('their_ab'), v.get('their_ba'), v.get('their_avg'),
                str(v.get('test_date') or '')[:19].replace('T', ' '), v.get('n_events'),
                v.get('mine_ab_pos'), v.get('mine_ab_gap'), v.get('mine_ab'),
                v.get('mine_ba_pos'), v.get('mine_ba_gap'), v.get('mine_ba'), v.get('mine_avg'),
                v.get('d_ab'), v.get('d_ba'), v.get('d_avg'), v.get('status'),
                v.get('pre'),
                (None if (v.get('pre') is None or v.get('mine_ab_raw') is None)
                 else round(v['mine_ab_raw'] - v['pre'], 3))]
        for i, val in enumerate(vals, 1):
            cc = ws.cell(r, i, val); cc.font = N9; cc.border = bd
            cc.alignment = lft if i in (1, 2, 10, 22) else ctr
            if i in (7, 8, 9, 14, 17, 18, 19, 20, 21, 23, 24):
                cc.number_format = '0.000'
            if i in (5, 6, 12, 15):
                cc.number_format = '0'
        st = v.get('status')
        sc = ws.cell(r, 22)
        sc.fill = ok if st == 'MATCH' else (warn if st == 'CLOSE' else bad)
        sc.font = Font(name=FN, size=9, bold=True)
        for i in (19, 20, 21):
            d = ws.cell(r, i).value
            if isinstance(d, (int, float)) and abs(d) > 0.02:
                ws.cell(r, i).fill = bad
                ws.cell(r, i).font = Font(name=FN, size=9, bold=True, color='9C0006')
        r += 1
    widths = [9, 20, 8, 10, 10, 10, 10, 10, 10, 18, 8, 12, 8, 10, 12, 8, 10, 10, 9, 9, 9, 16, 14, 15]
    for i, wd in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    ws.freeze_panes = ws.cell(FIRST, 5)
    ws.auto_filter.ref = f"A{HROW}:{get_column_letter(len(widths))}{max(r-1, HROW)}"



def _add_latest_check(wb, rows, meta, settings):
    """Hybrid 'then vs now'. Splice detail can only come from an analysed iOLM, but the
    newest raw OTDR still carries overall link loss, so this sheet puts the two side by
    side and shows whether each fibre actually improved after remedial work."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    navy = PatternFill('solid', fgColor='1F4E79')
    teal = PatternFill('solid', fgColor='2E75B6')
    rust = PatternFill('solid', fgColor='C55A11')
    green = PatternFill('solid', fgColor='548235')
    ok = PatternFill('solid', fgColor='C6EFCE')
    warn = PatternFill('solid', fgColor='FFEB9C')
    bad = PatternFill('solid', fgColor='FFC7CE')
    HD = Font(name=FN, size=9, bold=True, color='FFFFFF')
    N9 = Font(name=FN, size=9)
    hair = Side(style='thin', color='D9D9D9')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')

    ws = wb.create_sheet('Latest Test Check')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:N1')
    ws['A1'] = 'Latest Test Check - splice detail vs newest trace'
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    ws['A2'] = ("Left block = the analysed iOLM every splice figure in this workbook comes from. "
                "Right block = the newest test on that fibre whatever its type. A raw OTDR has no "
                "event table, so it cannot say WHICH location changed, but its overall loss is "
                "current. A negative Change means loss came out of the fibre after the splice "
                "detail was captured, so that remedial work is done but not itemised here.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)

    groups = [('Fibre', navy, ['End', 'Fibre', 'Description']),
              ('Splice detail taken from', teal, ['Test date', 'Type', 'Loss then', 'Length m']),
              ('Newest test on this fibre', rust, ['Test date', 'Type', 'Loss now', 'Length m']),
              ('Verdict', green, ['Change dB', 'Meaning'])]
    BROW, HROW, FIRST = 4, 5, 6
    c = 1
    for title, fill, cols in groups:
        ws.merge_cells(start_row=BROW, start_column=c, end_row=BROW, end_column=c + len(cols) - 1)
        hc = ws.cell(BROW, c, title); hc.font = HD; hc.fill = fill; hc.alignment = ctr
        for h in cols:
            cc = ws.cell(HROW, c, h); cc.font = HD; cc.fill = fill
            cc.alignment = ctr; cc.border = bd
            c += 1
    r = FIRST
    for v in rows:
        vals = [v.get('end'), v.get('fid'), v.get('desc'),
                str(v.get('ev_date') or '').replace('T', ' '), v.get('ev_type'),
                v.get('loss_then'), v.get('len_then'),
                str(v.get('latest_date') or '').replace('T', ' '), v.get('latest_type'),
                v.get('loss_now'), v.get('len_now'),
                v.get('change'), v.get('note')]
        for i, val in enumerate(vals, 1):
            cc = ws.cell(r, i, val); cc.font = N9; cc.border = bd
            cc.alignment = lft if i in (2, 3, 13) else ctr
            if i in (6, 10, 12):
                cc.number_format = '0.000'
            if i in (7, 11):
                cc.number_format = '0'
        ch = v.get('change')
        cc = ws.cell(r, 12)
        if isinstance(ch, (int, float)):
            if ch <= -0.05:
                cc.fill = ok; cc.font = Font(name=FN, size=9, bold=True, color='006100')
            elif ch >= 0.05:
                cc.fill = bad; cc.font = Font(name=FN, size=9, bold=True, color='9C0006')
            else:
                cc.fill = warn
        if not v.get('latest_date'):
            ws.cell(r, 8).fill = warn
        r += 1
    for i, wd in enumerate([16, 26, 16, 19, 8, 11, 10, 19, 8, 11, 10, 11, 58], 1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    ws.freeze_panes = ws.cell(FIRST, 4)
    ws.auto_filter.ref = f"A{HROW}:M{max(r-1, HROW)}"


def _add_otdr_measured(wb, rows, diag, meta, settings):
    """Splice loss measured directly off the raw OTDR trace at each scheduled location.
    This exists because a raw trace carries no event table through the API: the events are
    still in the data, they have just never been extracted. Measuring at a known distance
    is the same two-sided least squares fit an OTDR uses at a marker."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    navy = PatternFill('solid', fgColor='1F4E79')
    teal = PatternFill('solid', fgColor='2E75B6')
    warn = PatternFill('solid', fgColor='FFEB9C')
    HD = Font(name=FN, size=9, bold=True, color='FFFFFF')
    N9 = Font(name=FN, size=9)
    hair = Side(style='thin', color='D9D9D9')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')
    good = settings.get('good_splice', 0.15)
    med = settings.get('medium_splice', 0.1875)

    ws = wb.create_sheet('OTDR Measured')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:J1')
    ws['A1'] = 'Splice loss measured from the raw OTDR trace'
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    d = diag or {}
    ws['A2'] = (f"Measured at each scheduled distance by fitting the backscatter slope either side "
                f"and taking the step between the two fits, which is what an OTDR does at a marker. "
                f"Traces analysed: {d.get('traces', 0)}, failed: {d.get('failed', 0)}. "
                f"Decoded as: {d.get('how') or 'n/a'}.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)
    _sl, _tg = d.get('slope_km'), d.get('target_km')
    if _sl is not None and _tg and abs(_sl - _tg) > max(0.06, _tg * 0.3):
        ws['A4'] = (f"CALIBRATION WARNING: the trace works out at {_sl} dB/km but the instrument "
                    f"recorded {_tg} dB/km for the same measurement. The distance axis or the "
                    f"sample unit is therefore probably wrong, which puts every measurement below "
                    f"in doubt. Spacing came from: {d.get('spacing_src')}.")
        ws['A4'].font = Font(name=FN, size=10, bold=True, color='FFFFFF')
        ws['A4'].fill = PatternFill('solid', fgColor='C00000')
    ws['A3'] = ("Treat these as measurements, not as certified results: check a few against the FMS "
                "viewer before issuing. Anything flagged in the Quality column had a poor slope fit, "
                "usually noise, a reflective event or two joints too close together to separate.")
    ws['A3'].font = Font(name=FN, size=9, italic=True, color='9C0006')

    # key on location AND leg: a backsplice loop passes each location twice, and keying
    # on the name alone silently overwrote the outbound reading with the return one
    locs, fibres = [], []
    for r in rows:
        k = r.get('col') or r.get('location')
        r['_col'] = k
        if k not in locs:
            locs.append(k)
        if r.get('fid') not in fibres:
            fibres.append(r['fid'])
    dist_of = {}
    for r in rows:
        k = r['_col']
        if k not in dist_of or (r.get('sched_m') or 0) < dist_of[k]:
            dist_of[k] = r.get('sched_m') or 0
    order = sorted(locs, key=lambda L: dist_of.get(L, 0))
    grid, qual = {}, {}
    for r in rows:
        grid[(r['fid'], r['_col'])] = r.get('loss')
        qual[(r['fid'], r['_col'])] = r.get('quality')

    BROW, HROW, FIRST = 5, 6, 7
    for c, lab in ((1, 'Binder'), (2, 'Ribbon'), (3, 'Fibre')):
        hc = ws.cell(BROW, c, lab); hc.font = HD; hc.fill = navy
        hc.alignment = ctr; hc.border = bd
    ws.merge_cells(start_row=BROW, start_column=4, end_row=BROW, end_column=3 + max(len(order), 1))
    hc = ws.cell(BROW, 4, 'Measured splice loss by location (dB)')
    hc.font = HD; hc.fill = teal; hc.alignment = ctr
    ws.cell(HROW, 3, 'Location ->').font = Font(name=FN, size=8, italic=True)
    for i, loc in enumerate(order):
        m = dist_of.get(loc, 0)
        cc = ws.cell(HROW, 4 + i, f"{loc}\n{round(m)} m")
        cc.font = Font(name=FN, size=8, bold=True, color='FFFFFF')
        cc.fill = teal; cc.alignment = ctr; cc.border = bd
    r = FIRST
    last_rib = None
    for f in fibres:
        b_id, rib, fno = fibre_ids(f, settings)
        newrib = (last_rib is not None and rib != last_rib)
        last_rib = rib
        for c, v in ((1, f"B{b_id}" if b_id else None),
                     (2, f"R{rib:02d}" if rib else None),
                     (3, f"F{fno:03d}" if fno else f)):
            cc = ws.cell(r, c, v); cc.font = N9
            cc.alignment = ctr if c < 3 else lft
            cc.border = bd
        if newrib:
            for c in range(1, 4 + max(len(order), 1)):
                ex = ws.cell(r, c).border
                ws.cell(r, c).border = Border(left=ex.left, right=ex.right,
                                              top=Side(style='medium', color='595959'),
                                              bottom=ex.bottom)
        for i, loc in enumerate(order):
            v = grid.get((f, loc))
            cc = ws.cell(r, 4 + i, None if v is None else round(v, 3))
            cc.border = bd; cc.alignment = ctr; cc.number_format = '0.000'
            fo, fl = _lossfont(v, good, med, size=9)
            cc.font = fo
            if fl:
                cc.fill = fl
            q = qual.get((f, loc))
            if q and q != 'ok':
                cc.fill = warn
                cc.comment = None
        r += 1
    for c, wd in ((1, 8), (2, 9), (3, 9)):
        ws.column_dimensions[get_column_letter(c)].width = wd
    for c in range(4, 4 + max(len(order), 1)):
        ws.column_dimensions[get_column_letter(c)].width = 12
    ws.row_dimensions[HROW].height = 30
    ws.freeze_panes = ws.cell(FIRST, 4)


def _place_logo(ws, anchor='A1', height_px=52):
    """Drop the company logo in if one is sitting next to the tool.

    Deliberately file-driven rather than embedded in the code: put logo.png (or .jpg)
    beside the script and every sheet picks it up, swap the file and the branding changes
    with no new build."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ('logo.png', 'logo.jpg', 'logo.jpeg', 'logo.gif'):
        path = os.path.join(here, name)
        if not os.path.isfile(path):
            continue
        try:
            from openpyxl.drawing.image import Image as XLImage
            img = XLImage(path)
            if img.height:
                scale = float(height_px) / float(img.height)
                img.height = int(img.height * scale)
                img.width = int(img.width * scale)
            img.anchor = anchor
            ws.add_image(img)
            return True
        except Exception:
            return False
    return False


def _add_client_verify(wb, rows, meta, settings, info=None):
    """Side by side against a client report: their number, mine, the difference.

    Split into end-to-end and splice blocks because they disagree for different reasons,
    and the splice block carries the position gap, which usually explains a difference
    before the loss figures do."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    FN = 'Arial'
    navy = PatternFill('solid', fgColor='1F4E79')
    teal = PatternFill('solid', fgColor='2E75B6')
    green = PatternFill('solid', fgColor='548235')
    ok = PatternFill('solid', fgColor='C6EFCE')
    warn = PatternFill('solid', fgColor='FFEB9C')
    bad = PatternFill('solid', fgColor='FFC7CE')
    grey = PatternFill('solid', fgColor='D9D9D9')
    HD = Font(name=FN, size=9, bold=True, color='FFFFFF')
    N9 = Font(name=FN, size=9)
    hair = Side(style='thin', color='D9D9D9')
    bd = Border(left=hair, right=hair, top=hair, bottom=hair)
    ctr = Alignment(horizontal='center', vertical='center', wrap_text=True)
    lft = Alignment(horizontal='left', vertical='center')
    FILLS = {'MATCH': ok, 'CLOSE': warn, 'DIFFERS': bad,
             'NOT FOUND': bad, 'NOT IN PULL': grey, 'NO VALUE': grey}

    e2e = [r for r in rows if r.get('kind') == 'E2E']
    spl = [r for r in rows if r.get('kind') == 'Splice']
    i = info or {}
    ws = wb.create_sheet('Client Verify')
    ws.sheet_view.showGridLines = False
    ws.merge_cells('A1:N1')
    ws['A1'] = 'Client report vs this pull'
    ws['A1'].font = Font(name=FN, size=13, bold=True, color='FFFFFF')
    ws['A1'].fill = navy; ws['A1'].alignment = lft
    ws['A2'] = (f"Client report: {i.get('cable') or '?'}  {i.get('siteA') or ''} to "
                f"{i.get('siteB') or ''}, tested {', '.join(i.get('dates') or []) or '?'}. "
                f"{len(e2e)} fibres and {len(spl)} splices compared. "
                f"E2E: MATCH within 0.05 dB, CLOSE within 0.25. "
                f"Splices: MATCH within 0.02 dB, CLOSE within 0.08.")
    ws['A2'].font = Font(name=FN, size=9, italic=True)
    ws['A3'] = ("A splice difference is only meaningful when the position gap is small. "
                "A few dB apart at a gap of 30 m or more usually means the two reports are "
                "describing different joints, not disagreeing about one.")
    ws['A3'].font = Font(name=FN, size=9, italic=True, color='9C0006')

    r = 5
    ws.cell(r, 1, 'END TO END LOSS').font = Font(name=FN, size=11, bold=True, color='1F4E79')
    r += 1
    cols = ['Fibre', 'Their date', 'My date', 'Their A-B', 'My A-B', 'diff',
            'Their B-A', 'My B-A', 'diff', 'Their length', 'My length', 'Status']
    fills = [navy, navy, navy, teal, teal, green, teal, teal, green, navy, navy, green]
    for c, (h, f) in enumerate(zip(cols, fills), 1):
        cc = ws.cell(r, c, h); cc.font = HD; cc.fill = f; cc.alignment = ctr; cc.border = bd
    hrow_e2e = r
    r += 1
    for v in e2e:
        vals = [v.get('fid'), v.get('their_date'), v.get('mine_date'),
                v.get('their_ab'), v.get('mine_ab'), v.get('d_ab'),
                v.get('their_ba'), v.get('mine_ba'), v.get('d_ba'),
                v.get('their_len'), v.get('mine_len'), v.get('status')]
        for c, val in enumerate(vals, 1):
            cc = ws.cell(r, c, val); cc.font = N9; cc.border = bd
            cc.alignment = lft if c in (1, 2, 3) else ctr
            if c in (4, 5, 6, 7, 8, 9):
                cc.number_format = '0.000'
            if c in (10, 11):
                cc.number_format = '0.0'
        sc = ws.cell(r, 12)
        sc.fill = FILLS.get(v.get('status'), grey)
        sc.font = Font(name=FN, size=9, bold=True)
        r += 1
    last_e2e = r - 1

    if spl:
        r += 2
        ws.cell(r, 1, 'SPLICES FROM THE CLIENT REPORT').font = Font(
            name=FN, size=11, bold=True, color='1F4E79')
        r += 1
        cols2 = ['Fibre', 'Their position (m)', 'Found at (m)', 'gap m',
                 'Their loss', 'My loss', 'diff', 'Status']
        fills2 = [navy, navy, teal, teal, navy, teal, green, green]
        for c, (h, f) in enumerate(zip(cols2, fills2), 1):
            cc = ws.cell(r, c, h); cc.font = HD; cc.fill = f
            cc.alignment = ctr; cc.border = bd
        r += 1
        for v in spl:
            vals = [v.get('fid'), v.get('position'), v.get('found_at'), v.get('gap'),
                    v.get('their_ab'), v.get('mine_ab'), v.get('d_ab'), v.get('status')]
            for c, val in enumerate(vals, 1):
                cc = ws.cell(r, c, val); cc.font = N9; cc.border = bd
                cc.alignment = lft if c == 1 else ctr
                if c in (5, 6, 7):
                    cc.number_format = '0.000'
                if c in (2, 3, 4):
                    cc.number_format = '0'
            sc = ws.cell(r, 8)
            sc.fill = FILLS.get(v.get('status'), grey)
            sc.font = Font(name=FN, size=9, bold=True)
            g = v.get('gap')
            if isinstance(g, (int, float)) and abs(g) > 30:
                ws.cell(r, 4).fill = warn
            r += 1

    for c, wd in enumerate([26, 21, 21, 11, 11, 9, 11, 11, 9, 13, 12, 13], 1):
        ws.column_dimensions[get_column_letter(c)].width = wd
    ws.freeze_panes = ws.cell(hrow_e2e + 1, 2)
    ws.auto_filter.ref = f"A{hrow_e2e}:L{max(last_e2e, hrow_e2e)}"
