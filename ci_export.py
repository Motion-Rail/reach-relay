"""Cable Insight: Excel exports of one pull or of a comparison between two pulls."""
import io
from ci_store import tested_span

def _spur(l):
    l = l or {}
    if not l.get("spur"):
        return ""
    leg = l.get("spur_leg")
    if not leg:
        return l["spur"]
    return f"{l['spur']}, " + (f"end {l['spur_m']} m" if leg == "end" else f"{leg} {l['spur_m']} m")


HOW = {"both": "both ends", "nominal": "one end + nominal", "ribbon": "one end + ribbon average",
       "one_way": "one end only", None: ""}
STATUS = {"solved": "Solved", "remaining": "Remaining", "new": "New", "not_seen": "Not seen now"}
FILL = {"Solved": "C6EFCE", "Remaining": "FFC7CE", "New": "FFEB9C", "Not seen now": "D9D9D9",
        "Fail": "FFC7CE", "Pass": "C6EFCE"}


def _book():
    from openpyxl import Workbook
    return Workbook()


def _sheet(wb, title, header, rows, widths=None, status_col=None, first=False):
    from openpyxl.styles import Alignment, Font, PatternFill
    ws = wb.active if first else wb.create_sheet()
    ws.title = title[:31]
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3864")
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    if status_col is not None:
        for row in ws.iter_rows(min_row=2):
            v = row[status_col].value
            if v in FILL:
                row[status_col].fill = PatternFill("solid", fgColor=FILL[v])
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    return ws


def _save(wb):
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _settings_rows(st):
    return [["Pass below (dB)", st["threshold"]],
            ["S-ODF limit (dB)", f"{st['sodf_limit']} for NR ribbons {st['nr_ribbons']}"],
            ["One end only", {"nominal": f"nominal {st['nominal']} dB",
                              "auto": f"ribbon average if half the ribbon saw it, else {st['nominal']} dB",
                              "exclude": "left out"}.get(st["one_end"], st["one_end"])],
            ["Spur gap (m)", st["spur_gap"]], ["Group tolerance (m)", st["group_tol"]]]


def compare_book(c, cable_name):
    st, t = c["settings"], c["totals"]
    wb = _book()
    g = next((x for x in c["groups"] if x["id"] == c["group"]), None)
    summary = [["Cable", cable_name], ["Section", c["new"]["section"]],
               ["Before", f"{c['base']['label']}  ({c['base']['t_from']} to {c['base']['t_to']} UTC)"],
               ["Now", f"{c['new']['label']}  ({c['new']['t_from']} to {c['new']['t_to']} UTC)"],
               ["Fibres", g["label"] if g else "All fibres"], [],
               ["Fails before", t["fail_before"]], ["Fails now", t["fail_now"]],
               ["Solved", t["solved"]], ["Remaining", t["remaining"]], ["New", t["new"]],
               ["Not seen now", t["not_seen"]], []] + _settings_rows(st)
    _sheet(wb, "Summary", ["Item", "Value"], summary, [24, 70], first=True)
    _sheet(wb, "By location", ["Location", "km from End A", "Spur", "S-ODF", "Solved", "Remaining", "New",
                               "Not seen now"],
           [[l["loc"], round(l["pos"] / 1000, 3) if l["pos"] is not None else None, l["spur"] or "",
             "yes" if l["sodf"] else "", l["solved"], l["remaining"], l["new"], l["not_seen"]]
            for l in c["locations"]], [40, 14, 30, 8, 10, 12, 10, 14])
    _sheet(wb, "By ribbon", ["Ribbon", "Solved", "Remaining", "New", "Not seen now"],
           [[f"R{r['ribbon']}", r["solved"], r["remaining"], r["new"], r["not_seen"]] for r in c["ribbons"]],
           [10, 10, 12, 10, 14])
    _sheet(wb, "Splices", ["Status", "Fibre", "Ribbon", "Location", "km from End A", "Spur", "Limit (dB)",
                           "Before (dB)", "Before from", "Now (dB)", "Now from", "Change (dB)"],
           [[STATUS[r["status"]], f"F{r['fno']:03d}", r["ribbon"], r["loc"],
             round(r["loc_pos"] / 1000, 3) if r["loc_pos"] is not None else None, r["spur"] or "",
             r["limit"], r["before"], HOW[r["before_how"]], r["now"], HOW[r["now_how"]], r["change"]]
            for r in c["rows"]], [14, 8, 8, 40, 14, 28, 10, 11, 22, 10, 22, 11], status_col=0)
    return _save(wb)


