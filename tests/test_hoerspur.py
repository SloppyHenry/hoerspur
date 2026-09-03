#!/usr/bin/env python3
"""Tests für hoerspur. Braucht ffmpeg (erzeugt echte MP3s) und mutagen.

    python3 tests/test_hoerspur.py
"""
import argparse, os, subprocess, sys, tempfile, shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hoerspur as H

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def make_mp3(path, seconds, hz):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={hz}:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "64k", path], check=True)


def dominant_hz(path, frac):
    """Loudest frequency at a point in the file, as a content fingerprint."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path,
                          "-f", "f32le", "-ac", "1", "-ar", "8000", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    seg = a[int(len(a) * frac):int(len(a) * frac) + 4096]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return float(np.fft.rfftfreq(len(seg), 1 / 8000)[sp.argmax()])


# --------------------------------------------------------------------- units

def test_align():
    print("align(): Blockbildung")
    # identical split
    b, gap = H.align([10, 20, 30], [10, 20, 30], 2.0)
    check("1:1", b == [([0], [0]), ([1], [1]), ([2], [2])])

    # our file holds two release tracks (rip merged them)
    b, _ = H.align([30, 45], [30, 20, 25], 2.0)
    check("Datei enthält 2 Kapitel", b == [([0], [0]), ([1], [1, 2])])

    # a release track spans two of our files (rip split it)
    b, _ = H.align([20, 25], [30, 15], 2.0)
    check("Kapitel über 2 Dateien", b == [([0, 1], [0, 1])], str(b))

    # drift must not accumulate: 200 files each 50ms longer than the CD track
    n = 200
    b, gap = H.align([60.05] * n, [60.0] * n, 2.0)
    check("Drift akkumuliert nicht", b is not None and len(b) == n,
          f"größte Blockabweichung {gap:.3f}s (absolut wären es {n*0.05:.0f}s)")

    # a wholly different recording must be rejected
    b, why = H.align([10, 10], [600, 600], 2.0)
    check("falsche Ausgabe wird abgelehnt", b is None, why)


def test_naming():
    print("Namensbildung")
    check("Schrägstrich ersetzt", "/" not in H.sanitize("Kapitel 1/2"))
    check("Doppelpunkt ersetzt", H.sanitize("Um 10:30 Uhr") == "Um 10 -30 Uhr")
    check("Duplikate eindeutig",
          H.dedupe(["a.mp3", "a.mp3", "a.mp3"]) == ["a.mp3", "a (1).mp3", "a (2).mp3"])
    check("natürliche Sortierung",
          [f["name"] for f in H.natural_order(
              [{"name": n} for n in ["1-10 x.mp3", "1-2 x.mp3", "10-1 x.mp3"]])]
          == ["1-2 x.mp3", "1-10 x.mp3", "10-1 x.mp3"])


# ---------------------------------------------------------------- end to end

def test_recut():
    print("Neuschnitt (end-to-end, mit echten MP3s)")
    tmp = tempfile.mkdtemp(prefix="hoerspur-test-")
    try:
        # five files; each gets its own tone so we can prove where the audio went
        spec = [(30, 200), (45, 300), (20, 400), (25, 500), (60, 600)]
        for i, (d, hz) in enumerate(spec, 1):
            make_mp3(os.path.join(tmp, f"src{i}.mp3"), d, hz)

        # a release cut differently: 1:1 | one file -> two tracks |
        # two files -> one track | one file -> two tracks
        rel = [30.0, 20.0, 25.0, 45.0, 25.0, 35.0]
        tracks = [(1, i + 1, f"Kapitel {i+1}", d) for i, d in enumerate(rel)]

        files = H.local_tracks(tmp)
        blocks, gap = H.align([f["dur"] for f in files], rel, 2.0)
        check("Blöcke erkannt",
              blocks == [([0], [0]), ([1], [1, 2]), ([2, 3], [3]), ([4], [4, 5])],
              str(blocks))

        opt = argparse.Namespace(
            recut=True, join=" / ", part_word="Teil", per_disc=False,
            no_number=False, album=None, author=None, year=None, publisher=None,
            narrator="Testsprecher", genre="Hörbuch", lang="deu", no_rename=False)
        fake = {"id": "x", "title": "Testbuch", "date": "2001",
                "artist-credit": [{"artist": {"name": "Test Autor"}}],
                "label-info": [{"label": {"name": "Testverlag"}}]}
        res = H.build_plan(files, fake, tracks, blocks, opt)
        check("5 Dateien -> 6", res["count"] == 6)

        H.apply_plan(tmp, files, res, None)
        now = H.local_tracks(tmp)
        check("6 Dateien entstanden", len(now) == 6, str(len(now)))
        for f, want in zip(now, rel):
            check(f"  {f['name']} = {want}s", abs(f["dur"] - want) < 0.6,
                  f"{f['dur']:.2f}s")
        check("Gesamtlaufzeit erhalten",
              abs(sum(f["dur"] for f in now) - sum(d for d, _ in spec)) < 1.0)
        check("Originale nicht gelöscht",
              len(os.listdir(os.path.join(tmp, "_original"))) == 5)

        try:
            import numpy  # noqa: F401
            got = [round(dominant_hz(f["path"], 0.5)) for f in now]
            # track 4 is src3+src4 joined, so its middle sits in the 500 Hz half
            want = [200, 300, 300, 500, 600, 600]
            # the FFT bin is ~2 Hz wide, so compare with a tolerance
            check("Audio landet an der richtigen Stelle",
                  all(abs(g - w) <= 5 for g, w in zip(got, want)),
                  f"gemessen {got}, erwartet {want}")
        except ImportError:
            print("  --    Frequenzprüfung übersprungen (numpy fehlt)")

        from mutagen.id3 import ID3
        t = ID3(now[3]["path"])
        check("Tags geschrieben",
              str(t["TIT2"]) == "04 - Kapitel 4" and str(t["TRCK"]) == "4/6"
              and str(t["TPE3"]) == "Testsprecher", str(t["TIT2"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"{tool} fehlt — Tests brauchen ffmpeg")
            return 1
    test_align()
    test_naming()
    test_recut()
    print()
    if FAILED:
        print(f"{len(FAILED)} Test(s) fehlgeschlagen: {', '.join(FAILED)}")
        return 1
    print("alle Tests bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
