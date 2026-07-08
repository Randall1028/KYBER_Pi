"""
KYBER Network Gateway — kyber_netgw.service

Owns the Pi's wifi lifecycle, and nothing else. Neither the brain
(kyber_core.py) nor the Mainframe (kyber_config_server.py) talk to nmcli
directly — they ask this service for status, or hand it credentials, over
a small local HTTP API on 127.0.0.1:NETGW_PORT. That's the whole point of
splitting this out: one thing owns wifi state, full stop.

State machine, sequential (never concurrent, per directive):

    AP MODE ("kyber-ap" profile up)
        |  phone joins "KYBER Connect", submits target SSID + password
        v
    CLIENT MODE ("kyber-ap" down, target profile up)
        |  general internet connectivity check (provider-agnostic --
        |  netgw doesn't know or care which LLM/STT providers kyber_core
        |  is configured to use; that's not this service's job)
        |
        +-- reachable -> stay in CLIENT MODE, keep rechecking periodically
        |
        +-- not reachable, still associated -> "bridging" state, surfaces
        |       a message on the Mainframe recommending a personal hotspot
        |       (captive portals were tried via a real headless-Chromium
        |       bridge, kyber_portal_bridge.py -- confirmed architecturally
        |       non-viable for the realistic guest-network case, since
        |       client isolation blocks the phone from ever reaching the
        |       bridge's own viewer; removed rather than left running for
        |       nothing)
        |       |
        |       +-- check passes within the grace window -> stay in CLIENT MODE
        |       +-- still failing after the grace window  -> revert to AP MODE
        |
        +-- nmcli itself failed to associate (bad password, SSID gone) -> revert to AP MODE immediately

Both interfaces (AP_IFACE, STA_IFACE) default to wlan0 — today's behavior
is the single-radio sequential switch exactly as specified. Pointing
AP_IFACE at a second adapter (e.g. wlan1) later needs no code change.
"""

import os
import json
import time
import threading
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from dotenv import dotenv_values

# ── Paths & config (same convention as kyber_core.py / kyber_config_server.py) ──
HOME        = os.path.expanduser('~')
PROJECT_DIR = os.path.join(HOME, 'kyber')
ENV_PATH    = os.path.join(PROJECT_DIR, '.env')

DNSMASQ_SNIPPET_PATH = '/etc/NetworkManager/dnsmasq-shared.d/kyber-portal.conf'

AP_CONNECTION_NAME = 'kyber-ap'
AP_SSID_DEFAULT     = 'KYBER Connect'
AP_IP_DEFAULT       = '10.42.0.1'          # NetworkManager's own default for "shared" mode

NETGW_PORT          = 5003

CHECK_INTERVAL_SECONDS   = 20              # how often CLIENT MODE re-verifies once online
GRACE_WINDOW_SECONDS     = 600             # how long the "bridging" state gets before we give up and revert
ASSOCIATION_TIMEOUT_SECS = 25              # how long we give nmcli to associate with a target network
AP_RETRY_INTERVAL_SECONDS = 180            # how often AP MODE re-checks for a familiar network back in range
AP_RETRY_QUIET_SECONDS    = 90             # skip a retry if the portal was touched more recently than this


def read_env() -> dict:
    return dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}


def cfg(key: str, default: str) -> str:
    """Read a netgw config value from .env, falling back to default.
    Keeping AP_IFACE and STA_IFACE as two independent values (rather than
    one shared variable) is the point — it's what lets a future USB dongle
    take over the AP role without touching code, just .env."""
    return (read_env().get(key) or default).strip()


def ap_iface() -> str:
    return cfg("KYBER_AP_IFACE", "wlan0")


def sta_iface() -> str:
    return cfg("KYBER_STA_IFACE", "wlan0")


def ap_ssid() -> str:
    return cfg("KYBER_AP_SSID", AP_SSID_DEFAULT)


# ── State (single process, guarded by one lock — this service does one
#    network thing at a time by design, so a simple lock is sufficient) ──
_state_lock   = threading.Lock()
_state        = "unknown"      # "ap" | "associating" | "client_checking" | "client_online" | "bridging"
_target_ssid  = None
_last_error   = None
_grace_until  = None
_last_portal_activity = 0.0


def _set_state(new_state: str, error: str = None):
    global _state, _last_error
    with _state_lock:
        _state = new_state
        _last_error = error
    print(f"[NETGW]: state -> {new_state}" + (f" ({error})" if error else ""), flush=True)


