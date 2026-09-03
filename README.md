# hoerspur

Erkennt Hörbücher an ihrer **Zeitachse**, nicht an ihren Dateinamen — und benennt,
taggt und schneidet sie danach so, wie die Ausgabe offiziell veröffentlicht wurde.

```
183 Dateien, 12.08 h Gesamtlaufzeit

Suche MusicBrainz nach: Der Schwarm Frank Schätzing
Prüfe Kandidaten gegen die Laufzeiten …

 * 6c7549c6-…  Der Schwarm       2004  184 Tr  Der Hörverlag  passt (Abweichung 1.8s)
 * ac578ecc-…  Der Schwarm       2003  183 Tr                 passt (Abweichung 0.9s)
   bcfdda32-…  Der Schwarm       2014  373 Tr  Der Hörverlag  Laufzeit weicht um 68.3% ab
   e9a3fc1f-…  Tod und Teufel    2008  104 Tr  Der Hörverlag  Laufzeit weicht um 44.8% ab
```

## Warum die Zeitachse

Hörbuch-Rips haben beliebige Dateinamen — `1-01 Kapitel 1.mp3`,
`Schaetzing - Kapitel 093.mp3`, `track07.mp3`, alles im selben Ordner. Danach zu
suchen ist aussichtslos.

Die Folge der Tracklängen dagegen ist ein Fingerabdruck. hoerspur liest die
Laufzeiten aus, fragt MusicBrainz nach Kandidaten und vergleicht. Eine falsche
Ausgabe fällt sofort raus, auch wenn sie genauso heißt.

## Unterschiedlicher Schnitt

Dieselbe Aufnahme wird von Ausgabe zu Ausgabe anders in Tracks zerlegt. Deine
Datei kann zweieinhalb Kapitel enthalten, das angebrochene Kapitel läuft in der
nächsten Datei weiter. hoerspur ordnet trotzdem zu, indem es beide Seiten in
**Synchronisationsblöcke** gruppiert: es verlängert jeweils die Seite, die
zurückliegt, bis die Blockdauern übereinstimmen.

| Block | Bedeutung | ohne `--recut` | mit `--recut` |
|---|---|---|---|
| `[1 Datei] → [1 Track]` | Schnitt identisch | Titel übernehmen | unverändert kopieren |
| `[1 Datei] → [n Tracks]` | Datei enthält mehrere Kapitel | Titel verbinden (`A / B`) | Datei zerschneiden |
| `[n Dateien] → [1 Track]` | Kapitel über mehrere Dateien | `Titel (Teil 1)`, `(Teil 2)` | Dateien zusammenführen |
| `[n Dateien] → [m Tracks]` | beides vermischt | Mischform | zusammenführen, neu schneiden |

Verglichen werden **Blockdauern, nie absolute Positionen.** Jede kodierte Datei
trägt etwas Encoder-Padding, wodurch beide Zeitachsen um einige Zehntel-
Millisekunden pro Datei auseinanderlaufen. Über 183 Dateien summiert sich das auf
zwölf Sekunden — absolut gerechnet würde man an der falschen Stelle schneiden.
Blockweise setzt der Fehler an jedem Treffpunkt zurück und bleibt im
Millisekundenbereich:

```
Drift akkumuliert nicht — größte Blockabweichung 0.050s (absolut wären es 10s)
```

## Installation

```bash
git clone git@github.com:SloppyHenry/hoerspur.git
cd hoerspur && ./install.sh
```

