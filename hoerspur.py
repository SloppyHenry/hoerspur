#!/usr/bin/env python3
"""hoerspur — identify an audiobook release via MusicBrainz by matching track durations,
then optionally re-cut, rename and ID3-tag the files consistently.

The match is done on the *time axis*, not on filenames: a rip is identified by
the sequence of its track lengths. That also lets us carry over titles from a
release whose track split differs from ours, and -- with --recut -- rebuild the
files so they follow the release's chapter boundaries."""

import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
import unicodedata, urllib.error, urllib.parse, urllib.request

UA = "hoerspur/1.0 ( https://github.com/local/hoerspur )"
MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac")
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")


def die(msg):
    print(f"Fehler: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"  ! {msg}")


# --------------------------------------------------------------------------- MB

_last_call = [0.0]


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(6):
        wait = 1.1 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                _last_call[0] = time.time()
                return json.load(r)
        except urllib.error.HTTPError as e:
            _last_call[0] = time.time()
            if e.code == 404:
                return None
            if e.code not in (500, 502, 503, 429) or attempt == 5:
                die(f"MusicBrainz-Fehler {e.code}: {e.reason}")
            # 503 means "slow down". Honour Retry-After when sent, else back
            # off exponentially -- short retries are useless once the rate
            # limiter has tripped.
            delay = float(e.headers.get("Retry-After") or 0) or min(60, 3 * 2 ** attempt)
            print(f"  MusicBrainz drosselt ({e.code}), warte {delay:.0f}s …")
            time.sleep(delay)
        except Exception as e:
            _last_call[0] = time.time()
            if attempt == 5:
                die(f"MusicBrainz nicht erreichbar: {e}")
            time.sleep(min(30, 3 * 2 ** attempt))


def mb(path, **params):
    params["fmt"] = "json"
    return http_json(f"{MB}/{path}?{urllib.parse.urlencode(params)}")


def search_releases(query, limit):
    res = mb("release", query=query, limit=limit) or {}
    out = []
    for r in res.get("releases", []):
        out.append({
            "mbid": r["id"],
            "title": r["title"],
            "date": r.get("date", "") or "",
            "artist": ", ".join(a["artist"]["name"] for a in r.get("artist-credit", [])
                                if isinstance(a, dict) and "artist" in a),
            "label": ", ".join(l["label"]["name"] for l in r.get("label-info", [])
                               if l.get("label")),
            "ntracks": sum(m.get("track-count", 0) for m in r.get("media", [])),
        })
    return out


def fetch_release(mbid):
    r = mb(f"release/{mbid}", inc="recordings+artist-credits+labels+release-groups")
    if r is None:
        die(f"MusicBrainz kennt die Release-ID nicht: {mbid}")
    return r


def release_tracks(rel):
    """[(disc, no, title, seconds|None)] in playing order."""
    out = []
    for m in rel.get("media", []):
        for t in m.get("tracks", []):
            ln = t.get("length")
            out.append((m.get("position", 1), t.get("position"), t["title"].strip(),
                        ln / 1000 if ln else None))
    return out


# ------------------------------------------------------------------- local files

def _cache_path():
    d = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = os.path.join(d, "hoerspur")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "durations.json")


def _load_cache():
    try:
        with open(_cache_path()) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_cache(c):
    try:
        # keep it from growing without bound
        if len(c) > 20000:
            c = dict(list(c.items())[-20000:])
        with open(_cache_path(), "w") as fh:
            json.dump(c, fh)
    except Exception:
        pass


def local_tracks(folder, progress=None):
    """Durations of every audio file, in playing order.

    Reading headers off a network mount is slow, so results are cached by
    (path, size, mtime) -- a dry run followed by --apply then costs one scan."""
    try:
        from mutagen import File as MFile
    except ImportError:
        die("mutagen fehlt. Installieren mit: pip install --user mutagen")
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith(AUDIO_EXT) and not f.startswith("."))
    if not files:
        die(f"Keine Audiodateien in {folder}")
    cache, dirty, out = _load_cache(), False, []
    for i, f in enumerate(files):
        # absolute: ffmpeg's concat demuxer resolves relative paths against the
        # list file, which lives in a temp dir
        p = os.path.join(os.path.abspath(folder), f)
        st = os.stat(p)
        key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
        dur = cache.get(key)
        if dur is None:
            a = MFile(p)
            if a is None or not getattr(a, "info", None):
                die(f"Nicht lesbar: {f}")
            dur = a.info.length
            cache[key] = dur
            dirty = True
        if progress:
            progress(i + 1, len(files))
        out.append({"path": p, "name": f, "dur": dur})
    if dirty:
        _save_cache(cache)
    return natural_order(out)


