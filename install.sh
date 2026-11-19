#!/usr/bin/env bash
# Install proscope for the current user (or system-wide with sudo).
# Usage: bash install.sh            -> ~/.local/bin/proscope
#        sudo bash install.sh       -> /usr/local/bin/proscope
#        bash install.sh --uninstall
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $EUID -eq 0 ]]; then
    PREFIX="/usr/local"
else
    PREFIX="${HOME}/.local"
fi
LIB="${PREFIX}/lib/proscope"
BIN="${PREFIX}/bin/proscope"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -rf "$LIB" "$BIN"
    echo "proscope removed from ${PREFIX}"
    exit 0
fi

[[ "$(uname -s)" == "Linux" ]] || { echo "proscope only runs on Linux" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || { echo "Python 3.8+ is required" >&2; exit 1; }

mkdir -p "$LIB" "$(dirname "$BIN")"
rm -rf "${LIB:?}/proscope"
cp -r "$SRC/proscope" "$LIB/"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m proscope "\$@"
EOF
chmod +x "$BIN"

echo "Installed proscope -> $BIN"
case ":$PATH:" in
    *":${PREFIX}/bin:"*) ;;
    *) echo "Note: add ${PREFIX}/bin to your PATH (e.g. in ~/.bashrc)";;
esac
