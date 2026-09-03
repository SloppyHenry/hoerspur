#!/usr/bin/env python3
"""Curses front-end for hoerspur: pick a folder, analyse it, compare the
current state against the target, confirm."""

import curses, os, queue, threading, traceback
import hoerspur as H

K_ESC = 27


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------- primitives

def put(win, y, x, text, attr=0):
    """Write clipped to the window; curses raises at the last cell."""
    h, w = win.getmaxyx()
    if not (0 <= y < h) or x >= w:
        return
    try:
        win.addnstr(y, x, str(text), max(0, w - x - 1), attr)
    except curses.error:
        pass


def hline(win, y, ch="─"):
    h, w = win.getmaxyx()
    if 0 <= y < h:
        put(win, y, 0, ch * (w - 1), curses.color_pair(4))


def ell(s, n):
    s = str(s)
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def human(sec):
    sec = int(sec)
    return f"{sec//3600}:{sec%3600//60:02d}:{sec%60:02d}"


class Screen:
    """Shared chrome: title bar, status line, key hints."""

    def __init__(self, stdscr):
        self.s = stdscr

    def frame(self, title, sub="", hints=""):
        self.s.erase()
        h, w = self.s.getmaxyx()
        put(self.s, 0, 0, " " * (w - 1), curses.color_pair(1))
        put(self.s, 0, 1, "hoerspur", curses.color_pair(1) | curses.A_BOLD)
        put(self.s, 0, 15, ell(title, w - 17), curses.color_pair(1))
        if sub:
            put(self.s, 1, 1, ell(sub, w - 3), curses.color_pair(5))
        if hints:
            put(self.s, h - 1, 1, ell(hints, w - 3), curses.color_pair(4))
        return h, w


# --------------------------------------------------------------- folder picker

def pick_folder(scr, start):
    cur = os.path.abspath(start)
    sel, top = 0, 0
    while True:
        try:
            subs = sorted(d for d in os.listdir(cur)
                          if os.path.isdir(os.path.join(cur, d)) and not d.startswith("."))
        except PermissionError:
            subs = []
        naudio = len([f for f in os.listdir(cur)
                      if f.lower().endswith(H.AUDIO_EXT)]) if os.access(cur, os.R_OK) else 0
        entries = [".. (übergeordnet)"] + subs
        h, w = scr.frame("Ordner wählen", cur,
                         "↑↓ bewegen   ↵ öffnen   a analysieren   q Ende")
        body = h - 5
        sel = max(0, min(sel, len(entries) - 1))
        top = max(min(top, sel), sel - body + 1, 0)
        for i in range(top, min(len(entries), top + body)):
            y = 3 + i - top
            mark = "▸ " if i == sel else "  "
            attr = curses.color_pair(2) | curses.A_BOLD if i == sel else 0
            put(scr.s, y, 1, mark + ell(entries[i], w - 6), attr)
        note = (f"{naudio} Audiodateien hier — [a] analysieren"
                if naudio else "keine Audiodateien in diesem Ordner")
        put(scr.s, h - 3, 1, note,
            curses.color_pair(3) | curses.A_BOLD if naudio else curses.color_pair(4))
        scr.s.refresh()
        k = scr.s.getch()
        if k in (ord("q"), K_ESC):
            raise Cancelled
        if k in (curses.KEY_UP, ord("k")):
            sel -= 1
        elif k in (curses.KEY_DOWN, ord("j")):
            sel += 1
        elif k == curses.KEY_NPAGE:
            sel += body
        elif k == curses.KEY_PPAGE:
            sel -= body
        elif k in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
            cur = os.path.abspath(os.path.join(cur, ".." if sel == 0 else entries[sel]))
            sel, top = 0, 0
        elif k == curses.KEY_LEFT:
            cur = os.path.dirname(cur)
            sel, top = 0, 0
        elif k == ord("a") and naudio:
            return cur