def dataset_book(store, section, ds, st, cable_name):
    rows, an = store.judged(section, ds, st)
    info = next(d for d in store.datasets(section) if d["id"] == ds)
    meta = {l["loc"]: l for l in an["locs"]}
    grp = {}
    for g in an["groups"]:
        for f in g["fnos"]:
            grp[f] = g["id"]
    wb = _book()
    fails = [r for r in rows.values() if r["fail"]]
    summary = [["Cable", cable_name], ["Section", section], ["Pull", info["label"]],
               ["Tested", tested_span(info['t_from'], info['t_to'])], ["Fibres", info["n_fibres"]],
               ["Splices judged", len(rows)], ["Fail", len(fails)], ["Pass", len(rows) - len(fails)], []] \
        + [[g["label"], g.get("route", "")] for g in an["groups"]] + [[]] + [[sp["name"] + (f" (leaves at {sp['start'] / 1000:.3f} km, {sp['length']} m long)" if sp.get("length") is not None else ""),
                " · ".join(sp["locations"])] for sp in an["spurs"]] \
        + [[]] + _settings_rows(st)
    _sheet(wb, "Summary", ["Item", "Value"], summary, [40, 80], first=True)
    ordered = sorted(rows.values(), key=lambda r: ((meta.get(r["loc"]) or {}).get("pos") or 9e9, r["fno"]))
    _sheet(wb, "Splices", ["Result", "Fibre", "Ribbon", "Group", "Location", "Backbone km from End A", "Spur", "S-ODF",
                           "A-B @1550", "B-A @1550", "Loss (dB)", "From", "Limit (dB)"],
           [["Fail" if r["fail"] else "Pass", f"F{r['fno']:03d}", r["ribbon"], grp.get(r["fno"], ""), r["loc"],
             round(((meta.get(r["loc"]) or {}).get("pos") or 0) / 1000, 3), _spur(meta.get(r["loc"])),
             "yes" if (meta.get(r["loc"]) or {}).get("sodf") else "", r["ab"], r["ba"], r["value"], HOW[r["how"]],
             r["limit"]] for r in ordered],
           [8, 8, 8, 8, 40, 14, 28, 8, 11, 11, 10, 22, 10], status_col=0)
    return _save(wb)


TRACK = {"new": "New", "back": "Came back", "still": "Still failing", "fixed": "Fixed", "not_seen": "Not in the latest test"}


def track_book(t, cable_name):
    """Several tests side by side: every splice that failed in any of them, its loss in each test and
    what happened to it; the fails at every joint test by test; and the tests themselves."""
    from openpyxl.styles import PatternFill
    wb = _book()
    pulls = t["pulls"]
    heads = [f"{p['label']}" for p in pulls]
    summary = [["Cable", cable_name], ["Tests", len(pulls)]] + [[p["label"], f"{p['fails']} fails of {p['judged']} splices"]
                                                                  for p in pulls] + [[]] + \
        [[TRACK[k], v] for k, v in t["counts"].items()] + [[]] + \
        [["ODF remedials: " + TRACK[k], v] for k, v in (t.get("odf_counts") or {}).items()] + [[]] + _settings_rows(t["settings"])
    _sheet(wb, "Summary", ["Item", "Value"], summary, [40, 50], first=True)
    rows = []
    for r in t["rows"]:
        place = f"{r['spur']}, {r['spur_leg']} {r['spur_m']} m" if r.get("spur_leg") else (r.get("spur") or "")
        rows.append([TRACK[r["status"]], r.get("since_label") or "", f"F{r['fno']:03d}", r["ribbon"], r["loc"],
                     round(r["pos"] / 1000, 3) if r["pos"] is not None else None, place]
                    + [r["values"].get(str(p["id"])) for p in pulls]
                    + [r["limit"], r["change"], f"{r['fails_in']} of {len(pulls)}"])
    ws = _sheet(wb, "Splices over time", ["What happened", "Since", "Fibre", "Ribbon", "Joint", "Backbone km", "Spur"]
                + heads + ["Limit (dB)", "Change first to last (dB)", "Failed in"], rows,
                [20, 26, 8, 8, 34, 12, 26] + [14] * len(pulls) + [10, 12, 10], status_col=None)
    red, green = PatternFill("solid", fgColor=FILL["Fail"]), PatternFill("solid", fgColor=FILL["Pass"])
    for i, r in enumerate(t["rows"], start=2):
        for k, p in enumerate(pulls):
            f = r["fail"].get(str(p["id"]))
            if f is not None:
                ws.cell(i, 8 + k).fill = red if f else green
    orows = [[TRACK[r["status"]], r.get("since_label") or "", f"F{r['fno']:03d}", r["ribbon"], r["end"], r["site"], r["pos"]]
             + [r["values"].get(str(p["id"])) for p in pulls] + [r["change"], f"{r['fails_in']} of {len(pulls)}"]
             for r in t.get("odf_rows") or []]
    ws2 = _sheet(wb, "ODF remedials over time", ["What happened", "Since", "Fibre", "Ribbon", "End", "ODF at", "Position (m)"]
                 + heads + ["Change first to last (dB)", "Failed in"], orows, [20, 26, 8, 8, 6, 12, 12] + [14] * len(pulls) + [12, 10])
    for i, r in enumerate(t.get("odf_rows") or [], start=2):
        for k, p in enumerate(pulls):
            f = r["fail"].get(str(p["id"]))
            if f is not None:
                ws2.cell(i, 8 + k).fill = red if f else green
    jr = [[j["loc"], round(j["pos"] / 1000, 3), j.get("spur") or ""] + [j["fails"].get(p["id"]) for p in pulls]
          for j in t["joints"]]
    _sheet(wb, "Fails by joint", ["Joint", "Backbone km", "Spur"] + heads, jr, [34, 12, 26] + [14] * len(pulls))
    return _save(wb)


