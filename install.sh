#!/usr/bin/env bash
# KYBER installer
# Run this from inside the cloned repo, expected to live at ~/kyber
# (the application code itself hardcodes this path, so the repo needs
# to be cloned there for anything to work).

set -euo pipefail

KYBER_DIR="$HOME/kyber"
CURRENT_USER="$(whoami)"

echo "=== KYBER Installer ==="
echo "Installing for user: $CURRENT_USER"
echo "Target directory:     $KYBER_DIR"
echo ""

if [ "$(pwd)" != "$KYBER_DIR" ]; then
  echo "NOTE: This script expects to be run from $KYBER_DIR, but you're"
  echo "running it from $(pwd)."
  echo "The app itself hardcodes ~/kyber as its project directory, so"
  echo "anything other than that path will likely break later."
  read -r -p "Continue anyway? [y/N] " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo ""
echo "--- [1/8] Installing system packages ---"
sudo apt-get update
sudo apt-get install -y \
  python3-venv python3-dev \
  bluez bluez-firmware \
  network-manager dnsmasq-base avahi-daemon \
  alsa-utils \
  pipewire pipewire-alsa pipewire-audio pipewire-bin pipewire-pulse wireplumber \
  libgirepository1.0-dev libcairo2-dev pkg-config \
  iptables \
  python3-dbus python3-gi \
  git

# ---------------------------------------------------------------------------
# 2. Python virtual environment
# ---------------------------------------------------------------------------
echo ""
echo "--- [2/8] Setting up Python virtual environment ---"
# --system-site-packages is required here specifically so the venv can see
# the system-installed python3-dbus/python3-gi above. dbus-python and
# PyGObject are deep C bindings into the system's actual GLib/D-Bus
# libraries -- a pip-built copy can silently mismatch against the real
# runtime in ways that don't error, they just misbehave (confirmed: this
# is exactly what caused Beacon Relay's advertisement registration to fail
# on a from-scratch venv, while working fine with the system packages).
if [ ! -d "$KYBER_DIR/venv" ]; then
  python3 -m venv --system-site-packages "$KYBER_DIR/venv"
  echo "Created venv at $KYBER_DIR/venv"
else
  echo "venv already exists, reusing it"
fi

"$KYBER_DIR/venv/bin/python" -m pip install --upgrade pip
# Pinned below 82: setuptools 82.0.0 (Feb 2026) permanently removed
# pkg_resources, which webrtcvad (an older, unmaintained package) still
# imports at its own module level. Without this pin, webrtcvad silently
# fails to import and VAD-based mic capture never works.
"$KYBER_DIR/venv/bin/python" -m pip install "setuptools<82"
"$KYBER_DIR/venv/bin/python" -m pip install -r "$KYBER_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 3. .env scaffolding (never overwrite an existing one)
# ---------------------------------------------------------------------------
echo ""
echo "--- [3/8] Setting up .env ---"
if [ ! -f "$KYBER_DIR/.env" ]; then
  cp "$KYBER_DIR/.env.example" "$KYBER_DIR/.env"
  echo "Created .env from template."
  echo "API keys and droid pairing get filled in via the onboarding wizard"
  echo "after this script finishes -- nothing more to do here by hand."
else
  echo ".env already exists, leaving it untouched."
fi

# ---------------------------------------------------------------------------
# 4. systemd service files
# ---------------------------------------------------------------------------
echo ""
echo "--- [4/8] Installing systemd services ---"

sudo tee /etc/systemd/system/kyber.service > /dev/null <<EOF
[Unit]
Description=KYBER Voice Control Service
After=network.target bluetooth.target
Requires=bluetooth.target

[Service]
Type=simple
WorkingDirectory=$KYBER_DIR
EnvironmentFile=$KYBER_DIR/.env
ExecStartPre=/bin/sleep 15
ExecStart=$KYBER_DIR/venv/bin/python -u $KYBER_DIR/kyber_core.py
Restart=always
RestartSec=5
User=$CURRENT_USER
SupplementaryGroups=audio
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u "$CURRENT_USER")

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/kyber_config.service > /dev/null <<EOF
[Unit]
Description=KYBER Configuration Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$KYBER_DIR
EnvironmentFile=$KYBER_DIR/.env
ExecStart=$KYBER_DIR/venv/bin/python -u $KYBER_DIR/kyber_config_server.py
Restart=always
RestartSec=5
User=$CURRENT_USER

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/kyber_netgw.service > /dev/null <<EOF
[Unit]
Description=KYBER Network Gateway (AP/Client wifi switching + portal handling)
After=NetworkManager.service
Wants=NetworkManager.service
Before=kyber.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$KYBER_DIR
ExecStart=$KYBER_DIR/venv/bin/python -u $KYBER_DIR/kyber_netgw.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Service files written for $CURRENT_USER."

