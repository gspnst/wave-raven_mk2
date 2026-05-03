#!/bin/bash
# ============================================================
# TowerScan Mk2 - Setup Script
# Raspberry Pi 5 + Nooelec NESDR SMArt XTR
# Raspberry Pi OS Bookworm 64-bit
#
# Installs:
#   - rtl-sdr drivers
#   - kalibrate-rtl (from source)
#   - gr-gsm (from source, with device.py patch)
#   - LTE-Cell-Scanner (from source)
# ============================================================
set -e

SUDO=""; [ "$EUID" -ne 0 ] && SUDO="sudo"
CORES=$(nproc)
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

log()  { echo ""; echo "[$1/$STEPS] $2..."; }
ok()   { echo "  ✓ $1"; }
skip() { echo "  → Already installed, skipping."; }

STEPS=8

echo "╔══════════════════════════════════════════════╗"
echo "║        TowerScan Mk2 — Setup                 ║"
echo "║  Raspberry Pi 5 + Nooelec NESDR SMArt XTR    ║"
echo "╚══════════════════════════════════════════════╝"
echo "Install prefix : /usr/local"
echo "Project dir    : $INSTALL_DIR"
echo "CPU cores      : $CORES"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
log 1 "Installing system packages"
$SUDO apt-get update -qq
$SUDO apt-get install -y \
  rtl-sdr librtlsdr-dev librtlsdr0 \
  gnuradio gnuradio-dev gr-osmosdr \
  libosmocore-dev \
  python3-pip python3-requests python3-numpy \
  git build-essential cmake pkg-config \
  libboost-all-dev libcppunit-dev \
  libfftw3-dev libfftw3-dev \
  swig doxygen \
  tshark wireshark-common \
  autoconf automake libtool \
  libusb-1.0-0-dev
ok "System packages installed"

# ── 2. Blacklist DVB-T modules ────────────────────────────────────────────────
log 2 "Blacklisting DVB-T kernel modules"
BFILE="/etc/modprobe.d/blacklist-rtl.conf"
for mod in dvb_usb_rtl28xxu rtl2832 rtl2830; do
  if ! grep -q "$mod" "$BFILE" 2>/dev/null; then
    echo "blacklist $mod" | $SUDO tee -a "$BFILE" > /dev/null
    ok "Blacklisted $mod"
  fi
done
$SUDO modprobe -r dvb_usb_rtl28xxu rtl2832 rtl2830 2>/dev/null || true
ok "DVB-T modules unloaded"

# ── 3. udev rules + USB power ─────────────────────────────────────────────────
log 3 "Configuring udev rules and USB power (Pi 5)"
cat << 'EOF' | $SUDO tee /etc/udev/rules.d/20-rtlsdr.rules > /dev/null
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
EOF
$SUDO udevadm control --reload-rules && $SUDO udevadm trigger
$SUDO usermod -a -G plugdev "$USER" 2>/dev/null || true

CONF="/boot/firmware/config.txt"
if ! grep -q "usb_max_current_enable" "$CONF" 2>/dev/null; then
  echo "usb_max_current_enable=1" | $SUDO tee -a "$CONF" > /dev/null
  ok "USB max current enabled (Pi 5)"
else
  ok "USB max current already configured"
fi

# ── 4. kalibrate-rtl ─────────────────────────────────────────────────────────
log 4 "Building kalibrate-rtl from source"
if command -v kal &>/dev/null; then
  skip
else
  TMP=$(mktemp -d)
  git clone --depth=1 https://github.com/steve-m/kalibrate-rtl "$TMP/kal"
  cd "$TMP/kal"
  ./bootstrap && ./configure && make -j"$CORES"
  $SUDO make install
  cd "$INSTALL_DIR"
  rm -rf "$TMP"
  ok "kalibrate-rtl installed → $(command -v kal)"
fi

# ── 5. gr-gsm from source (with device.py patch) ─────────────────────────────
log 5 "Building gr-gsm from source"
if command -v grgsm_scanner &>/dev/null; then
  skip
else
  GR_DIR="$HOME/src/gr-gsm"
  mkdir -p "$HOME/src"
  [ -d "$GR_DIR" ] && rm -rf "$GR_DIR"
  git clone --depth=1 https://github.com/ptrkrysik/gr-gsm "$GR_DIR"

  # Patch device.py before building — fixes osmosdr Python incompatibility
  DEVPY="$GR_DIR/python/receiver/device.py"
  if [ -f "$DEVPY" ]; then
    python3 - "$DEVPY" << 'PYEOF'
import sys, re
path = sys.argv[1]
with open(path) as f:
    src = f.read()
new_match = '''def match(dev, filters):
    dev_str = dev.to_string()
    if isinstance(filters, dict):
        for k, v in filters.items():
            if k + "=" + v not in dev_str:
                return False
    return True'''