def natural_order(items):
    """Sort by the numbers embedded in filenames, tuple-wise.

    '1-2 Kapitel 10' -> (1,2,10). Beats lexical sort ('10' < '2') and handles
    the common disc-track prefix without special-casing it."""
    return sorted(items, key=lambda it: (tuple(int(n) for n in re.findall(r"\d+", it["name"])),
                                         it["name"].lower()))


# --------------------------------------------------------------------- alignment

def align(mine, theirs, tol):
    """Group local files and release tracks into synchronisation blocks.

    Both sides are the same recording, so their cut points fall at the same
    places -- but every encoded file carries a little encoder padding, so the
    two clocks drift apart by tens of milliseconds per file. Comparing absolute
    positions would accumulate that into seconds; comparing *block durations*
    does not, because each block restarts from its own origin.

    Walks both sides, extending whichever is behind until the two block
    durations agree within `tol`. Returns (blocks, worst_gap) where a block is
    (file_indices, track_indices) -- 1:1 for a clean match, m:1 where the rip
    split a chapter, 1:n where it merged several."""
    n, m = len(mine), len(theirs)
    if not n or not m:
        return None, "leer"
    if abs(sum(mine) - sum(theirs)) / max(sum(theirs), 1) > 0.02:
        d = abs(sum(mine) - sum(theirs)) / max(sum(theirs), 1) * 100
        return None, f"Gesamtlaufzeit weicht um {d:.1f}% ab"

    blocks, worst = [], 0.0
    i = j = 0
    while i < n and j < m:
        fi, tj = [i], [j]
        a, b = mine[i], theirs[j]
        guard = 0
        while abs(a - b) > tol:
            guard += 1
            if guard > n + m:
                return None, "Trackgrenzen lassen sich nicht zuordnen"
            if a < b:
                i += 1
                if i >= n:
                    return None, "Dateien enden zu früh"
                fi.append(i)
                a += mine[i]
            else:
                j += 1
                if j >= m:
                    return None, "Release-Tracks enden zu früh"
                tj.append(j)
                b += theirs[j]
        worst = max(worst, abs(a - b))
        blocks.append((fi, tj))
        i += 1
        j += 1
    if i != n or j != m:
        return None, "Reste am Ende ({} Dateien / {} Tracks)".format(n - i, m - j)
    return blocks, worst


def describe_blocks(blocks):
    merges = sum(1 for f, t in blocks if len(f) == 1 and len(t) > 1)
    splits = sum(1 for f, t in blocks if len(f) > 1 and len(t) == 1)
    tangled = sum(1 for f, t in blocks if len(f) > 1 and len(t) > 1)
    return merges, splits, tangled


def titles_for_files(blocks, titles, join, part_word):
    """One title per existing local file (no re-cutting)."""
    out = [None] * sum(len(f) for f, _ in blocks)
    for fi, tj in blocks:
        if len(fi) == 1:
            out[fi[0]] = join.join(titles[j] for j in tj)
        else:
            base = join.join(titles[j] for j in tj)
            for k, f in enumerate(fi, 1):
                out[f] = f"{base} ({part_word} {k})"
    return out


# ------------------------------------------------------------------------ recut

def ffmpeg(args):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y"] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"ffmpeg fehlgeschlagen:\n{r.stderr.strip()[:800]}")


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def plan_recut(blocks, files, tracks):
    """Per release track: which source files and which offsets within them.

    Cut offsets are taken relative to the block's own start and rescaled to the
    block's actual length, so encoder padding cannot push a cut off target."""
    ops = []
    for fi, tj in blocks:
        srcs = [files[f] for f in fi]
        our_len = sum(s["dur"] for s in srcs)
        rel_len = sum(tracks[j][3] for j in tj)
        scale = our_len / rel_len if rel_len else 1.0
        pos = 0.0
        for k, j in enumerate(tj):
            start = pos
            pos += tracks[j][3] * scale
            end = None if k == len(tj) - 1 else pos
            ops.append({"track": j, "srcs": srcs, "start": start, "end": end,
                        "verbatim": len(fi) == 1 and len(tj) == 1})
        ops[-1]["end"] = None
    return ops


