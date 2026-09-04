# hoerspur

Erkennt Hörbücher an ihrer **Zeitachse**, nicht an ihren Dateinamen — und benennt,
taggt und schneidet sie danach so, wie die Ausgabe offiziell veröffentlicht wurde.

Dieses Projekt arbeitet exemplarisch mit beliebigen Hörbuch-Rips. In den
Beispielen in dieser Anleitung wird ein generischer "Beispiel-Titel" verwendet
statt konkreter Veröffentlichungen.

```
183 Dateien, 12.08 h Gesamtlaufzeit

Suche MusicBrainz nach: Beispiel-Titel Autor
Prüfe Kandidaten gegen die Laufzeiten …

 * 6c7549c6-…  Beispiel-Titel    2004  184 Tr  Verlag  passt (Abweichung 1.8s)
 * ac578ecc-…  Beispiel-Titel    2003  183 Tr          passt (Abweichung 0.9s)
   bcfdda32-…  Anderes Werk      2014  373 Tr  Verlag  Laufzeit weicht um 68.3% ab
```

## Kurzüberblick

hoerspur extrahiert die Laufzeiten aller Dateien in einem Ordner, fragt
öffentlich verfügbare Metadatenquellen (z. B. MusicBrainz) nach möglichen
Ausgaben und vergleicht die Folge der Trackdauern als Fingerabdruck. Das macht
hoerspur robust gegenüber beliebigen Dateinamen und verschiedenen Schnitten der
gleichen Aufnahme.

## Kernkonzepte

- Zeitachsen-Fingerabdruck: Die Folge der Tracklängen ist der Vergleichsmaßstab.
- Synchronisationsblöcke: Dateien werden in logische Blöcke gruppiert und
  blockweise verglichen, nicht über absolute Positionen.
- Dry-run-first: Ohne `--apply` schreibt hoerspur niemals Dateien oder Tags.

## Installation

```bash
git clone git@github.com:SloppyHenry/hoerspur.git
cd hoerspur && ./install.sh
```

Voraussetzungen: Python 3.8+, [mutagen](https://mutagen.readthedocs.io)
(`pip install --user mutagen`). `ffmpeg` wird nur für `--recut` benötigt.

## Schnelle Nutzung

```bash
hoerspur .                        # Vorschau — schreibt nichts
hoerspur . --tui                  # Oberfläche im Terminal
hoerspur . --apply                # umbenennen + taggen
hoerspur . --recut --apply        # zusätzlich an Kapitelgrenzen neu schneiden
```

### Hinweise

- `--tui` führt interaktiv durch Analyse und Auswahl der Kandidaten.
- `--recut` schneidet nur MP3-Dateien verlustfrei mit `ffmpeg -c copy`.
- Ohne `--apply` bleibt alles im Dry-Run-Modus.

## Optionen (Auszug)

| Option | Wirkung |
|---|---|
| `--list` | nur Kandidaten mit Match-Qualität, ändert nichts |
| `--mbid <ID>` | Ausgabe festlegen statt suchen |
| `-q "…"` | eigener Suchbegriff (Default: Ordnername) |
| `--numbers-only` | keine Titel übernehmen, nur `Kapitel NNN` |
| `--per-disc` | pro Disc nummerieren (`1-01`) statt durchlaufend |
| `--narrator "…"` | Sprecher setzen — MusicBrainz kennt sie selten |
| `--fetch-cover` | Cover neu vom Cover Art Archive laden |
| `--tol <s>` | erlaubte Blockabweichung (Default 2 s) |

## Cover

Reihenfolge: lokales `cover.jpg` → sonst [Cover Art Archive](https://coverartarchive.org).
`--fetch-cover` erzwingt den Download, `--no-cover` deaktiviert das Setzen von
Covern. Das Bild wird als APIC-Frame in jede Datei eingebettet.

## Was geschrieben wird

hoerspur aktualisiert ID3v2-Tags (TIT2, TALB, TPE1, TRCK, TPOS, etc.) und fügt
zusätzliche Felder wie `TXXX:AUTHOR` und `TXXX:NARRATEDBY` hinzu. ID3v2.3 wird
verwendet, da es breit unterstützt ist.

## Sicherheitsnetze

- Ohne `--apply` wird nichts geschrieben.
- Alte Tags werden vor dem Überschreiben in `.hoerspur-backup.json` gesichert.
- Beim Neuschnitt wird die Gesamtlaufzeit geprüft; bei Abweichungen > 5 s wird
  abgebrochen.
- Originaldateien werden nie gelöscht, sondern nach `_original/` verschoben.

## Grenzen

- MusicBrainz kennt nicht jedes Hörbuch; bei Nischentiteln bleibt `--numbers-only`.
- Die Titelqualität hängt von den Freiwilligen-Einträgen ab; hoerspur ändert
  keine Titel orthografisch.
- `--recut` unterstützt aktuell nur MP3.

## Beispiele

1. Ordner analysieren, ohne zu schreiben:

```bash
hoerspur /pfad/zu/rip
```

2. Interaktiv prüfen und anwenden:

```bash
hoerspur /pfad/zu/rip --tui --apply
```

## Tests

```bash
python3 tests/test_hoerspur.py
```

Die Tests erzeugen Test-MP3s, führen ggf. Neuschnitt durch und prüfen, ob die
Audio-Segmentierung korrekt ist.

## Mitwirken

- Bugfixes und Verbesserungen per Pull Request willkommen.
- Bitte Issues anlegen mit reproduzierbaren Schritten und Beispieldateien, wenn
  möglich.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