# -------------------------------------------------------------------- worker

def run_job(scr, title, fn):
    """Run fn(report) on a thread while drawing a live status screen."""
    q, box = queue.Queue(), {}

    def report(msg):
        q.put(msg)

    def body():
        try:
            box["val"] = fn(report)
        except BaseException as e:                      # noqa: BLE001
            box["err"] = e
            box["tb"] = traceback.format_exc()
        q.put(None)

    t = threading.Thread(target=body, daemon=True)
    t.start()
    lines, spin, done = [], 0, False
    scr.s.nodelay(True)
    try:
        while not done:
            try:
                while True:
                    m = q.get_nowait()
                    if m is None:
                        done = True
                        break
                    lines.append(m)
                    lines[:] = lines[-12:]
            except queue.Empty:
                pass
            h, w = scr.frame(title, "", "bitte warten …")
            put(scr.s, 2, 1, "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[spin % 10] + "  arbeite",
                curses.color_pair(3) | curses.A_BOLD)
            for i, m in enumerate(lines):
                put(scr.s, 4 + i, 3, ell(m, w - 5), curses.color_pair(5))
            scr.s.refresh()
            spin += 1
            curses.napms(80)
            scr.s.getch()
    finally:
        scr.s.nodelay(False)
    if "err" in box:
        raise box["err"]
    return box["val"]


# ---------------------------------------------------------------- candidates

def pick_candidate(scr, cands):
    ok = [c for c in cands if c.get("blocks")]
    if not ok:
        return None
    sel = 0
    while True:
        h, w = scr.frame("Passende Ausgaben",
                         f"{len(ok)} von {len(cands)} Treffern passen zur Laufzeit",
                         "↑↓ wählen   ↵ übernehmen   q Ende")
        for i, c in enumerate(ok):
            y = 3 + i * 3
            if y + 2 >= h - 1:
                break
            on = i == sel
            put(scr.s, y, 1, ("▸ " if on else "  ") +
                ell(f"{c['title']}  ({c['date'][:4] or '?'})", w - 6),
                curses.color_pair(2) | curses.A_BOLD if on else curses.A_BOLD)
            me, sp, tg = H.describe_blocks(c["blocks"])
            shape = ("Schnitt identisch" if not (me or sp or tg) else
                     f"{me} zusammengefasst, {sp} aufgeteilt"
                     + (f", {tg} gemischt" if tg else ""))
            put(scr.s, y + 1, 4, ell(f"{c['ntracks_real']} Tracks · "
                                     f"{c['label'] or 'ohne Label'} · {shape} · "
                                     f"Abweichung {c['gap']:.1f}s", w - 6),
                curses.color_pair(5))
            put(scr.s, y + 2, 4, ell(f"z.B. „{c['sample']}“", w - 6),
                curses.color_pair(4))
        scr.s.refresh()
        k = scr.s.getch()
        if k in (ord("q"), K_ESC):
            raise Cancelled
        if k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = min(len(ok) - 1, sel + 1)
        elif k in (curses.KEY_ENTER, 10, 13):
            return ok[sel]


# ------------------------------------------------------------ IST / SOLL view

def confirm(scr, question, detail):
    while True:
        h, w = scr.frame("Bestätigen", "", "j = ja   n = nein")
        put(scr.s, 3, 2, question, curses.color_pair(3) | curses.A_BOLD)
        for i, d in enumerate(detail):
            put(scr.s, 5 + i, 4, ell(d, w - 6))
        put(scr.s, 6 + len(detail), 2, "Schreiben? [j/n]", curses.A_BOLD)
        scr.s.refresh()
        k = scr.s.getch()
        if k in (ord("j"), ord("y")):
            return True
        if k in (ord("n"), ord("q"), K_ESC):
            return False