def run_recut(ops, folder, names, workdir, progress=None):
    """Rebuild the audio so one output file == one release track."""
    outdir = os.path.join(folder, ".hoerspur-new")
    os.makedirs(outdir, exist_ok=True)
    made = []
    for idx, (op, name) in enumerate(zip(ops, names), 1):
        dst = os.path.join(outdir, name)
        if op["verbatim"]:
            shutil.copy2(op["srcs"][0]["path"], dst)
        else:
            if len(op["srcs"]) == 1:
                src = op["srcs"][0]["path"]
            else:
                lst = os.path.join(workdir, f"concat{idx}.txt")
                with open(lst, "w") as fh:
                    for s in op["srcs"]:
                        fh.write("file '%s'\n" % s["path"].replace("'", r"'\''"))
                src = os.path.join(workdir, f"joined{idx}.mp3")
                ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", src])
            args = ["-ss", f"{op['start']:.3f}"]
            if op["end"] is not None:
                args += ["-to", f"{op['end']:.3f}"]
            ffmpeg(args + ["-i", src, "-c", "copy", "-map", "0:a", dst])
            for tmp in (f"joined{idx}.mp3",):
                p = os.path.join(workdir, tmp)
                if os.path.exists(p):
                    os.remove(p)
        made.append(dst)
        if progress:
            progress("schneiden", idx, len(ops))
        elif idx % 10 == 0 or idx == len(ops):
            print(f"  … {idx}/{len(ops)}", flush=True)
    return outdir, made


# ------------------------------------------------------------------------ naming

def sanitize(s, maxlen=120):
    s = unicodedata.normalize("NFC", s)
    s = s.replace("/", "-").replace("\\", "-").replace(":", " -")
    s = s.replace('"', "'").replace("*", "").replace("?", "").replace("|", "-")
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return s[:maxlen].strip() or "Track"


def dedupe(names):
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            root, ext = os.path.splitext(n)
            n = f"{root} ({seen[n]}){ext}"
        else:
            seen[n] = 0
        out.append(n)
    return out


# ------------------------------------------------------------------------- cover

def local_cover(folder, given):
    for c in ([given] if given else [os.path.join(folder, n) for n in COVER_NAMES]):
        if c and os.path.isfile(c):
            mime = "image/png" if c.lower().endswith(".png") else "image/jpeg"
            with open(c, "rb") as fh:
                return mime, fh.read(), c
    return None


def caa_cover(rel, folder, save=True):
    """Front cover from the Cover Art Archive, trying the release then its group."""
    ids = [("release", rel["id"])]
    rg = rel.get("release-group", {}).get("id")
    if rg:
        ids.append(("release-group", rg))
    for kind, i in ids:
        url = f"{CAA}/{kind}/{i}/front"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        except Exception:
            continue
        if not data:
            continue
        path = os.path.join(folder, "cover" + (".png" if "png" in mime else ".jpg"))
        if save:
            with open(path, "wb") as fh:
                fh.write(data)
            print(f"  Cover geladen ({kind}, {len(data)} bytes) -> {path}")
        return mime, data, path
    return None


def pick_cover(args, folder, rel):
    if args.no_cover:
        return None
    have = local_cover(folder, args.cover)
    if args.fetch_cover and rel is not None:
        got = caa_cover(rel, folder)
        if got:
            return got
        warn("kein Cover im Cover Art Archive, nehme lokales")
    if have:
        print(f"  Cover: {os.path.basename(have[2])} ({len(have[1])} bytes)")
        return have
    if rel is not None:
        print("  kein lokales Cover, versuche Cover Art Archive …")
        got = caa_cover(rel, folder)
        if got:
            return got
        warn("auch dort keines gefunden")
    return None


# --------------------------------------------------------------------------- tag

