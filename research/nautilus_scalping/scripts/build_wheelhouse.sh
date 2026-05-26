#!/usr/bin/env bash
#
# ROB-316 — reproducible Intel-macOS native wheelhouse for NautilusTrader.
#
# Builds a version/ABI/arch-specific nautilus_trader wheel FROM SOURCE on
# Intel macOS (no PyPI x86_64-macOS wheel exists), preserves it in a local
# wheelhouse, and smoke-installs it into a clean venv. The final wheel is
# specific to (version + cpXY + macos-arch + Rust); but the Cargo/uv build
# caches persist across versions so re-builds reuse compiled crates.
#
# Docker is intentionally NOT used (perf on macOS). v1.191.0-era macOS x86
# wheels are a historical note only — the primary path is latest source build.
#
# Nothing here touches brokers/orders/schedulers/prod-DB/secrets. Public
# package source only. The wheelhouse lives OUTSIDE the repo (~/wheelhouse)
# and is never committed.
#
# KNOWN ISSUE (reproducibility): a clean-room source build can FAIL on upstream
# Rust crate drift — nautilus 1.227.0 pulls pyo3-stub-gen 0.20.0, which breaks
# against pyo3 0.28.3 (E0425, PyEncodingWarning removed). nautilus's sdist does
# not lock that transitive dep, so a fresh CARGO_HOME may resolve a broken set.
# => The canonical artifact is the PRESERVED wheel in wheels/, not a guaranteed
#    rebuild. For rebuilds, pin pyo3 (cargo update --precise) or use a captured
#    Cargo.lock. See docs/plans/ROB-316-nautilustrader-adoption-spike-plan.md §12.7.
#
# Usage:
#   scripts/build_wheelhouse.sh [VERSION] [--smoke-only]
#     VERSION       NautilusTrader version (default: 1.227.0, the verified one)
#     --smoke-only  skip the source build; just smoke-install the existing
#                   wheelhouse wheel (fast check, no Rust compile)

set -uo pipefail

# ---- config ---------------------------------------------------------------
VERSION="1.227.0"
SMOKE_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --smoke-only) SMOKE_ONLY=1 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) VERSION="$arg" ;;
  esac
done

PYTHON_REQ="3.13"          # the Python the spike verified (cp313)
WH="$HOME/wheelhouse/nautilus_trader"
WHEELS="$WH/wheels"
LOGS="$WH/logs"
export CARGO_HOME="$WH/cargo-home"
export CARGO_TARGET_DIR="$WH/cargo-target"
export UV_CACHE_DIR="$WH/uv-cache"
export PIP_CACHE_DIR="$WH/pip-cache"   # pip wheel uses this for the sdist
export PATH="$HOME/.cargo/bin:$PATH"

mkdir -p "$WHEELS" "$LOGS" "$CARGO_HOME" "$CARGO_TARGET_DIR" "$UV_CACHE_DIR" "$PIP_CACHE_DIR"
TS="$(date +%Y%m%d-%H%M%S)"

STEP="init"
say()  { echo "[wheelhouse] $*"; }
fail() { echo "[wheelhouse] FAILED at step: $STEP" >&2; exit 1; }

# ---- 3) arch gate ---------------------------------------------------------
STEP="arch-check"
ARCH="$(uname -m)"
say "arch: $ARCH"
[ "$ARCH" = "x86_64" ] || { echo "this policy is Intel macOS x86_64 only (got $ARCH)"; fail; }

# ---- 4) rust / cargo ------------------------------------------------------
STEP="rust-check"
command -v cargo >/dev/null 2>&1 || { echo "cargo not found — install rustup"; fail; }
RUST_VER="$(rustc --version | awk '{print $2}')"
say "rustc: $(rustc --version)"
say "cargo: $(cargo --version)"

# ---- 2) python / ABI ------------------------------------------------------
STEP="build-venv"
BUILD_VENV="$WH/buildenv"
# --clear so re-runs replace an existing buildenv instead of erroring.
uv venv "$BUILD_VENV" --python "$PYTHON_REQ" --clear >"$LOGS/buildenv-$TS.log" 2>&1 \
  || { echo "could not create build venv:"; tail -5 "$LOGS/buildenv-$TS.log"; fail; }