def compare_view(scr, folder, files, pick, opt):
    """The main screen: current state left, target right."""
    off = 0
    while True:
        res = H.build_plan(files, pick["rel"], pick["tracks"], pick["blocks"], opt)
        rows = build_rows(files, res)
        meta = res["meta"]
        h, w = scr.frame(
            os.path.basename(folder),
            f"{meta['album']} · {meta['author']} · {meta['year'] or '?'} · "
            f"{meta['publisher'] or 'ohne Verlag'}",
            "↑↓/PgUp/PgDn blättern   r Neuschnitt   c andere Ausgabe   "
            "↵ übernehmen   q Ende")
        mid = w // 2
        put(scr.s, 2, 1, "IST", curses.A_BOLD | curses.color_pair(4))
        put(scr.s, 2, mid + 1, "SOLL", curses.A_BOLD | curses.color_pair(4))
        put(scr.s, 2, 8, f"{len(files)} Dateien, {human(sum(f['dur'] for f in files))}",
            curses.color_pair(5))
        put(scr.s, 2, mid + 8, f"{res['count']} Dateien"
            + ("  ⟲ neu geschnitten" if res["recut"] else ""),
            curses.color_pair(3) if res["recut"] else curses.color_pair(5))
        hline(scr.s, 3)
        body = h - 6
        maxoff = max(0, len(rows) - body)
        off = max(0, min(off, maxoff))
        for i in range(off, min(len(rows), off + body)):
            y = 4 + i - off
            left, right, kind = rows[i]
            put(scr.s, y, 1, ell(left, mid - 3),
                curses.color_pair(4) if not left else 0)
            put(scr.s, y, mid - 1, {"same": "→", "cut": "✂", "join": "⊕"}[kind],
                curses.color_pair(3) | curses.A_BOLD if kind != "same"
                else curses.color_pair(4))
            put(scr.s, y, mid + 1, ell(right, w - mid - 3))
        hline(scr.s, h - 2)
        me, sp, tg = H.describe_blocks(pick["blocks"])
        note = ("Schnitt stimmt überein" if not (me or sp or tg) else
                (f"{me} Track(s) stecken in einer Datei, {sp} Datei(en) teilen sich "
                 f"einen Track — [r] " +
                 ("Neuschnitt AN" if res["recut"] else "Neuschnitt aus")))
        put(scr.s, h - 2, 1, ell(note, w - 3),
            curses.color_pair(3) if res["recut"] else curses.color_pair(5))
        scr.s.refresh()

        k = scr.s.getch()
        if k in (ord("q"), K_ESC):
            raise Cancelled
        elif k in (curses.KEY_UP, ord("k")):
            off -= 1
        elif k in (curses.KEY_DOWN, ord("j")):
            off += 1
        elif k == curses.KEY_NPAGE:
            off += body
        elif k == curses.KEY_PPAGE:
            off -= body
        elif k == curses.KEY_HOME:
            off = 0
        elif k == curses.KEY_END:
            off = maxoff
        elif k == ord("r"):
            opt.recut = not opt.recut
            if opt.recut and not H.shutil.which("ffmpeg"):
                opt.recut = False
        elif k == ord("c"):
            return "change", None
        elif k in (curses.KEY_ENTER, 10, 13):
            detail = [f"Ordner:  {folder}",
                      f"Dateien: {len(files)} → {res['count']}",
                      f"Tags:    {meta['album']} / {meta['author']}"]
            if res["recut"]:
                detail.append("Neuschnitt: ja — Originale wandern nach _original/")
            if confirm(scr, "Änderungen jetzt schreiben?", detail):
                return "apply", res