def write_tags(items, meta, cover, total, progress=None):
    from mutagen.id3 import (ID3, ID3NoHeaderError, TIT2, TALB, TPE1, TPE2, TPE3,
                             TCOM, TPOS, TRCK, TCON, TDRC, TPUB, TLAN, TSOA, TXXX,
                             APIC)
    backup = {}
    for idx, it in enumerate(items, 1):
        try:
            tag = ID3(it["path"])
        except ID3NoHeaderError:
            tag = ID3()
        backup[os.path.basename(it["path"])] = {
            k: str(v) for k, v in tag.items() if not k.startswith("APIC")}
        tag.clear()
        tag.add(TIT2(encoding=3, text=it["title"]))
        tag.add(TALB(encoding=3, text=meta["album"]))
        tag.add(TSOA(encoding=3, text=meta["album"]))
        tag.add(TPE1(encoding=3, text=meta["author"]))
        tag.add(TPE2(encoding=3, text=meta["author"]))
        tag.add(TRCK(encoding=3, text=f"{it['n']}/{total}"))
        if it.get("disc"):
            tag.add(TPOS(encoding=3, text=f"{it['disc']}/{meta['discs']}"))
        tag.add(TCON(encoding=3, text=meta["genre"]))
        tag.add(TLAN(encoding=3, text=meta["lang"]))
        if meta.get("narrator"):
            tag.add(TPE3(encoding=3, text=meta["narrator"]))
            tag.add(TCOM(encoding=3, text=meta["narrator"]))
            tag.add(TXXX(encoding=3, desc="NARRATEDBY", text=meta["narrator"]))
        if meta.get("year"):
            tag.add(TDRC(encoding=3, text=meta["year"]))
        if meta.get("publisher"):
            tag.add(TPUB(encoding=3, text=meta["publisher"]))
        tag.add(TXXX(encoding=3, desc="AUTHOR", text=meta["author"]))
        if cover:
            tag.add(APIC(encoding=3, mime=cover[0], type=3, desc="Cover", data=cover[1]))
        tag.save(it["path"], v2_version=3, v1=0)
        if progress:
            progress("taggen", idx, len(items))
        elif idx % 10 == 0 or idx == len(items):
            print(f"  … {idx}/{len(items)}", flush=True)
    return backup


def existing_discs(files):
    from mutagen import File as MFile
    out = []
    for f in files:
        try:
            a = MFile(f["path"])
            v = a.tags.get("TPOS") if a and a.tags else None
            out.append(int(str(v).split("/")[0]) if v else 1)
        except Exception:
            out.append(1)
    return out if any(d != 1 for d in out) else None



# ------------------------------------------------------------------ engine API


def evaluate(durs, raw, tol, progress=None, stop_after=0):
    """Fetch each candidate release and test it against our track lengths.

    Annotates the candidate dicts in place; `progress(i, n, title)` runs before
    each fetch so a UI can show what is happening. With `stop_after` we give up
    once that many candidates match -- MusicBrainz allows one request a second,
    so scanning every hit is the slowest part of a run."""
    hits = 0
    for k, c in enumerate(raw):
        if stop_after and hits >= stop_after:
            c["why"] = "nicht geprüft"
            continue
        if progress:
            progress(k, len(raw), c["title"])
        full = fetch_release(c["mbid"])
        trs = release_tracks(full)
        c["ntracks_real"] = len(trs)
        if not trs or any(t[3] is None for t in trs):
            c["why"] = "keine Laufzeiten hinterlegt"
            continue
        b, info = align(durs, [t[3] for t in trs], tol)
        if b is None:
            c["why"] = info
        else:
            c.update(rel=full, tracks=trs, blocks=b, gap=info,
                     why=f"passt (Abweichung {info:.1f}s)", sample=trs[0][2][:46])
            hits += 1
    return raw