src = re.sub(r'def match\(dev, filters\):.*?return True',
             new_match, src, flags=re.DOTALL)
with open(path, 'w') as f:
    f.write(src)
print("  device.py patched.")
PYEOF
  fi

  mkdir -p "$GR_DIR/build" && cd "$GR_DIR/build"
  cmake .. -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_BUILD_TYPE=Release -Wno-dev
  make -j"$CORES"
  $SUDO make install
  $SUDO ldconfig
  cd "$INSTALL_DIR"
  ok "gr-gsm installed → $(command -v grgsm_scanner 2>/dev/null || echo '/usr/local/bin/grgsm_scanner')"
fi

# ── 5b. libitpp (required by LTE-Cell-Scanner) ───────────────────────────────
log "5b" "Installing libitpp (LTE-Cell-Scanner dependency)"
if ldconfig -p | grep -q libitpp; then
  skip
elif apt-get install -y libitpp-dev 2>/dev/null; then
  ok "libitpp-dev installed from apt"
else
  echo "  Building libitpp from source (~5 min)..."
  ITPP_DIR="$HOME/src/itpp"
  mkdir -p "$HOME/src"
  [ -d "$ITPP_DIR" ] && rm -rf "$ITPP_DIR"
  wget -q -O /tmp/itpp.tar.bz2     "https://sourceforge.net/projects/itpp/files/itpp/4.3.1/itpp-4.3.1.tar.bz2/download"
  mkdir -p "$ITPP_DIR" && tar xf /tmp/itpp.tar.bz2 -C "$ITPP_DIR" --strip-components=1
  mkdir -p "$ITPP_DIR/build" && cd "$ITPP_DIR/build"
  cmake .. -DCMAKE_BUILD_TYPE=Release -Wno-dev
  make -j"$CORES"
  $SUDO make install
  $SUDO ldconfig
  cd "$INSTALL_DIR"
  rm -f /tmp/itpp.tar.bz2
  ok "libitpp built and installed from source"
fi

# ── 6. LTE-Cell-Scanner ───────────────────────────────────────────────────────
log 6 "Building LTE-Cell-Scanner from source"
if command -v LTE-Cell-Scanner &>/dev/null || command -v CellSearch &>/dev/null; then
  skip
else
  LTE_DIR="$HOME/src/LTE-Cell-Scanner"
  mkdir -p "$HOME/src"
  [ -d "$LTE_DIR" ] && rm -rf "$LTE_DIR"
  git clone --depth=1 https://github.com/Evrytania/LTE-Cell-Scanner "$LTE_DIR"

  mkdir -p "$LTE_DIR/build" && cd "$LTE_DIR/build"
  cmake .. -DCMAKE_BUILD_TYPE=Release
  make -j"$CORES"
  $SUDO make install
  $SUDO ldconfig
  cd "$INSTALL_DIR"
  ok "LTE-Cell-Scanner installed"
fi

# ── 7. Python dependencies ────────────────────────────────────────────────────
log 7 "Installing Python dependencies"
pip3 install requests numpy --break-system-packages 2>/dev/null \
  || pip3 install requests numpy
ok "Python deps installed"

# ── 8. Verify installation ────────────────────────────────────────────────────
log 8 "Verifying installation"

check_tool() {
  if command -v "$1" &>/dev/null; then
    ok "$1 found at $(command -v $1)"
  else
    echo "  ✗ $1 NOT FOUND"
  fi
}

check_tool rtl_sdr
check_tool rtl_power
check_tool kal
check_tool grgsm_scanner
check_tool LTE-Cell-Scanner || check_tool CellSearch

echo ""
RTL_OUT=$(timeout 4 rtl_test 2>&1 || true)
if echo "$RTL_OUT" | grep -qi "found"; then
  echo "$RTL_OUT" | grep -Ei "found|tuner|e4000|r820|crystal" | head -4 | sed 's/^/  /'
else
  echo "  RTL-SDR dongle: not detected (plug in and re-run: rtl_test)"
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           Setup complete!                    ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "⚠  REBOOT REQUIRED for USB power + module blacklist:"
echo "   sudo reboot"
echo ""
echo "NEXT STEPS after reboot:"
echo ""
echo "1. Download OpenCelliD data (Czech Republic):"
echo "   Register free: https://opencellid.org/register"
echo "   wget -O cell_towers.csv.gz \\"
echo "     'https://opencellid.org/ocid/downloads?token=TOKEN&type=mcc&file=mcc-230.csv.gz'"
echo ""
echo "2. Run diagnostic:"
echo "   python3 scan.py --diagnose"
echo ""
echo "3. Import OCID towers near you:"
echo "   python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378"
echo ""
echo "4. Scan GSM + LTE:"
echo "   python3 scan.py --scan --lat 50.0759 --lon 14.4378"
echo ""
echo "5. Open map:"
echo "   python3 server.py  →  http://localhost:5000"
