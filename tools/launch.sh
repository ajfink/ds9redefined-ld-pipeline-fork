#!/bin/sh
# FromStunToKill launcher for macOS and Linux (started by FromStunToKill.command / FromStunToKill.sh).
# Finds Python 3.12-3.15 (or offers to download a private, checksum-verified copy), then hands over to
# tools/fstk_launch.py, which builds this machine's environment, runs setup on a new machine and starts the app.
# Nothing is written into the app folder: everything this script creates lives under the per-machine folder.

here=$(cd "$(dirname "$0")" && pwd -P) || exit 1
core="$here/fstk_launch.py"
os=$(uname -s)
arch=$(uname -m)
if [ "$os" = "Darwin" ]; then
    machine="$HOME/Library/Application Support/FromStunToKill"
else
    machine="${XDG_DATA_HOME:-$HOME/.local/share}/fstk"
fi
private="$machine/python/bin/python3"
check='import sys; sys.exit(0 if (3,12) <= sys.version_info[:2] < (3,16) else 1)'

usable() { command -v "$1" >/dev/null 2>&1 && "$1" -c "$check" >/dev/null 2>&1; }

find_python() {
    for v in 3.13 3.12 3.14 3.15; do
        for c in "python$v" "/opt/homebrew/bin/python$v" "/usr/local/bin/python$v" \
                 "/Library/Frameworks/Python.framework/Versions/$v/bin/python3"; do
            if usable "$c"; then echo "$c"; return 0; fi
        done
    done
    for c in python3 python "$private"; do
        if usable "$c"; then echo "$c"; return 0; fi
    done
    return 1
}

fetch() {   # fetch URL [OUTFILE]
    if command -v curl >/dev/null 2>&1; then
        if [ -n "$2" ]; then curl -fsSL "$1" -o "$2"; else curl -fsSL "$1"; fi
    elif command -v wget >/dev/null 2>&1; then
        if [ -n "$2" ]; then wget -q "$1" -O "$2"; else wget -qO- "$1"; fi
    else
        echo "Neither curl nor wget is available." >&2; return 1
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

install_private_python() {
    case "$os/$arch" in
        Darwin/arm64) triple=aarch64-apple-darwin ;;
        Darwin/x86_64) triple=x86_64-apple-darwin ;;
        Linux/x86_64) triple=x86_64-unknown-linux-gnu ;;
        Linux/aarch64|Linux/arm64) triple=aarch64-unknown-linux-gnu ;;
        *) echo "No prebuilt Python for $os/$arch." >&2; return 1 ;;
    esac
    meta=$(fetch https://raw.githubusercontent.com/astral-sh/python-build-standalone/latest-release/latest-release.json) || return 1
    tag=$(printf '%s' "$meta" | sed -n 's/.*"tag"[^"]*"\([^"]*\)".*/\1/p' | head -n 1)
    [ -n "$tag" ] || { echo "Could not read the release tag." >&2; return 1; }
    prefix="https://github.com/astral-sh/python-build-standalone/releases/download/$tag"
    sums=$(fetch "$prefix/SHA256SUMS") || return 1
    line=$(printf '%s\n' "$sums" | grep -E "cpython-3\.12\.[0-9]+\+$tag-$triple-install_only\.tar\.gz\$" | head -n 1)
    [ -n "$line" ] || { echo "No Python 3.12 build for $triple in release $tag." >&2; return 1; }
    want=$(printf '%s' "$line" | awk '{print $1}')
    file=$(printf '%s' "$line" | awk '{print $2}')
    [ -n "$file" ] || { echo "Could not read the archive name." >&2; return 1; }
    mkdir -p "$machine" || return 1
    echo "Downloading $file ..."
    fetch "$prefix/$file" "$machine/$file" || return 1
    got=$(sha256_of "$machine/$file")
    if [ "$got" != "$want" ]; then
        rm -f "${machine:?}/${file:?}"
        echo "Checksum mismatch for $file." >&2
        return 1
    fi
    rm -rf "${machine:?}/python"
    tar -xzf "$machine/$file" -C "$machine" || return 1
    rm -f "${machine:?}/${file:?}"
    [ -x "$private" ] || { echo "The unpacked Python was not where expected." >&2; return 1; }
}

pause_if_interactive() {
    if [ -t 0 ]; then printf '\nPress Enter to close this window. '; read -r _ignored; fi
}

py=$(find_python)
if [ -z "$py" ]; then
    echo "FromStunToKill needs Python 3.12 to 3.15, and none was found on this computer."
    if [ ! -t 0 ]; then
        echo "Install Python 3.12 or newer (https://www.python.org/downloads/ or your package manager) and start again."
        exit 3
    fi
    echo "It can download a private copy (python-build-standalone, checksum-verified, about 40 MB) into"
    echo "  $machine/python"
    echo "No administrator rights are needed and nothing else on the computer is changed."
    printf 'Download it now? [Y/n] '
    read -r answer
    case "$answer" in
        [nN]*) echo "Install Python 3.12 or newer and start this again."; pause_if_interactive; exit 3 ;;
    esac
    if ! install_private_python; then
        echo "Could not install Python automatically. Install Python 3.12 or newer and start this again."
        pause_if_interactive
        exit 1
    fi
    py="$private"
fi
exec "$py" "$core" "$@"