def build_plan(files, rel, tracks, blocks, opt):
    """Work out the target state: how many files, their names, tags and cuts.

    `opt` is the argparse namespace (or anything with the same attributes), so
    CLI and TUI derive the target from exactly the same code."""
    recut = bool(getattr(opt, "recut", False)) and blocks is not None and any(
        len(f) != 1 or len(t) != 1 for f, t in blocks)

    if recut:
        ops = plan_recut(blocks, files, tracks)
        titles = [tracks[o["track"]][2] for o in ops]
        discs = [tracks[o["track"]][0] for o in ops]
        count = len(ops)
    else:
        ops = None
        titles = (titles_for_files(blocks, [t[2] for t in tracks],
                                   opt.join, opt.part_word) if blocks else None)
        if blocks:
            discs = [None] * len(files)
            for fi, tj in blocks:
                for f in fi:
                    discs[f] = tracks[tj[0]][0]
        else:
            discs = existing_discs(files) or [1] * len(files)
        count = len(files)

    width = max(2, len(str(count)))
    seen, per_disc = {}, []
    for d in discs:
        seen[d] = seen.get(d, 0) + 1
        per_disc.append(seen[d])

    names, plan = [], []
    for i in range(count):
        num = f"{discs[i]}-{per_disc[i]:02d}" if opt.per_disc else f"{i+1:0{width}d}"
        body = titles[i] if titles else f"Kapitel {i+1:0{width}d}"
        title = sanitize(body if opt.no_number else f"{num} - {body}")
        plan.append({"n": i + 1, "disc": discs[i], "title": title})
        names.append(title + ".mp3")
    names = dedupe(names)

    credit = rel.get("artist-credit", []) if rel else []
    folder = os.path.dirname(files[0]["path"]) if files else ""
    meta = {
        "album": opt.album or (rel["title"] if rel else os.path.basename(folder)),
        "author": opt.author or (", ".join(a["artist"]["name"] for a in credit
                                           if isinstance(a, dict) and "artist" in a)
                                 if rel else ""),
        "year": opt.year or ((rel.get("date") or "")[:4] if rel else None) or None,
        "publisher": opt.publisher or (next((l["label"]["name"]
                                             for l in rel.get("label-info", [])
                                             if l.get("label")), None) if rel else None),
        "narrator": opt.narrator,
        "genre": opt.genre, "lang": opt.lang,
        "discs": max([d for d in discs if d] or [1]),
    }
    return {"recut": recut, "ops": ops, "titles": titles, "discs": discs,
            "count": count, "names": names, "plan": plan, "meta": meta,
            "no_rename": getattr(opt, "no_rename", False)}


def apply_plan(folder, files, res, cover, progress=None):
    """Execute a plan: re-cut if asked, then tag, then rename."""
    names, plan, meta, count = res["names"], res["plan"], res["meta"], res["count"]
    if res["recut"]:
        total_in = sum(f["dur"] for f in files)
        with tempfile.TemporaryDirectory(prefix="hoerspur-") as wd:
            outdir, made = run_recut(res["ops"], folder, names, wd, progress)
        total_out = sum(probe_duration(p) for p in made)
        if abs(total_out - total_in) > 5:
            raise RuntimeError(
                f"Laufzeit nach dem Schnitt weicht ab ({total_in:.0f}s -> "
                f"{total_out:.0f}s). Neue Dateien liegen unangetastet in {outdir}, "
                f"die Originale sind unberührt.")
        orig = os.path.join(folder, "_original")
        os.makedirs(orig, exist_ok=True)
        for f in files:
            shutil.move(f["path"], os.path.join(orig, f["name"]))
        for p in made:
            shutil.move(p, os.path.join(folder, os.path.basename(p)))
        os.rmdir(outdir)
        items = [{"path": os.path.join(folder, nm), **p} for nm, p in zip(names, plan)]
    else:
        items = [{"path": f["path"], **p} for f, p in zip(files, plan)]

    backup = write_tags(items, meta, cover, count, progress)
    if not res["recut"] and not res.get("no_rename"):
        for it, nm in zip(items, names):
            dst = os.path.join(folder, nm)
            if it["path"] != dst:
                os.rename(it["path"], dst)
    bp = os.path.join(folder, ".hoerspur-backup.json")
    with open(bp, "w") as fh:
        json.dump(backup, fh, ensure_ascii=False, indent=1)
    return bp


# ---------------------------------------------------------------------- choosing