def get_status() -> dict:
    with _state_lock:
        state = _state
        target_ssid = _target_ssid
        last_error = _last_error
    # _current_ip() shells out to nmcli -- deliberately outside the lock
    # above, so a slow/hung subprocess call here can't block _set_state()
    # being called from another thread in the meantime.
    current_ip = _current_ip() if state in ("client_checking", "client_online", "bridging") else None
    return {
        "state": state,
        "target_ssid": target_ssid,
        "last_error": last_error,
        "ap_ssid": ap_ssid(),
        "ap_iface": ap_iface(),
        "sta_iface": sta_iface(),
        "current_ip": current_ip,
    }


# ── nmcli wrappers ────────────────────────────────────────────────────────
# All privileged calls go through sudo, matching the existing project
# convention in kyber_config_server.py (stop_kyber_service/restart_kyber_service)
# rather than running this whole service as root. See the deploy notes for
# the sudoers entry this needs.

def _nmcli(args: list, timeout: int = 20) -> subprocess.CompletedProcess:
    cmd = ['sudo', 'nmcli'] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _connection_exists(name: str) -> bool:
    result = _nmcli(['-t', '-f', 'NAME', 'connection', 'show'])
    return name in result.stdout.splitlines()


def _current_ip() -> str:
    """Best-effort IP currently bound to the STA interface, for display
    only -- never used for the connectivity check itself (probe_internet_reachable
    is deliberately stricter than 'has an IP'). Returns None on any failure;
    callers are expected to only ask for this in client-side states, since
    AP_IP_DEFAULT (10.42.0.1) wouldn't tell anyone anything useful anyway."""
    try:
        result = _nmcli(['-g', 'IP4.ADDRESS', 'device', 'show', sta_iface()], timeout=5)
        addr = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        return addr.split('/')[0] if addr else None
    except Exception:
        return None


def _current_connection_name() -> str:
    """Best-effort name of whatever connection profile is actually active on
    the STA interface right now. Since connect_to_target() always creates
    profiles with con-name equal to the SSID, this doubles as the SSID for
    anything actually joined through the normal flow. Returns None on any
    failure or if nothing's connected."""
    try:
        result = _nmcli(['-g', 'GENERAL.CONNECTION', 'device', 'show', sta_iface()], timeout=5)
        name = result.stdout.strip()
        return name if name and name != '--' else None
    except Exception:
        return None


def ensure_ap_profile_exists():
    """Create the kyber-ap connection profile once, if it isn't already there.
    ipv4.method shared is what makes NetworkManager run its own internal
    DHCP server for phones that join — no hand-maintained dnsmasq/hostapd
    config for the AP's basic operation, only for the captive-portal trigger
    on top of it (see write_dnsmasq_snippet)."""
    if _connection_exists(AP_CONNECTION_NAME):
        return
    result = _nmcli([
        'connection', 'add',
        'type', 'wifi',
        'ifname', ap_iface(),
        'con-name', AP_CONNECTION_NAME,
        'autoconnect', 'no',
        'ssid', ap_ssid(),
        '802-11-wireless.mode', 'ap',
        '802-11-wireless.band', 'bg',
        'ipv4.method', 'shared',
    ])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create AP profile: {result.stderr.strip()}")


def _portal_redirect_args(action: str) -> list:
    """action is '-A' (add) or '-D' (delete). Scoped to ap_iface() specifically
    -- this must never apply to sta_iface() traffic, which matters most when
    they're the same physical interface (the default, single-radio case)."""
    return [
        '-t', 'nat', action, 'PREROUTING',
        '-i', ap_iface(), '-p', 'tcp', '--dport', '80',
        '-j', 'REDIRECT', '--to-port', str(NETGW_PORT),
    ]


def _add_portal_redirect():
    # Delete first (ignoring failure) so repeated start_ap() calls -- e.g. a
    # service restart while the AP's already up -- can't pile up duplicate
    # identical rules.
    subprocess.run(['sudo', 'iptables'] + _portal_redirect_args('-D'), capture_output=True)
    subprocess.run(['sudo', 'iptables'] + _portal_redirect_args('-A'), capture_output=True)


def _remove_portal_redirect():
    subprocess.run(['sudo', 'iptables'] + _portal_redirect_args('-D'), capture_output=True)


_cached_networks = []  # purged and replaced wholesale on every scan, never appended to


def _known_wifi_profiles() -> set:
    """SSIDs NetworkManager already has a saved profile for -- this is what
    drives Connect-vs-Forget in the portal's list, and needs no scan at all,
    just reading what's already stored."""
    try:
        result = _nmcli(['-t', '-f', 'NAME,TYPE', 'connection', 'show'], timeout=10)
        names = set()
        for line in result.stdout.splitlines():
            parts = line.split(':')
            if len(parts) >= 2 and parts[1] == '802-11-wireless':
                names.add(parts[0])
        return names
    except Exception:
        return set()