# ---------------------------------------------------------------------------
# 5. sudoers files
# ---------------------------------------------------------------------------
echo ""
echo "--- [5/8] Configuring sudo permissions ---"

sudo tee /etc/sudoers.d/kyber > /dev/null <<EOF
$CURRENT_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop kyber.service, /usr/bin/systemctl start kyber.service, /usr/bin/systemctl restart kyber.service, /usr/bin/systemctl poweroff
$CURRENT_USER ALL=(ALL) NOPASSWD: /usr/sbin/shutdown -h now
EOF
sudo chmod 440 /etc/sudoers.d/kyber

IPTABLES_PATH="$(which iptables)"
sudo tee /etc/sudoers.d/kyber-netgw > /dev/null <<EOF
# Managed by install.sh -- scoped to exactly what kyber_netgw.py shells out
# to: nmcli, writing the one dnsmasq config snippet that makes the
# captive-portal trigger work, and the port-80-to-5003 NAT redirect that
# lets phone OS captive-portal probes reach netgw's HTTP server.
$CURRENT_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
$CURRENT_USER ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/NetworkManager/dnsmasq-shared.d/kyber-portal.conf
$CURRENT_USER ALL=(ALL) NOPASSWD: $IPTABLES_PATH -t nat -A PREROUTING -i * -p tcp --dport 80 -j REDIRECT --to-port 5003
$CURRENT_USER ALL=(ALL) NOPASSWD: $IPTABLES_PATH -t nat -D PREROUTING -i * -p tcp --dport 80 -j REDIRECT --to-port 5003
EOF
sudo chmod 440 /etc/sudoers.d/kyber-netgw

echo "Validating sudoers syntax..."
sudo visudo -c

# ---------------------------------------------------------------------------
# 6. BlueZ experimental mode (needed for Beacon Relay)
# ---------------------------------------------------------------------------
echo ""
echo "--- [6/8] Enabling BlueZ experimental mode ---"

# Fresh Raspberry Pi OS images can ship with Bluetooth soft-blocked by
# default (often tied to regional/country-code settings never being set
# during imaging). Nothing downstream -- bluetoothctl, KYBER's own
# scanning, none of it -- can work until this is cleared.
sudo rfkill unblock bluetooth

sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf > /dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --experimental
EOF

# ---------------------------------------------------------------------------
# 7. Persistent journald logging (drop-in, main journald.conf left untouched)
# ---------------------------------------------------------------------------
echo ""
echo "--- [7/8] Configuring persistent logging ---"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/kyber.conf > /dev/null <<'EOF'
[Journal]
Storage=persistent
MaxRetentionSec=1day
SystemMaxUse=500M
EOF

# ---------------------------------------------------------------------------
# 8. Reload and start everything
# ---------------------------------------------------------------------------
echo ""
echo "--- [8/8] Starting services ---"
sudo systemctl daemon-reload
sudo systemctl restart bluetooth.service
sudo systemctl restart systemd-journald.service
sudo systemctl enable --now kyber_netgw.service
sudo systemctl enable --now kyber_config.service
sudo systemctl enable --now kyber.service

HOSTNAME="$(hostname)"
IP_ADDRESS="$(hostname -I | awk '{print $1}')"
echo ""
echo "======================================================"
echo " KYBER installed."
echo ""
echo " Next step: open one of these in a browser to run the"
echo " onboarding wizard and pair your droid:"
echo ""
echo "   http://$HOSTNAME.local:5001"
echo "   http://$IP_ADDRESS:5001"
echo ""
echo " If the first one doesn't load (.local addresses don't"
echo " always resolve on every network), use the IP instead."
echo "======================================================"