def choose(cands, interactive):
    usable = [c for c in cands if c.get("blocks")]
    if not usable:
        return None
    if len(usable) == 1 or not interactive:
        return usable[0]
    print("\nMehrere Ausgaben passen zur Laufzeit:\n")
    for k, c in enumerate(usable, 1):
        me, sp, tg = describe_blocks(c["blocks"])
        shape = ("exakt gleicher Schnitt" if not (me or sp or tg)
                 else f"{me} zusammengefasst, {sp} aufgeteilt"
                      + (f", {tg} gemischt" if tg else ""))
        print(f"  [{k}] {c['title']}  ({c['date'][:4] or '?'})  "
              f"{c['ntracks_real']} Tracks  {c['label'] or '—'}")
        print(f"      {shape}; Abweichung {c['gap']:.1f}s; {c['mbid']}")
        print(f"      z.B. {c['sample']}")
    while True:
        try:
            a = input(f"\nAuswahl [1-{len(usable)}, Enter = 1, q = abbrechen]: ").strip()
        except EOFError:
            return usable[0]
        if a.lower() == "q":
            sys.exit(0)
        if not a:
            return usable[0]
        if a.isdigit() and 1 <= int(a) <= len(usable):
            return usable[int(a) - 1]


# -------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        prog="hoerspur",
        description="Hörbuch-Ordner über MusicBrainz identifizieren, "
                    "optional neu schneiden, umbenennen und taggen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Beispiele:
  hoerspur .                        Vorschau (Dry-Run)
  hoerspur . --tui                  Oberfläche im Terminal
  hoerspur . --list                 nur Kandidaten mit Match-Qualität
  hoerspur . --apply                umbenennen + taggen
  hoerspur . --recut --apply        an den Kapitelgrenzen neu schneiden
  hoerspur . --mbid <ID> --apply    Ausgabe festnageln
  hoerspur . --numbers-only --apply ohne Titel, nur durchnummerieren