def scan_and_cache_networks():
    """Scan for nearby networks at the one moment the radio is actually free
    to do it -- right after leaving AP/STA, right before AP takes the radio
    over. Not a live scan while AP is up (the same single-radio limit that
    already rules out AP+STA concurrently applies to scanning too); this is
    "as fresh as the last time the radio was idle," which is what's on offer
    every time the Pi is about to go to AP mode anyway. Purges and replaces
    the cache wholesale -- nothing here ever goes stale by accumulating."""
    global _cached_networks
    _cached_networks = []
    try:
        _nmcli(['device', 'wifi', 'rescan'], timeout=10)
        time.sleep(2)
        result = _nmcli(['-t', '-f', 'SSID,SECURITY,SIGNAL', 'device', 'wifi', 'list'], timeout=15)
        known = _known_wifi_profiles()
        seen = set()
        fresh = []
        for line in result.stdout.splitlines():
            parts = line.split(':')
            if len(parts) < 3:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen or ssid == ap_ssid():
                continue
            seen.add(ssid)
            security = parts[1].strip()
            try:
                signal = int(parts[2].strip())
            except ValueError:
                signal = 0
            # nmcli reports "WPA2 WPA3" for transition-mode networks (still
            # accept wpa-psk) and just "WPA3" alone for SAE-only networks
            # (need "sae" as the key-mgmt instead, see connect_to_target).
            sec_upper = security.upper()
            fresh.append({
                "ssid": ssid,
                "open": security in ("", "--"),
                "wpa3_only": "WPA3" in sec_upper and "WPA2" not in sec_upper,
                "signal": signal,
                "known": ssid in known,
            })
        fresh.sort(key=lambda n: -n["signal"])
        _cached_networks = fresh
    except Exception as e:
        print(f"[NETGW]: network scan failed — {e}", flush=True)


def start_ap():
    global _target_ssid
    ensure_ap_profile_exists()
    write_dnsmasq_snippet()
    # Bring down whatever's active on this interface first for a predictable
    # transition, then bring the AP up — explicit two-step rather than relying
    # on NetworkManager's implicit "activating B deactivates A" behavior.
    _nmcli(['device', 'disconnect', sta_iface()])
    # The radio is genuinely idle right here -- the one moment free to scan
    # before AP mode claims it.
    scan_and_cache_networks()
    result = _nmcli(['connection', 'up', AP_CONNECTION_NAME])
    if result.returncode != 0:
        _set_state("ap", error=f"AP failed to start: {result.stderr.strip()}")
        return
    _add_portal_redirect()
    _target_ssid = None
    _set_state("ap")


def stop_ap():
    # Remove the redirect before bringing the connection down -- if
    # AP_IFACE and STA_IFACE are the same interface (the default), a stale
    # rule left in place would incorrectly redirect the target network's
    # own port-80 traffic once STA mode takes over the same radio.
    _remove_portal_redirect()
    _nmcli(['connection', 'down', AP_CONNECTION_NAME])


def _is_wpa3_only(ssid: str) -> bool:
    """Best-effort lookup against the last scan cache. Returns False (the
    existing wpa-psk assumption) for anything not in the cache -- manually
    typed SSIDs never went through a scan, and the vast majority of real
    networks are WPA2 or WPA2/WPA3 transition mode anyway, both of which
    wpa-psk already handles correctly."""
    for net in _cached_networks:
        if net["ssid"] == ssid:
            return net.get("wpa3_only", False)
    return False