Voraussetzungen: Python 3.8+, [mutagen](https://mutagen.readthedocs.io)
(`pip install --user mutagen`). `ffmpeg` nur für `--recut`.

## Benutzung

```bash
hoerspur .                        # Vorschau — schreibt nichts
hoerspur . --tui                  # Oberfläche im Terminal
hoerspur . --apply                # umbenennen + taggen
hoerspur . --recut --apply        # zusätzlich an den Kapitelgrenzen neu schneiden
```

**Ohne `--apply` wird nie geschrieben.** Der Dry-Run zeigt jede Umbenennung, jeden
Schnitt und alle Tags.

### Oberfläche

`--tui` führt durch Ordnerauswahl → Analyse → Kandidatenliste → IST/SOLL-Vergleich
→ Bestätigen. Neuschnitt lässt sich mit `r` zuschalten, `c` wählt eine andere
Ausgabe.

```
 hoerspur  Der Schwarm - Frank Schätzing
 Der Schwarm · Frank Schätzing · 2004 · Der Hörverlag
 IST      183 Dateien, 12:04:54     SOLL     184 Dateien  ⟲ neu geschnitten
 ────────────────────────────────────────────────────────────────────────────
 125 Bormann sah ….mp3            →  125 - Bormann sah Schemenhaft ….mp3
 126 Dicht vor ihm ….mp3 [0s–177s] ✂  126 - Dicht vor ihm drehte die ….mp3
 126 Dicht vor ihm ….mp3 [177s–Ende] ✂ 127 - Eines der Tiere drehte ….mp3
 ────────────────────────────────────────────────────────────────────────────
 1 Track(s) stecken in einer Datei — [r] Neuschnitt AN
```

### Wichtige Optionen

| Option | Wirkung |
|---|---|
| `--list` | nur Kandidaten mit Match-Qualität, ändert nichts |
| `--mbid <ID>` | Ausgabe festnageln statt suchen |
| `-q "…"` | eigener Suchbegriff (Default: Ordnername) |
| `--numbers-only` | keine Titel übernehmen, nur `Kapitel NNN` |
| `--per-disc` | pro CD nummerieren (`1-01`) statt durchlaufend |
| `--narrator "…"` | Sprecher setzen — MusicBrainz kennt sie selten |
| `--fetch-cover` | Cover neu vom Cover Art Archive laden |
| `--tol <s>` | erlaubte Blockabweichung (Default 2 s) |
| `--all-candidates` | alle Treffer prüfen statt nach 3 Volltreffern aufzuhören |

## Cover

Reihenfolge: lokales `cover.jpg` → sonst automatisch
[Cover Art Archive](https://coverartarchive.org) (erst Release, dann
Release-Group). `--fetch-cover` erzwingt den Download, `--no-cover` schaltet ab.
Das Bild wird als APIC-Frame in jede Datei eingebettet.

## Was geschrieben wird

`TIT2` `TALB` `TSOA` `TPE1` `TPE2` `TPE3` `TCOM` `TRCK` `TPOS` `TCON` `TDRC`
`TPUB` `TLAN` `APIC`, dazu `TXXX:AUTHOR` und `TXXX:NARRATEDBY`. ID3v2.3, weil
mehr Player damit umgehen können; ID3v1 wird entfernt.

## Sicherheitsnetze

- Ohne `--apply` wird nichts geschrieben.
- Die alten Tags landen vor dem Überschreiben in `.hoerspur-backup.json`.
- Beim Neuschnitt wird die Gesamtlaufzeit gegengeprüft. Weicht sie um mehr als
  5 s ab, bricht hoerspur ab, ohne die Originale anzufassen.
- Originale werden nie gelöscht, sondern nach `_original/` verschoben.
- Geschnitten wird mit `ffmpeg -c copy` — verlustfrei, kein Neukodieren.

## Grenzen

- **MusicBrainz kennt nicht jedes Hörbuch.** Bei Nischentiteln bleibt
  `--numbers-only`.
- **Die Titelqualität schwankt**, weil sie von Freiwilligen eingetragen wird.
  Tippfehler und abgeschnittene Titel kommen vor. hoerspur übernimmt sie
  unverändert und korrigiert nichts still.
- **`--recut` kann bisher nur MP3.**
- MP3-Schnitte liegen auf Frame-Grenzen (~26 ms). Durch das Bit-Reservoir kann
  an der Nahtstelle ein sehr kurzer Übergang hörbar sein.
- Ein Rip mit fehlenden oder doppelten Dateien wird abgelehnt, nicht repariert.

## Tests

```bash
python3 tests/test_hoerspur.py
```

Erzeugt echte MP3s, in denen jede Quelldatei ihren eigenen Ton hat, schneidet sie
neu und prüft per Frequenzanalyse nach, dass das Audio an der richtigen Stelle
gelandet ist.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
