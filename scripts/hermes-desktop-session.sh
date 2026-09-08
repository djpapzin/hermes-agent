#!/usr/bin/env bash
# Keep one browser desktop alive independently of any cua-driver transport.
# The display number is allocated by Xvfb; no host-specific :N is assumed.
set -euo pipefail

STATE_FILE="${HERMES_HOME:-${HOME:-/tmp}/.hermes}/state/computer_use/desktop-session.env"
SCREEN="1920x1080x24"
INSIDE=0
GENERATION=""

usage() {
    echo "usage: $0 [--state-file PATH] [--screen WxHxD] -- COMMAND [ARGS...]" >&2
    exit 2
}

while (($#)); do
    case "$1" in
        --state-file)
            (($# >= 2)) || usage
            STATE_FILE=$2
            shift 2
            ;;
        --screen)
            (($# >= 2)) || usage
            SCREEN=$2
            shift 2
            ;;
        --inside)
            INSIDE=1
            shift
            ;;
        --generation)
            (($# >= 2)) || usage
            GENERATION=$2
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *)
            usage
            ;;
    esac
done

(($# > 0)) || usage

STATE_DIR=$(dirname -- "$STATE_FILE")
mkdir -p -- "$STATE_DIR"
chmod 700 -- "$STATE_DIR" 2>/dev/null || true

write_state() {
    local generation=$1 browser_pid=$2 at_spi_pid=${3:-}
    local temporary
    umask 077
    temporary=$(mktemp "${STATE_FILE}.tmp.XXXXXX")
    chmod 600 -- "$temporary"
    {
        printf 'DISPLAY=%s\n' "$DISPLAY"
        if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" ]]; then
            printf 'XDG_RUNTIME_DIR=%s\n' "$XDG_RUNTIME_DIR"
        fi
        [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] &&
            printf 'DBUS_SESSION_BUS_ADDRESS=%s\n' "$DBUS_SESSION_BUS_ADDRESS"
        [[ -n "${DBUS_SESSION_BUS_PID:-}" ]] &&
            printf 'DBUS_SESSION_BUS_PID=%s\n' "$DBUS_SESSION_BUS_PID"
        [[ -n "${AT_SPI_BUS_ADDRESS:-}" ]] &&
            printf 'AT_SPI_BUS_ADDRESS=%s\n' "$AT_SPI_BUS_ADDRESS"
        [[ -n "$at_spi_pid" ]] && printf 'AT_SPI_BUS_PID=%s\n' "$at_spi_pid"
        printf 'BROWSER_PID=%s\n' "$browser_pid"
        printf 'SESSION_PID=%s\n' "$$"
        printf 'GENERATION=%s\n' "$generation"
    } > "$temporary"
    mv -f -- "$temporary" "$STATE_FILE"
}

clear_state_if_owned() {
    local generation=$1 current
    [[ -f "$STATE_FILE" ]] || return 0
    current=$(awk -F= '$1 == "GENERATION" { print substr($0, index($0, "=") + 1); exit }' "$STATE_FILE" 2>/dev/null || true)
    [[ "$current" == "$generation" ]] && rm -f -- "$STATE_FILE"
}

if ((INSIDE)); then
    generation="$GENERATION"
    [[ -n "$generation" ]] || generation="$(date +%s%N)-$$"

    at_spi_pid=""
    at_spi_launcher="/usr/libexec/at-spi-bus-launcher"
    if [[ ! -x "$at_spi_launcher" ]]; then
        at_spi_launcher=$(command -v at-spi-bus-launcher || true)
    fi
    if [[ -n "$at_spi_launcher" ]]; then
        "$at_spi_launcher" --launch-immediately >/dev/null 2>&1 &
        at_spi_pid=$!
    fi

    # Give the accessibility registrar a short, quiet head start. Failure is
    # left to cua-driver's doctor; the browser and DBus session remain alive.
    if command -v dbus-send >/dev/null 2>&1; then
        for _ in {1..20}; do
            if dbus-send --session --type=method_call --print-reply \
                --dest=org.a11y.Bus /org/a11y/bus \
                org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1; then
                break
            fi
            sleep 0.1
        done
    fi

    "$@" &
    browser_pid=$!
    write_state "$generation" "$browser_pid" "$at_spi_pid"

    cleanup_inside() {
        kill "$browser_pid" 2>/dev/null || true
        kill "$at_spi_pid" 2>/dev/null || true
        clear_state_if_owned "$generation"
    }
    trap cleanup_inside EXIT INT TERM
    wait "$browser_pid"
    exit $?
fi

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/hermes-desktop-session.XXXXXX")
display_file="$tmp_dir/display"
xvfb_pid=""
cleanup_outer() {
    if [[ -n "$xvfb_pid" ]]; then
        kill "$xvfb_pid" 2>/dev/null || true
        wait "$xvfb_pid" 2>/dev/null || true
    fi
    rm -rf -- "$tmp_dir"
}
trap cleanup_outer EXIT INT TERM

# Xvfb writes the allocated display number to this descriptor. This is the
# only display selection mechanism used by the persistent browser service.
exec {display_fd}>"$display_file"
Xvfb -displayfd "$display_fd" -screen 0 "$SCREEN" -nolisten tcp \
    >/dev/null 2>&1 &
xvfb_pid=$!
exec {display_fd}>&-

display_number=""
for _ in {1..100}; do
    if [[ -s "$display_file" ]]; then
        IFS= read -r display_number < "$display_file" || true
        [[ "$display_number" =~ ^[0-9]+$ ]] && break
        display_number=""
    fi
    kill -0 "$xvfb_pid" 2>/dev/null || exit 1
    sleep 0.05
done
[[ -n "$display_number" ]] || exit 1
export DISPLAY=":$display_number"

generation="$(date +%s%N)-$$"

# dbus-run-session creates a session bus tied to this browser supervisor.
# Keep this shell alive so cleanup always tears down only this desktop.
dbus-run-session -- "$0" --inside --generation "$generation" \
    --state-file "$STATE_FILE" -- "$@"