def remedial_book(rm, cable_name, sm=None):
    """The remedial list of one test: splice remedials and, on their own sheet, ODF remedials."""
    st = rm["settings"]
    wb = _book()
    c = rm["counts"]
    d = rm["dataset"]
    summary = [["Cable", cable_name], ["Test", d["label"]], ["Tested", tested_span(d['t_from'], d['t_to'])], [],
               ["Splice remedials", f"{c['splice']} splices at {c['joints']} joints"],
               ["ODF remedials", f"{c['odf']} ({c['odf_A']} at {d.get('site_a') or 'End A'}, {c['odf_B']} at {d.get('site_b') or 'End B'})"],
               ["Fibres with a remedial", c["fibres"]], []] + _settings_rows(st) + [["ODF limit (dB)", f"over {st.get('odf_limit', 0.5)} fails"]]
    if sm:
        p_, b_ = sm.get("pct") or {}, sm.get("bands") or []
        oc = rm["counts"]
        summary += [[], ["Splices judged", p_.get("judged")], ["Pass", f"{sm['counts']['pass']} ({p_.get('pass')}%)"],
                    ["Fail", f"{sm['counts']['fail']} ({p_.get('fail')}%)"], ["From both ends", f"{sm['counts']['both']} ({p_.get('both')}%)"],
                    ["One end filled", f"{sm['counts']['filled']} ({p_.get('filled')}%)"], []] + \
                   [[("Gainer, loss under 0 dB" if b["hi"] == 0 else "Loss " + b["label"] + " dB"), f"{b['n']} ({b['pct']}%)"] for b in b_]
    _sheet(wb, "Summary", ["Item", "Value"], summary, [26, 70], first=True)
    _sheet(wb, "Splice remedials", ["Fibre", "Ribbon", "Joint", "Type", "Backbone km", "Spur", "S-ODF", "A-B @1550", "B-A @1550",
                                    "Loss (dB)", "From", "Limit (dB)", "On its trace (m)"],
           [[f"F{r['fno']:03d}", r["ribbon"], r["loc"], r["kind"], round(r["pos"] / 1000, 3) if r["pos"] is not None else None,
             (f"{r['spur']}, {r['spur_leg']} {r['spur_m']} m" if r.get("spur_leg") else (r.get("spur") or "")),
             "yes" if r["sodf"] else "", r["ab"], r["ba"], r["value"], HOW.get(r["how"], ""), r["limit"], r["trace_pos"]]
            for r in rm["splices"]], [8, 8, 34, 12, 12, 30, 7, 11, 11, 10, 22, 10, 14])
    _sheet(wb, "ODF remedials", ["Fibre", "Ribbon", "End", "ODF at", "Position on its own trace (m)", "Event", "Reflectance (dB)",
                                 "Loss @1310", "Loss @1550", "Loss @1625", "Limit (dB)"],
           [[f"F{r['fno']:03d}", r["ribbon"], r["end"], r["site"], r["pos"], r["type"], r["refl"], r["w1310"], r["w1550"],
             r["w1625"], r["limit"]] for r in rm["odfs"]], [8, 8, 6, 14, 16, 12, 12, 11, 11, 11, 10])
    return _save(wb)


