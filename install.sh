#!/usr/bin/env bash
# Installiert hoerspur nach ~/.local/share/hoerspur und ~/.local/bin/hoerspur
set -euo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
share="${HOME}/.local/share/hoerspur"
bin="${HOME}/.local/bin"

mkdir -p "$share" "$bin"
install -m 644 "$src/hoerspur.py" "$src/hoerspur_tui.py" "$share/"
install -m 755 "$src/hoerspur" "$bin/hoerspur"

echo "installiert:"
echo "  $bin/hoerspur"
echo "  $share/"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo; echo "Hinweis: $bin liegt nicht im PATH." ;;
esac
python3 -c 'import mutagen' 2>/dev/null \
  || echo "Hinweis: 'mutagen' fehlt — pip install --user mutagen"
command -v ffmpeg >/dev/null \
  || echo "Hinweis: 'ffmpeg' fehlt — nur nötig für --recut"