BPY="$BUILD_VENV/bin/python"
PYV="$("$BPY" -c 'import sys;print("cp%d%d"%sys.version_info[:2])')"
PLAT="$("$BPY" -c 'import sysconfig;print(sysconfig.get_platform())')"
say "python: $("$BPY" --version) ($PYV, $PLAT)"

# ---- cache key ------------------------------------------------------------
KEY="nautilus_trader-${VERSION}+${PYV}+macos-${ARCH}+rust-${RUST_VER}"
say "cache key: $KEY"
say "wheelhouse: $WH"

LOG="$LOGS/build-${VERSION}-${PYV}-${ARCH}-${TS}.log"

# ---- 6) build the wheel ---------------------------------------------------
if [ "$SMOKE_ONLY" -eq 0 ]; then
  STEP="pip-in-build-venv"
  uv pip install --python "$BPY" pip >/dev/null 2>&1 || { echo "could not install pip into build venv"; fail; }

  STEP="pip-wheel (source build — slow Rust compile; cargo-target persists)"
  say "building wheel for nautilus_trader==$VERSION  (log: $LOG)"
  SECONDS=0
  if ! "$BPY" -m pip wheel "nautilus_trader==$VERSION" \
        --no-binary nautilus_trader --no-deps -w "$WHEELS" >"$LOG" 2>&1; then
    echo "----- last 20 log lines -----"; tail -20 "$LOG"; fail
  fi
  say "build duration: ${SECONDS}s"
else
  say "--smoke-only: skipping source build"
fi

STEP="locate-wheel"
WHEEL="$(ls -t "$WHEELS"/nautilus_trader-"$VERSION"-*.whl 2>/dev/null | head -1)"
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || { echo "no wheel for $VERSION in $WHEELS"; fail; }

# ---- 7+8) smoke install: two modes ---------------------------------------
SMOKE_PY='import nautilus_trader as n, platform, sys; \
print("import_ok", n.__version__, "|", n.__file__.split("site-packages/")[-1], \
"|", platform.machine(), "py"+".".join(map(str, sys.version_info[:3])))'

smoke() { # $1 = mode
  local mode="$1" venv="$WH/smoke-$1" extra=""
  [ "$mode" = "offline-strict" ] && extra="--no-index"
  rm -rf "$venv"
  uv venv "$venv" --python "$PYTHON_REQ" >/dev/null 2>&1 || { echo "venv fail"; return 1; }
  if uv pip install --python "$venv/bin/python" $extra --find-links "$WHEELS" \
        "nautilus_trader==$VERSION" >"$LOGS/smoke-$mode-$TS.log" 2>&1; then
    "$venv/bin/python" -c "$SMOKE_PY"
    return 0
  fi
  return 1
}

echo ""
say "=== smoke: online-prefer-local (local wheel + PyPI deps) ==="
if smoke "online-prefer-local"; then
  ONLINE="PASS"
else
  ONLINE="FAIL"; echo "  see $LOGS/smoke-online-prefer-local-$TS.log"
fi

echo ""
say "=== smoke: offline-strict (--no-index, wheelhouse only) ==="
if smoke "offline-strict"; then
  OFFLINE="PASS"
else
  OFFLINE="EXPECTED-FAIL (dependency wheels not yet in wheelhouse)"
  grep -iE "no version of|cannot be used|no matching" "$LOGS/smoke-offline-strict-$TS.log" | head -2
fi

# ---- 9) summary -----------------------------------------------------------
echo ""
say "================= RESULT ================="
say "wheel:    $(basename "$WHEEL")"
say "size:     $(du -h "$WHEEL" | cut -f1)"
say "sha256:   $(shasum -a 256 "$WHEEL" | cut -d' ' -f1)"
say "key:      $KEY"
say "online-prefer-local: $ONLINE"
say "offline-strict:      $OFFLINE"
say "caches (persist across versions):"
say "  CARGO_HOME=$CARGO_HOME"
say "  CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
say "  UV_CACHE_DIR=$UV_CACHE_DIR"
say "=========================================="