def jointer_book(t, cable_name):
    """Cable Insight v0.12: out of spec ribbons per joint, tallied against each jointer."""
    wb = _book()
    d, st, T = t["dataset"], t["settings"], t["totals"]
    summary = [["Cable", cable_name], ["Test", d.get("label")], ["Tested", tested_span(d.get('t_from'), d.get('t_to'))],
               ["Jointers from", t.get("source", "")], [],
               ["Out of spec ribbon", f"any fibre in the ribbon fails at that joint (pass under {st['threshold']} dB, S-ODF {st.get('sodf_limit')} dB for R{st.get('nr_ribbons')}); the whole ribbon is re-spliced"],
               ["Joints", T["joints"]], ["Ribbons spliced", T["ribbons_spliced"]], ["Out of spec ribbons", T["ribbons_out"]],
               ["Failing fibres", T["fibres_fail"]],
               ["No jointer recorded", f"{t['not_recorded']['joints']} joints, {t['not_recorded']['ribbons_out']} out of spec ribbons"]]
    _sheet(wb, "Summary", ["Item", "Value"], summary, [24, 110], first=True)
    P = t.get("people") or {}
    who = lambda k: " / ".join(dict.fromkeys(P.get(x.strip(), x.strip()) for x in str(k).split("/")))   # noqa: E731
    head = ["Jointer", "Name", "Joints", "Ribbons spliced", "Out of spec ribbons", "Per joint", "% of ribbons", "Joints with any",
            "Failing fibres", "Re-spliced ribbons (tracker)", "Exceptions (tracker)", "Tally by joint"]
    rows = lambda xs: [[x["jointer"], who(x["jointer"]), x["joints"], x["ribbons_spliced"], x["ribbons_out"], x["per_joint"], x["pct"],   # noqa: E731
                        x["joints_with_out"], x["fibres_fail"], x.get("ribbons_remediated"), x.get("exceptions"), ", ".join(x["joint_list"])] for x in xs]
    W = [12, 30, 8, 10, 12, 9, 10, 10, 10, 12, 12, 90]
    _sheet(wb, "Tally by jointer", head, rows(t["by_jointer"]), W)
    _sheet(wb, "Tally by person", head, rows(t["by_person"]), W)
    if t.get("technicians"):
        _sheet(wb, "Remediation by technician", ["Technician", "Name", "Attempts", "Ribbons worked", "First attempts", "First attempt passes",
                                                 "First attempt pass %", "All attempts pass %", "Exceptions left", "ODF attempts"],
               [[x["tech"], P.get(x["tech"], ""), x["attempts"], x["ribbons"], x["first"], x["first_pass"], x["first_pct"], x["pass_pct"],
                 x["exceptions"], x["odf"]] for x in t["technicians"]], [12, 26, 9, 10, 10, 10, 10, 10, 10, 10])
    if t.get("odf_ends"):
        _sheet(wb, "ODF ends", ["ODF", "Terminated by", "Completed", "Ribbons re-done (tracker)", "Fibres", "Exceptions"],
               [[o["name"], o["jointers"], o["completed"], o.get("ribbons_remediated", 0), o.get("fibres_remediated", 0), o.get("exceptions", 0)]
                for o in t["odf_ends"]], [40, 14, 12, 12, 10, 10])
    _sheet(wb, "By joint", ["Joint", "Name", "Type", "Mileage", "Jointer", "Completed", "Ribbons spliced", "Out of spec ribbons",
                            "Which ribbons", "Failing fibres", "Worst dB", "Re-spliced ribbons (tracker)", "Which ribbons (tracker)", "Exceptions"],
           [[j["key"], j["name"], j["type"], j["mileage"], j["jointers"], j["completed"], j["ribbons_spliced"], j["ribbons_out"],
             j["out_list"], j["fibres_fail"], j["worst"], j.get("ribbons_remediated"), j.get("remediated_list"), j.get("exceptions")]
            for j in t["joints"]], [14, 40, 30, 15, 10, 12, 10, 12, 30, 10, 9, 12, 30, 10])
    _sheet(wb, "Ribbons out of spec", ["Joint", "Jointer", "Ribbon", "Failing fibres", "Worst dB", "Fibres"],
           [[j["key"], j["jointers"], f"R{o['ribbon']}", o["fibres"], o["worst"], ", ".join(o["fails"])] for j in t["joints"] for o in j["out"]],
           [14, 10, 8, 10, 9, 90])
    return _save(wb)