""")
    ap.add_argument("folder", nargs="?", default=".")
    ap.add_argument("-q", "--query", help="Suchbegriff (Default: Ordnername)")
    ap.add_argument("--mbid", help="MusicBrainz-Release-ID direkt vorgeben")
    ap.add_argument("--tui", action="store_true", help="Oberfläche im Terminal")
    ap.add_argument("--list", action="store_true", help="nur Kandidaten auflisten")
    ap.add_argument("--apply", action="store_true", help="schreiben (sonst Dry-Run)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="nicht nachfragen, besten Treffer nehmen")
    ap.add_argument("--limit", type=int, default=15,
                    help="max. Suchtreffer (Default 15)")
    ap.add_argument("--all-candidates", action="store_true",
                    help="alle Treffer prüfen statt nach 3 Volltreffern aufzuhören")
    ap.add_argument("--tol", type=float, default=2.0,
                    help="erlaubte Abweichung je Block in Sekunden (Default 2)")
    ap.add_argument("--recut", action="store_true",
                    help="Dateien an den Kapitelgrenzen neu schneiden (braucht ffmpeg)")
    ap.add_argument("--author"); ap.add_argument("--album")
    ap.add_argument("--narrator", help="Sprecher (MusicBrainz kennt sie selten)")
    ap.add_argument("--year"); ap.add_argument("--publisher")
    ap.add_argument("--genre", default="Hörbuch")
    ap.add_argument("--lang", default="deu")
    ap.add_argument("--cover", help="Cover-Datei (Default: cover.jpg im Ordner)")
    ap.add_argument("--no-cover", action="store_true")
    ap.add_argument("--fetch-cover", action="store_true",
                    help="Cover immer neu vom Cover Art Archive laden")
    ap.add_argument("--numbers-only", action="store_true",
                    help="keine MB-Titel, nur 'Kapitel NNN'")
    ap.add_argument("--no-number", action="store_true")
    ap.add_argument("--per-disc", action="store_true",
                    help="pro CD nummerieren (D-TT) statt durchlaufend")
    ap.add_argument("--no-rename", action="store_true")
    ap.add_argument("--join", default=" / ")
    ap.add_argument("--part-word", default="Teil")
    args = ap.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        die(f"Kein Ordner: {folder}")

    if args.tui:
        if not sys.stdout.isatty():
            die("--tui braucht ein Terminal")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import hoerspur_tui
        args.folder = folder
        return hoerspur_tui.run(args)

    interactive = sys.stdin.isatty() and not args.yes
    files = local_tracks(folder)
    durs = [f["dur"] for f in files]
    print(f"{len(files)} Dateien, {sum(durs)/3600:.2f} h Gesamtlaufzeit\n")

    rel = tracks = blocks = None
    if not args.numbers_only:
        if args.mbid:
            raw = [{"mbid": args.mbid, "title": "(vorgegeben)", "date": "",
                    "artist": "", "label": "", "ntracks": 0}]
        else:
            q = re.sub(r"\s*[-–]\s*", " ", args.query or os.path.basename(folder))
            print(f"Suche MusicBrainz nach: {q}")
            raw = search_releases(q, args.limit)
            if not raw:
                die("Keine Treffer. Mit -q anderen Suchbegriff versuchen "
                    "oder --numbers-only nehmen.")

        print("\nPrüfe Kandidaten gegen die Laufzeiten …")
        evaluate(durs, raw, args.tol,
                 stop_after=0 if (args.list or args.all_candidates) else 3)
        print()
        for c in raw:
            mark = "*" if c.get("blocks") else " "
            print(f" {mark} {c['mbid']}  {c['title'][:36]:36s} "
                  f"{(c['date'][:4] or '?'):5s} {c.get('ntracks_real', 0):4d} Tr  "
                  f"{c['label'][:20]:20s} {c['why']}")
        if args.list:
            return

        pick = choose(raw, interactive)
        if pick is None:
            die("Kein Release passt zur Laufzeit. Mit --mbid erzwingen, --tol "
                "erhöhen oder --numbers-only verwenden.")
        rel, tracks, blocks = pick["rel"], pick["tracks"], pick["blocks"]
        me, sp, tg = describe_blocks(blocks)
        print(f"\nGewählt: {rel['title']} ({(rel.get('date') or '?')[:4]})  {rel['id']}")
        if me or sp or tg:
            print(f"Schnitt weicht ab: {me} Release-Track(s) in einer Datei, "
                  f"{sp} Datei(en) teilen sich einen Track"
                  + (f", {tg} gemischt" if tg else ""))
            if not args.recut:
                print("  -> ohne --recut behalten die Dateien ihren Schnitt, "
                      "Titel werden zusammengefasst bzw. als 'Teil n' vergeben")
        else:
            print("Schnitt stimmt exakt überein.")

    res = build_plan(files, rel, tracks, blocks, args)
    recut, ops, count = res["recut"], res["ops"], res["count"]
    names, plan, meta = res["names"], res["plan"], res["meta"]

    if args.recut and not recut:
        print("--recut: nichts zu tun, der Schnitt stimmt bereits.")
    if recut:
        if not shutil.which("ffmpeg"):
            die("--recut braucht ffmpeg (sudo apt install ffmpeg)")
        exts = {os.path.splitext(f["name"])[1].lower() for f in files}
        if exts != {".mp3"}:
            die(f"--recut ist bisher nur für MP3 gebaut (gefunden: {sorted(exts)})")
    if not meta["author"]:
        die("Kein Autor ermittelbar — bitte --author setzen.")

    print()
    if recut:
        for op, nm in zip(ops, names):
            if op["verbatim"]:
                print(f"  {op['srcs'][0]['name'][:46]:46s} ->  {nm}")
            else:
                span = ("+".join(s["name"] for s in op["srcs"]))[:46]
                end = f"{op['end']:.0f}" if op["end"] is not None else "Ende"
                print(f"  {span:46s} -> [{op['start']:.0f}s..{end}] {nm}")
        print(f"\n{len(files)} Dateien -> {count} Dateien "
              f"(Originale wandern nach _original/)")
    else:
        for f, nm, p in zip(files, names, plan):
            arrow = "==" if nm == f["name"] else "->"
            print(f"  CD{p['disc'] or 1:<2d} {f['name'][:44]:44s} {arrow} {nm}")

    print()
    cover = pick_cover(args, folder, rel)
    print(f"\nAlbum: {meta['album']} | Autor: {meta['author']} | Jahr: {meta['year']} "
          f"| Verlag: {meta['publisher']} | Sprecher: {meta['narrator'] or '—'}")

    if not args.apply:
        print("\nDry-Run. Mit --apply ausführen.")
        return

    if recut:
        print(f"\nSchneide {count} Dateien neu …")
    else:
        print(f"\nSchreibe Tags ({count} Dateien) …")

    def prog(phase, i, n):
        if i % 10 == 0 or i == n:
            print(f"  … {phase} {i}/{n}", flush=True)

    try:
        bp = apply_plan(folder, files, res, cover, prog)
    except RuntimeError as e:
        die(str(e))
    print(f"Fertig. Alte Tags gesichert in {bp}")


if __name__ == "__main__":
    main()