def connect_to_target(ssid: str, password: str, assume_known: bool = False) -> bool:
    """Hand off from AP to the target network. Returns True if nmcli reports
    a successful association; the connectivity check (separate step) is what
    actually proves internet reachability."""
    global _target_ssid
    _target_ssid = ssid
    _set_state("associating")
    stop_ap()

    if assume_known:
        # One-tap reconnect from the network list -- reuse whatever's
        # already stored rather than deleting it, since there's no fresh
        # password to rebuild it from. Falls through to the normal path
        # below if the stored secret's gone stale rather than just failing.
        result = _nmcli(['connection', 'up', ssid], timeout=ASSOCIATION_TIMEOUT_SECS)
        if result.returncode == 0:
            _set_state("client_checking")
            return True

    # Always start from a clean slate for this SSID. nmcli reuses an
    # existing same-named profile rather than creating a fresh one -- if
    # one's already sitting there without working secrets (a prior failed
    # attempt, or one created outside this flow entirely), connecting
    # below would try to reuse it and fail no matter what password gets
    # typed this time.
    _nmcli(['connection', 'delete', ssid])

    # Built explicitly rather than via the "device wifi connect SSID
    # password PASS" shortcut. That shortcut depends on NetworkManager
    # already having a fresh scan result for this exact SSID to auto-fill
    # the security type -- right after leaving AP mode, it frequently
    # doesn't, and silently produces a profile with a password but no
    # key-mgmt set at all, which then fails to activate with "key-mgmt:
    # property is missing" regardless of how correct the password is.
    # Setting it explicitly here doesn't depend on scan timing.
    add_args = ['connection', 'add', 'type', 'wifi', 'con-name', ssid, 'ifname', sta_iface(), 'ssid', ssid]
    if password:
        key_mgmt = 'sae' if _is_wpa3_only(ssid) else 'wpa-psk'
        add_args += ['--', 'wifi-sec.key-mgmt', key_mgmt, 'wifi-sec.psk', password]
    result = _nmcli(add_args)
    if result.returncode != 0:
        _nmcli(['connection', 'delete', ssid])
        _set_state("ap", error=f"Could not configure {ssid}: {result.stderr.strip()}")
        start_ap()
        return False

    result = _nmcli(['connection', 'up', ssid], timeout=ASSOCIATION_TIMEOUT_SECS)
    if result.returncode != 0:
        # Don't leave a broken profile behind for the next attempt to trip
        # over the same way this one did.
        _nmcli(['connection', 'delete', ssid])
        _set_state("ap", error=f"Could not join {ssid}: {result.stderr.strip()}")
        start_ap()
        return False
    _set_state("client_checking")
    return True


def revert_to_ap(reason: str, delete_profile: bool = True):
    """Tear down whatever target-network profile is active and re-arm
    KYBER Connect for another attempt. delete_profile=False preserves the
    current network's saved credentials -- used by a deliberate "just let
    me look around" rescan, where wiping a perfectly good password as a
    side effect would be wrong. Real failure-recovery callers keep the
    default (True), since a profile that just failed might be bad and
    shouldn't be blindly reused on the next attempt."""
    global _target_ssid
    if _target_ssid:
        _nmcli(['connection', 'down', _target_ssid])
        if delete_profile:
            _nmcli(['connection', 'delete', _target_ssid])
    _target_ssid = None
    start_ap()
    if reason:
        _set_state("ap", error=reason)


def _is_still_associated() -> bool:
    """Is the target SSID still the active connection on the STA interface,
    at the OS level right now? Distinguishes a genuine wifi drop (nothing
    to wait for, just reconnect) from still being associated but blocked by
    something like a captive portal (worth giving the bridge its grace
    window for) -- these need different handling, not the same 10-minute
    wait either way."""
    if not _target_ssid:
        return False
    try:
        result = _nmcli(['-t', '-f', 'NAME,DEVICE', 'connection', 'show', '--active'], timeout=10)
        for line in result.stdout.splitlines():
            parts = line.split(':')
            if len(parts) >= 2 and parts[0] == _target_ssid and parts[1] == sta_iface():
                return True
    except Exception:
        pass
    return False


def try_known_networks() -> bool:
    """Scan, then attempt every already-known network in range, strongest
    signal first, before ever touching AP mode. This exists because
    start_ap()'s use of "device disconnect" suppresses NetworkManager's own
    native autoconnect every time it runs -- from here on, reconnecting to
    a familiar network has to be driven explicitly rather than left to NM,
    or it simply won't happen on its own."""
    scan_and_cache_networks()
    for n in [x for x in _cached_networks if x.get("known")]:
        if connect_to_target(n["ssid"], "", assume_known=True):
            return True
    return False


# ── Connectivity check ───────────────────────────────────────────────────

def probe_internet_reachable(timeout: int = 6) -> bool:
    """A bare HTTPS request to a well-known, always-up host, no auth needed.
    If it completes at all — even a non-204/error response — that proves
    DNS, TLS, and routing all worked against the *correct* host, which a
    captive portal cannot forge for an HTTPS endpoint it doesn't hold a
    certificate for. Any exception (timeout, connection refused, TLS
    failure) means "not actually online yet", which is all this needs to
    know — the bridge is what actually shows a human *why*.

    Deliberately provider-agnostic: this used to hit whichever LLM provider
    kyber_core.py was configured for, which meant a brief LLM-specific
    outage (rate limit, DNS blip on that one host) looked identical to a
    real network problem and could trigger a full AP revert over nothing
    wifi-related. netgw owns wifi and nothing else — whether the LLM itself
    is reachable is kyber_core's problem to handle per-turn, same as any
    other STT/LLM failure, never a reason to touch the wifi connection."""
    try:
        requests.get("https://www.google.com/generate_204", timeout=timeout)
        return True
    except requests.RequestException:
        return False