def build_rows(files, res):
    """Zip current filenames against target filenames for the two columns."""
    rows = []
    if res["recut"]:
        seen = {}
        for op, nm in zip(res["ops"], res["names"]):
            src = "+".join(s["name"] for s in op["srcs"])
            if op["verbatim"]:
                rows.append((src, nm, "same"))
            elif len(op["srcs"]) > 1:
                rows.append((src, nm, "join"))
            else:
                n = seen.get(src, 0) + 1
                seen[src] = n
                end = "Ende" if op["end"] is None else f"{op['end']:.0f}s"
                rows.append((f"{src}  [{op['start']:.0f}s–{end}]", nm, "cut"))
        return rows
    for f, nm in zip(files, res["names"]):
        rows.append((f["name"], nm, "same" if f["name"] == nm else "same"))
    return rows


# --------------------------------------------------------------------- driver

def tui(stdscr, opt):
    curses.curs_set(0)
    curses.use_default_colors()
    for i, fg in enumerate([curses.COLOR_CYAN, curses.COLOR_YELLOW,
                            curses.COLOR_GREEN, curses.COLOR_BLUE,
                            curses.COLOR_MAGENTA], start=1):
        curses.init_pair(i, fg, -1)
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
    scr = Screen(stdscr)

    folder = pick_folder(scr, opt.folder)

    def analyse(report):
        report(f"lese {folder}")
        files = H.local_tracks(folder, lambda i, n: (
            report(f"Dateien gelesen: {i}/{n}") if i % 25 == 0 or i == n else None))
        report(f"{len(files)} Dateien, {sum(f['dur'] for f in files)/3600:.2f} h")
        q = H.re.sub(r"\s*[-–]\s*", " ", opt.query or os.path.basename(folder))
        if opt.mbid:
            raw = [{"mbid": opt.mbid, "title": "(vorgegeben)", "date": "",
                    "artist": "", "label": "", "ntracks": 0}]
        else:
            report(f"MusicBrainz: „{q}“")
            raw = H.search_releases(q, opt.limit)
            report(f"{len(raw)} Treffer, prüfe gegen die Laufzeiten")
        H.evaluate([f["dur"] for f in files], raw, opt.tol,
                   lambda i, n, t: report(f"[{i+1}/{n}] {t}"),
                   stop_after=0 if getattr(opt, "all_candidates", False) else 3)
        return files, raw

    files, cands = run_job(scr, "Analyse", analyse)
    if not any(c.get("blocks") for c in cands):
        h, w = scr.frame("Keine passende Ausgabe", "",
                         "beliebige Taste — dann im CLI --numbers-only versuchen")
        for i, c in enumerate(cands[:12]):
            put(scr.s, 3 + i, 2, ell(f"{c['title'][:34]:34s} {c.get('why','')}", w - 4))
        scr.s.refresh()
        scr.s.getch()
        return

    while True:
        pick = pick_candidate(scr, cands)
        action, res = compare_view(scr, folder, files, pick, opt)
        if action == "change":
            continue
        cover = None
        if not opt.no_cover:
            cover = (H.local_cover(folder, opt.cover)
                     or H.caa_cover(pick["rel"], folder))

        def work(report):
            def prog(phase, i, n):
                if i % 5 == 0 or i == n:
                    report(f"{phase} {i}/{n}")
            res["no_rename"] = opt.no_rename
            return H.apply_plan(folder, files, res, cover, prog)

        bp = run_job(scr, "Schreibe …", work)
        h, w = scr.frame("Fertig", "", "beliebige Taste")
        put(scr.s, 3, 2, f"✔ {res['count']} Dateien getaggt"
            + (" und neu geschnitten" if res["recut"] else ""),
            curses.color_pair(3) | curses.A_BOLD)
        put(scr.s, 5, 2, f"Alte Tags gesichert in {os.path.basename(bp)}",
            curses.color_pair(5))
        if res["recut"]:
            put(scr.s, 6, 2, "Originale liegen in _original/", curses.color_pair(5))
        scr.s.refresh()
        scr.s.getch()
        return


def run(opt):
    try:
        curses.wrapper(tui, opt)
    except Cancelled:
        print("Abgebrochen.")
    except KeyboardInterrupt:
        print("Abgebrochen.")