def _nudge_avahi():
    """Force avahi-daemon to re-publish its address records right away
    instead of waiting on its own interface-change detection timing. A
    restart's first announce after coming back up always carries the mDNS
    cache-flush bit (RFC 6762 S8.3), which tells any client already holding
    a cached <hostname>.local -> old-IP mapping to drop it immediately
    rather than serve it until TTL expiry. A SIGHUP/'reload' reloads
    config/service files but doesn't reliably force that same
    re-probe-and-announce sequence, so a full restart is used instead.
    Requires a NOPASSWD sudoers entry for this exact command -- see deploy
    notes. Checked by returncode, not just exception: a missing/wrong
    sudoers entry makes 'sudo' itself fail and exit non-zero without
    subprocess.run ever raising, so returncode is the only reliable signal
    for that case. Either failure mode is logged and swallowed, never
    fatal to the loop that called it."""
    try:
        result = subprocess.run(['sudo', 'systemctl', 'restart', 'avahi-daemon'],
                                 capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print("[NETGW]: avahi nudge sent", flush=True)
        else:
            print(f"[NETGW]: avahi nudge failed — {result.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"[NETGW]: avahi nudge failed — {e}", flush=True)


def _client_monitor_loop():
    """Runs for the lifetime of the process. Only acts while in a CLIENT-side
    state; sleeps otherwise. This is the thread that turns 'connection
    dropped' or a network requiring sign-in into the same recovery flow as a
    first-time join, without anything else needing to notice."""
    global _grace_until
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        with _state_lock:
            current = _state
        if current not in ("client_checking", "client_online", "bridging"):
            continue

        if probe_internet_reachable():
            if current != "client_online":
                _nudge_avahi()
                _set_state("client_online")
            _grace_until = None
            continue

        # Not reachable. If the wifi link itself is gone, there's no sign-in
        # page to wait for -- skip straight to recovery instead of burning
        # the full 10-minute grace window on a network that isn't even
        # there anymore.
        if not _is_still_associated():
            _grace_until = None
            if not try_known_networks():
                start_ap()
            continue

        # Still associated, just can't get out -- a network requiring
        # sign-in (captive portal). Confirmed architecturally non-viable to
        # solve automatically for the realistic guest-network case (client
        # isolation blocks the phone from ever reaching a bridge tool's own
        # viewer), so this just surfaces the "try your hotspot" message via
        # the "bridging" state rather than launching anything. Still worth
        # the grace window rather than reverting instantly, in case this
        # resolves on its own (e.g. a portal session that was already
        # completed moments ago catching up).
        if _grace_until is None:
            _grace_until = time.time() + GRACE_WINDOW_SECONDS
            _set_state("bridging")
        elif time.time() > _grace_until:
            _grace_until = None
            revert_to_ap("Could not get past the network's sign-in page in time")


def _ap_retry_loop():
    """Runs for the lifetime of the process. While sitting in AP mode with
    nobody actively using the portal, periodically checks whether a
    familiar network has come back into range -- without this, the only
    way out of AP mode is a human opening the portal and picking something,
    which defeats the point for a network that was only ever briefly out
    of range. Skips a retry if the portal was touched recently, so this
    can't pull the AP out from under someone mid-setup."""
    while True:
        time.sleep(AP_RETRY_INTERVAL_SECONDS)
        with _state_lock:
            current = _state
        if current != "ap":
            continue
        if time.time() - _last_portal_activity < AP_RETRY_QUIET_SECONDS:
            continue
        try_known_networks()


# ── Captive-portal trigger for KYBER Connect itself ──────────────────────
# Two layers, deliberately not either/or: RFC 8910 (DHCP option 114) for
# modern clients that support it (iOS 14+, Android 11+), and the classic
# DNS-wildcard + redirect trick everything else still falls back to. See
# do_GET's handling of the well-known OS probe paths below.

def write_dnsmasq_snippet():
    portal_url = f"http://{AP_IP_DEFAULT}:{NETGW_PORT}/portal"
    snippet = (
        "# Managed by kyber_netgw.py — do not edit by hand.\n"
        f"address=/#/{AP_IP_DEFAULT}\n"
        f'dhcp-option=114,"{portal_url}"\n'
    )
    try:
        subprocess.run(
            ['sudo', 'tee', DNSMASQ_SNIPPET_PATH],
            input=snippet, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[NETGW]: could not write dnsmasq snippet — {e.stderr}", flush=True)


# OS captive-portal probe paths this needs to recognize and answer in a way
# that triggers the native popup, rather than letting the probe succeed.
_OS_PROBE_PATHS = (
    '/hotspot-detect.html',        # Apple
    '/generate_204',                # Android
    '/connecttest.txt',             # Windows NCSI
)

_PORTAL_PAGE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Connect</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@500;600;700&family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --void: #051222; --deep: #0d1726; --edge: #394D6B; --dim: #1B3048;
    --blue: #00a8ff; --text: #eef1f7; --muted: #8FAEC1;
    --gold-light: #C79C72; --gold-dark: #7C573D; --gold-text: #ECDBC5;
    --gold-border-light: #F1D3B6; --gold-border-dark: #907060;
    --font-head: 'Quicksand', sans-serif; --font-mono: 'Space Mono', monospace;
  }
  body { margin:0; min-height:100vh; background: radial-gradient(circle at 50% -10%, var(--deep) 0%, var(--void) 55%);
         color:var(--text); font-family: var(--font-head); display:flex; align-items:center; justify-content:center; }
  .card { width:90%; max-width:360px; padding:26px 22px; border-radius:16px; border:1px solid var(--edge);
          background: radial-gradient(circle at 50% -10%, var(--deep) 0%, var(--void) 55%); }
  h1 { font-family: var(--font-head); font-size:17px; font-weight:700; letter-spacing:0.04em;
       text-transform:uppercase; color:var(--gold-text); text-align:center; margin:0 0 14px; }
  .wq { font-size:13px; color:var(--muted); text-align:center; margin:0 0 20px; line-height:1.6; }
  label { display:block; font-size:12px; font-weight:600; color:var(--muted); margin:14px 0 6px; }
  input { width:100%; box-sizing:border-box; padding:12px 14px; border-radius:10px; border:1px solid var(--edge);
          background:rgba(255,255,255,0.04); color:var(--text); font-family: var(--font-mono); font-size:14px; outline:none; }
  input:focus { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(0,168,255,0.15); }
  input::placeholder { color: var(--muted); }
  button { width:100%; margin-top:14px; border:1px solid transparent; border-radius:12px; padding:14px 20px;
          font-family: var(--font-head); font-weight:700; font-size:14px; cursor:pointer; color:#fff;
          background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
          background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px rgba(0,5,16,0.5); }
  button:hover { background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
          color: var(--gold-text); }
  .choice-row { display:flex; flex-direction:column; gap:12px; }
  .choice-row button { margin-top:0; }
  .err { color:#E63946; font-size:12px; margin-top:10px; min-height:14px; text-align:center; }
  .netlist { display:flex; flex-direction:column; gap:6px; max-height:280px; overflow-y:auto; margin-bottom:6px; }
  .netrow { background:rgba(255,255,255,0.04); border:1px solid var(--edge); border-radius:10px; padding:10px 12px; }
  .netrow-top { display:flex; align-items:center; gap:8px; }
  .netname { flex:1; font-size:13px; font-family: var(--font-mono); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .netbtn { width:auto; margin-top:0; padding:7px 12px; font-size:11px; flex-shrink:0; }
  .netbtn.forget { background-image:none; background:transparent; border:1px solid #E63946; color:#E63946; }
  .netbtn.forget:hover { background-image:none; background:#E63946; color:#fff; }
  .netpw { display:none; margin-top:8px; }
  .netpw.show { display:block; }
  .netpw button { margin-top:6px; }
  .manual-link { display:block; text-align:center; font-size:12px; color:var(--muted); margin-top:14px; cursor:pointer; }
  .manual-form { display:none; margin-top:14px; }
  .manual-form.show { display:block; }
  .empty-note { font-size:12px; color:var(--muted); text-align:center; padding:10px 0; }
  .back-link { font-family: var(--font-mono); font-size:11px; color:var(--muted); margin-bottom:14px; cursor:pointer; }
</style></head>
<body>
<div class="card">

  <div id="screen-choice">
    <h1>Connect KYBER to a network</h1>
    <p class="wq">How are you going to connect to the Internet?</p>
    <div class="choice-row">
      <button onclick="showScreen('hotspot')">Personal Hotspot</button>
      <button onclick="showScreen('network')">Network Wifi</button>
    </div>
  </div>

  <div id="screen-hotspot" style="display:none">
    <div class="back-link" onclick="showScreen('choice')">&larr; Back</div>
    <h1>Personal hotspot</h1>
    <p class="wq">Please enter in your hotspot credentials, connect, and then proceed to activate your Hotspot if you haven't already.</p>
    <form id="hotspotForm">
      <label>Network name</label>
      <input id="hsSsid" autocomplete="off" required>
      <label>Password</label>
      <input id="hsPsk" type="password" autocomplete="off">
      <button type="submit">Connect</button>
    </form>
    <div class="err" id="hsErr"></div>
  </div>

  <div id="screen-network" style="display:none">
    <div class="back-link" onclick="showScreen('choice')">&larr; Back</div>
    <h1>Network wifi</h1>
    <p class="wq">Unfortunately, KYBER is not compatible with networks that require additional sign ins (common at cafes, airports, hotels). Please use your personal hotspot instead.</p>
    <div class="netlist" id="netlist"><div class="empty-note">Looking for nearby networks...</div></div>
    <div class="err" id="err"></div>
    <div class="manual-link" id="manualLink" onclick="document.getElementById('manualForm').classList.add('show'); this.style.display='none'">Don't see your network? Enter it manually</div>
    <form id="manualForm" class="manual-form">
      <label>Network name</label>
      <input id="ssid" autocomplete="off" required>
      <label>Password (leave blank if open)</label>
      <input id="psk" type="password" autocomplete="off">
      <button type="submit">Connect</button>
    </form>
  </div>

</div>
<script>
function showScreen(name) {
  document.getElementById('screen-choice').style.display = name === 'choice' ? 'block' : 'none';
  document.getElementById('screen-hotspot').style.display = name === 'hotspot' ? 'block' : 'none';
  document.getElementById('screen-network').style.display = name === 'network' ? 'block' : 'none';
  if (name === 'network' && !window._networksLoaded) {
    window._networksLoaded = true;
    loadNetworks();
  }
}

async function loadNetworks() {
  try {
    const res = await fetch('/netgw/networks');
    const data = await res.json();
    const list = document.getElementById('netlist');
    if (!data.networks || data.networks.length === 0) {
      list.innerHTML = '<div class="empty-note">No networks found nearby — try entering yours manually below.</div>';
      return;
    }
    list.innerHTML = data.networks.map(function(n, i) {
      const lock = n.open ? '' : ' &#128274;';
      const action = n.known
        ? '<button class="netbtn" onclick="connectKnown(' + i + ')">Connect</button><button class="netbtn forget" onclick="forgetNetwork(' + i + ')">Forget</button>'
        : (n.open
            ? '<button class="netbtn" onclick="connectOpen(' + i + ')">Connect</button>'
            : '<button class="netbtn" onclick="togglePassword(' + i + ')">Connect</button>');
      const pwField = (!n.known && !n.open)
        ? '<div class="netpw" id="pw-' + i + '"><input type="password" id="pwinput-' + i + '" placeholder="Password" autocomplete="off"><button class="netbtn" style="width:100%;margin-top:6px" onclick="connectWithPassword(' + i + ')">Join</button></div>'
        : '';
      return '<div class="netrow" data-ssid="' + n.ssid.replace(/"/g, '&quot;') + '">' +
        '<div class="netrow-top"><span class="netname">' + n.ssid + lock + '</span>' + action + '</div>' + pwField + '</div>';
    }).join('');
    window._networks = data.networks;
  } catch (e) {
    document.getElementById('netlist').innerHTML = '<div class="empty-note">Could not load nearby networks — try entering yours manually below.</div>';
  }
}

function togglePassword(i) {
  document.getElementById('pw-' + i).classList.toggle('show');
}

async function submitConnect(ssid, password, assumeKnown, errElId) {
  const errEl = document.getElementById(errElId);
  errEl.textContent = 'Connecting...';
  try {
    const res = await fetch('/netgw/credentials', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid: ssid, password: password, assume_known: assumeKnown})
    });
    const data = await res.json();
    errEl.textContent = data.ok
      ? 'Joining the network now. This page will stop responding shortly -- that is expected.'
      : (data.error || 'Could not connect.');
  } catch (err) {
    errEl.textContent = 'Could not reach KYBER.';
  }
}

function connectKnown(i) { submitConnect(window._networks[i].ssid, '', true, 'err'); }
function connectOpen(i) { submitConnect(window._networks[i].ssid, '', false, 'err'); }
function connectWithPassword(i) {
  const pw = document.getElementById('pwinput-' + i).value;
  submitConnect(window._networks[i].ssid, pw, false, 'err');
}

async function forgetNetwork(i) {
  const ssid = window._networks[i].ssid;
  await fetch('/netgw/forget', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ssid: ssid})
  });
  loadNetworks();
}

document.getElementById('manualForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const ssid = document.getElementById('ssid').value.trim();
  const psk = document.getElementById('psk').value;
  submitConnect(ssid, psk, false, 'err');
});

document.getElementById('hotspotForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const ssid = document.getElementById('hsSsid').value.trim();
  const psk = document.getElementById('hsPsk').value;
  submitConnect(ssid, psk, false, 'hsErr');
});
</script>
</body></html>"""


# ── Local HTTP API + portal pages ────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str, status: int = 200):
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == '/netgw/status':
            self._json(get_status())
            return

        if path == '/netgw/networks':
            self._json({"networks": _cached_networks})
            return

        if path == '/netgw/known_networks':
            # Deliberately not the scan cache (_cached_networks), which only
            # refreshes when the Pi transitions into AP mode -- that could be
            # a very long time from now, or never again, if the Pi's been
            # sitting happily on a known network since. This is just
            # "nmcli connection show" read fresh on every call (cheap, no
            # scan/rescan involved), so Forget/Connect are reflected the
            # instant they actually happen instead of on the next AP visit.
            names = sorted(n for n in _known_wifi_profiles() if n != AP_CONNECTION_NAME)
            self._json({"networks": names})
            return

        if path == '/netgw/check':
            self._json({"reachable": probe_internet_reachable()})
            return

        if path in _OS_PROBE_PATHS:
            # Deliberately NOT the expected "no portal here" response —
            # this is what makes the phone's OS pop its captive-portal
            # browser pointed at /portal, the classic-detection half of
            # the two-layer trigger described above.
            self.send_response(302)
            self.send_header('Location', '/portal')
            self.end_headers()
            return

        if path == '/portal' or path == '/':
            global _last_portal_activity
            _last_portal_activity = time.time()
            self._html(_PORTAL_PAGE)
            return

        self._html('<h1>Not found</h1>', status=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length).decode()) if length else {}
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad request body"}, status=400)
            return

        if path == '/netgw/credentials':
            global _last_portal_activity
            _last_portal_activity = time.time()
            ssid = (data.get('ssid') or '').strip()
            password = data.get('password') or ''
            assume_known = bool(data.get('assume_known'))
            if not ssid:
                self._json({"ok": False, "error": "Network name required"}, status=400)
                return
            threading.Thread(target=connect_to_target, args=(ssid, password, assume_known), daemon=True).start()
            self._json({"ok": True})
            return

        if path == '/netgw/forget':
            ssid = (data.get('ssid') or '').strip()
            if not ssid:
                self._json({"ok": False, "error": "Network name required"}, status=400)
                return
            result = _nmcli(['connection', 'delete', ssid])
            if result.returncode != 0:
                self._json({"ok": False, "error": result.stderr.strip()}, status=400)
                return
            for n in _cached_networks:
                if n["ssid"] == ssid:
                    n["known"] = False
            self._json({"ok": True})
            return

        if path == '/netgw/revert':
            delete_profile = data.get('delete_profile', True)
            reason = "Manual reconnect requested" if delete_profile else "Rescan requested"
            threading.Thread(target=revert_to_ap, args=(reason, delete_profile), daemon=True).start()
            self._json({"ok": True})
            return

        self._json({"ok": False, "error": "not found"}, status=404)

    def log_message(self, format, *args):
        pass  # quiet by default; status is available via /netgw/status


def main():
    threading.Thread(target=_client_monitor_loop, daemon=True).start()
    threading.Thread(target=_ap_retry_loop, daemon=True).start()

    # Start in whichever state actually matches reality rather than always
    # assuming AP — if this process restarts while already happily on a
    # target network, don't yank the radio back to provisioning mode.
    # A familiar network gets a real attempt before AP is ever touched --
    # start_ap()'s use of "device disconnect" suppresses NetworkManager's
    # own autoconnect, so from boot onward reconnecting has to be driven
    # explicitly or it simply won't happen on its own.
    if probe_internet_reachable():
        global _target_ssid
        _target_ssid = _current_connection_name()
        _set_state("client_online")
    elif not try_known_networks():
        start_ap()

    server = ThreadingHTTPServer(('0.0.0.0', NETGW_PORT), _Handler)
    print(f"[NETGW]: listening on :{NETGW_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
