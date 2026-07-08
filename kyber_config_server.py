#!/usr/bin/env python3
"""
Droid Configuration Server
Runs on port 5001 — always-on web UI for API keys and personality selection
"""

import os
import subprocess
import socket
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from dotenv import dotenv_values

# ── Dynamic paths ────────────────────────────────────────────────────────────
HOME        = os.path.expanduser('~')
PROJECT_DIR = os.path.join(HOME, 'kyber')
ENV_PATH    = os.path.join(PROJECT_DIR, '.env')
MAP_DIR     = os.path.join(PROJECT_DIR, 'personality_maps')
PORT        = 5001
MAPPER_PORT = 5000

# ── Logic Core Connections — curated STT / LLM provider lists ───────────────
# Each tuple: (value, display label, .env key it reads/writes, placeholder,
# field note shown under the input). Groq and OpenAI appear on BOTH lists
# but share a single underlying key — see SHARED_PROVIDER_KEYS below, which
# render_logic_core_panel() uses to collapse them to one rendered field
# instead of two separate inputs writing to the same env var.
STT_PROVIDERS = [
    ("deepgram",   "Deepgram",       "DEEPGRAM_API_KEY",   "...",
     'Free credit at <a href="https://console.deepgram.com" target="_blank">console.deepgram.com</a>'),
    ("groq",       "Groq Whisper",   "GROQ_API_KEY",       "gsk_...",
     'Free at <a href="https://console.groq.com" target="_blank">console.groq.com</a> — used by whichever engine(s) above are set to Groq'),
    ("openai",     "OpenAI Whisper", "OPENAI_API_KEY",     "sk-...",
     'From <a href="https://platform.openai.com" target="_blank">platform.openai.com</a> — used by whichever engine(s) above are set to OpenAI'),
    ("google",     "Google STT",     "GOOGLE_STT_API_KEY", "...",
     'Needs a Google Cloud project — a bit more setup than the others'),
    ("assemblyai", "AssemblyAI",     "ASSEMBLYAI_API_KEY", "...",
     'Free credits at <a href="https://www.assemblyai.com" target="_blank">assemblyai.com</a>'),
]
LLM_PROVIDERS = [
    ("gemini",    "Gemini 2.5 Flash", "GEMINI_API_KEY",    "AIza...",
     'Free at <a href="https://aistudio.google.com" target="_blank">aistudio.google.com</a>'),
    ("openai",    "OpenAI GPT",       "OPENAI_API_KEY",    "sk-...",
     'Same key as OpenAI Whisper above, if both selected'),
    ("anthropic", "Anthropic Claude", "ANTHROPIC_API_KEY", "sk-ant-...",
     'From <a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a>'),
    ("groq",      "Groq",             "GROQ_API_KEY",      "gsk_...",
     'Same key as Groq Whisper above, if both selected'),
]
SHARED_PROVIDER_KEYS = {"groq", "openai"}


def render_logic_core_panel(vals: dict) -> str:
    """Renders the Logic Core Connections panel body — STT + LLM provider
    dropdowns, each revealing only the API key field for whichever provider
    is currently selected. Groq and OpenAI are shared between both lists,
    so their key field is rendered once and shown whenever EITHER dropdown
    points at them — never two inputs fighting over the same env var."""
    active_stt = vals.get("STT_PROVIDER", "deepgram")
    active_llm = vals.get("LLM_PROVIDER", "gemini")

    stt_options = ""
    for value, label, *_ in STT_PROVIDERS:
        selected = "selected" if active_stt == value else ""
        stt_options += f'<option value="{value}" {selected}>{label}</option>'

    llm_options = ""
    for value, label, *_ in LLM_PROVIDERS:
        selected = "selected" if active_llm == value else ""
        llm_options += f'<option value="{value}" {selected}>{label}</option>'

    def field_html(group: str, value: str, label: str, env_key: str, placeholder: str, note: str, shown: bool) -> str:
        display = "flex" if shown else "none"
        key_val = vals.get(env_key, "")
        return (
            f'<div data-{group}="{value}" style="display:{display};flex-direction:column;gap:6px;">'
            f'<label class="field-label">{label} API Key</label>'
            f'<div class="input-wrap"><input type="text" name="{env_key}" value="{key_val}" placeholder="{placeholder}" autocomplete="off"></div>'
            f'<p class="field-note">{note}</p>'
            f'</div>'
        )

    stt_fields = ""
    shared_fields = ""
    for value, label, env_key, placeholder, note in STT_PROVIDERS:
        if value in SHARED_PROVIDER_KEYS:
            shown = (active_stt == value or active_llm == value)
            shared_fields += field_html("shared", value, label, env_key, placeholder, note, shown)
        else:
            stt_fields += field_html("stt", value, label, env_key, placeholder, note, active_stt == value)

    llm_fields = ""
    for value, label, env_key, placeholder, note in LLM_PROVIDERS:
        if value not in SHARED_PROVIDER_KEYS:
            llm_fields += field_html("llm", value, label, env_key, placeholder, note, active_llm == value)

    body = f"""<div class="field">
          <label class="field-label">Speech-to-Text Engine</label>
          <div class="identity-select-wrap">
            <select class="identity-select" id="sttProviderSelect" name="STT_PROVIDER" onchange="kyberLogicCoreUpdate()">
              {stt_options}
            </select>
            <span class="identity-chevron">&#9662;</span>
          </div>
        </div>
        {stt_fields}
        {shared_fields}
        <hr class="divider">
        <div class="field">
          <label class="field-label">AI Personality Engine</label>
          <div class="identity-select-wrap">
            <select class="identity-select" id="llmProviderSelect" name="LLM_PROVIDER" onchange="kyberLogicCoreUpdate()">
              {llm_options}
            </select>
            <span class="identity-chevron">&#9662;</span>
          </div>
        </div>
        {llm_fields}"""

    script = """<script>
        function kyberLogicCoreUpdate() {
          var stt = document.getElementById('sttProviderSelect').value;
          var llm = document.getElementById('llmProviderSelect').value;
          document.querySelectorAll('[data-stt]').forEach(function(el) {
            el.style.display = (el.getAttribute('data-stt') === stt) ? 'flex' : 'none';
          });
          document.querySelectorAll('[data-llm]').forEach(function(el) {
            el.style.display = (el.getAttribute('data-llm') === llm) ? 'flex' : 'none';
          });
          document.querySelectorAll('[data-shared]').forEach(function(el) {
            var p = el.getAttribute('data-shared');
            el.style.display = (stt === p || llm === p) ? 'flex' : 'none';
          });
        }
        </script>"""

    return body + script


def read_env() -> dict:
    vals = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    return {
        "GEMINI_API_KEY":       vals.get("GEMINI_API_KEY",       ""),
        "DEEPGRAM_API_KEY":     vals.get("DEEPGRAM_API_KEY",     ""),
        "GROQ_API_KEY":         vals.get("GROQ_API_KEY",         ""),
        "OPENAI_API_KEY":       vals.get("OPENAI_API_KEY",       ""),
        "GOOGLE_STT_API_KEY":   vals.get("GOOGLE_STT_API_KEY",   ""),
        "ASSEMBLYAI_API_KEY":   vals.get("ASSEMBLYAI_API_KEY",   ""),
        "ANTHROPIC_API_KEY":    vals.get("ANTHROPIC_API_KEY",    ""),
        "STT_PROVIDER":         vals.get("STT_PROVIDER",         "deepgram"),
        "LLM_PROVIDER":         vals.get("LLM_PROVIDER",         "gemini"),
        "ACTIVE_PERSONALITY":   vals.get("ACTIVE_PERSONALITY",   "1"),
        "ACTIVE_SOUND_PROFILE": vals.get("ACTIVE_SOUND_PROFILE", "1"),
        "DROID_NAME":           vals.get("DROID_NAME",           ""),
        "DROID_TYPE":           vals.get("DROID_TYPE",           "R"),
        "DROID_MAC":            vals.get("DROID_MAC",            ""),
        "BT_COMLINK_MAC":       vals.get("BT_COMLINK_MAC",       ""),
        "MIC_SETUP_SKIPPED":    vals.get("MIC_SETUP_SKIPPED",    ""),
        "MODEL_CONFIRMED":      vals.get("MODEL_CONFIRMED",      ""),
        "PERSONALITY_CONFIRMED": vals.get("PERSONALITY_CONFIRMED", ""),
        "ACTIVATION_CONFIRMED": vals.get("ACTIVATION_CONFIRMED", ""),
    }


def sanitize_droid_name(raw: str) -> str:
    """Strip anything that could break JSON, an LLM prompt, or a Python f-string.
    Caps at 10 characters. Letters, numbers, spaces, dashes, and basic punctuation only.
    Requires at least one letter or number — a name of just dashes/periods/spaces
    (e.g. someone typing the literal '##-###' placeholder) is treated as no name at all,
    since it would render as broken-looking fragments like "-'s IP" elsewhere in the UI."""
    if not raw:
        return ""
    raw = raw.strip()[:10]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.")
    cleaned = "".join(ch for ch in raw if ch in allowed).strip()
    has_alphanumeric = any(ch.isalnum() for ch in cleaned)
    return cleaned if has_alphanumeric else ""


def display_droid_name(raw: str) -> str:
    """Return the saved designation if one exists, otherwise fall back to
    the generic 'your droid' for UI text that hasn't been personalized yet."""
    clean = sanitize_droid_name(raw)
    return clean if clean else "your droid"


def write_env(keys: dict):
    existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    existing.update({k: v for k, v in keys.items() if v})
    with open(ENV_PATH, 'w') as f:
        for k, v in existing.items():
            f.write(f'{k}={v}\n')


# Single source of truth for the top tab bar -- every page passes its own
# key in as `active` and calls render_nav(active) rather than each page
# carrying its own copy of this markup. (key, href, icon, label)
NAV_TABS = [
    ("mainframe",   "/",            "ti-settings",       "Mainframe"),
    ("protocols",   "/protocols",   "ti-list-details",   "Subroutines"),
    ("controls",    "/controls",    "ti-arrows-move",    "Gestures"),
    ("bluetooth",   "/bluetooth",   "ti-bluetooth",       "Bluetooth"),
    ("calibration", "/calibration", "ti-adjustments",    "Calibration"),
    ("reset",       "/reset",       "ti-refresh",        "Reset"),
]


def render_nav(active: str) -> str:
    links = []
    for key, href, icon, label in NAV_TABS:
        cls = "tab-link active" if key == active else "tab-link"
        links.append(f'<a class="{cls}" href="{href}"><i class="ti {icon}" aria-hidden="true"></i> {label}</a>')
    return '<nav class="tab-nav">\n    ' + '\n    '.join(links) + '\n  </nav>'


def is_setup_complete() -> bool:
    vals = read_env()
    stt_key_map = {value: env_key for value, _, env_key, *_ in STT_PROVIDERS}
    llm_key_map = {value: env_key for value, _, env_key, *_ in LLM_PROVIDERS}
    stt_env_key = stt_key_map.get(vals["STT_PROVIDER"], "DEEPGRAM_API_KEY")
    llm_env_key = llm_key_map.get(vals["LLM_PROVIDER"], "GEMINI_API_KEY")
    return bool(vals.get(stt_env_key) and vals.get(llm_env_key))


# ── First-run onboarding wizard ──────────────────────────────────────────────
# Locked model IDs the wizard's droid picker writes — same ids
# PERSONALITY_DEFAULT_NAMES already uses, plus bd1 reserved for when that
# profile exists. Picking a model writes ACTIVE_PERSONALITY to one of these
# id strings specifically (never a plain digit, which is what a custom slot
# looks like) — that's what makes "has the wizard's model step run" a safe,
# unambiguous check against real state instead of needing its own tracker.
WIZARD_MODEL_CHASSIS = {"r2d2": "R", "bb8": "BB", "chopper": "C", "bd1": "BD", "aseries": "A"}

# Stages the breadcrumb trail covers, in order. "activation" is deliberately
# excluded -- it's presentational and unlabeled, with no form to revisit.
WIZARD_BREADCRUMB_STAGES = ["welcome", "mic", "keys", "claim", "ready"]


def wizard_reached_idx(natural_stage: str) -> int:
    """How far back it's actually safe to edit from, given where the wizard
    truly is right now. Once a droid has been claimed (Activation, Ready, or
    fully complete), Mic/Keys/Claim are deliberately excluded: each of those
    pages calls stop_kyber_service() when rendered, which would kill a live
    connection attempt -- or an already-working droid -- if revisited via a
    breadcrumb or Start Over. Only Identity stays reachable that far in;
    Mic/Keys/Claim are always still properly editable from the Mainframe
    once onboarding is done."""
    if natural_stage in ("activation", "ready", "complete"):
        return 0
    if natural_stage in WIZARD_BREADCRUMB_STAGES:
        return WIZARD_BREADCRUMB_STAGES.index(natural_stage) - 1
    return -1


def render_wizard_breadcrumbs(viewing_stage: str, natural_stage: str, wizard_path: str) -> str:
    """Breadcrumb dots for the onboarding wizard. viewing_stage is the page
    actually being rendered (possibly an earlier one, via ?edit=); natural_stage
    is what current_setup_stage() truly derives from the saved flags right
    now -- used only to figure out how far the trail has *really* reached,
    so a dot doesn't look reachable before its own step is actually done.

    Dots for stages already reached are real links back via ?edit=<stage> --
    revisiting and resubmitting an earlier page never loses later progress,
    since current_setup_stage() always derives forward from the real saved
    flags; it just lands back wherever the person actually left off after.
    Stages not yet reached render as plain unlinked dots."""
    if viewing_stage not in WIZARD_BREADCRUMB_STAGES:
        return ""
    reached_idx = wizard_reached_idx(natural_stage)
    viewing_idx = WIZARD_BREADCRUMB_STAGES.index(viewing_stage)
    parts = []
    for i, stg in enumerate(WIZARD_BREADCRUMB_STAGES):
        if i == viewing_idx:
            parts.append('<span class="wizard-crumb-dot active"></span>')
        elif i <= reached_idx:
            parts.append(
                f'<a class="wizard-crumb-dot past" href="/?edit={stg}&path={wizard_path or "yes"}" '
                f'aria-label="Back to {stg}"></a>'
            )
        else:
            parts.append('<span class="wizard-crumb-dot"></span>')
        if i < len(WIZARD_BREADCRUMB_STAGES) - 1:
            parts.append('<span class="wizard-crumb-line"></span>')
    return '<div class="wizard-crumbs">' + "".join(parts) + '</div>'
# The reverse direction, used only to suggest a starting point on the
# Personality step -- never written anywhere, purely a UI default.
CHASSIS_DEFAULT_PERSONALITY = {"R": "neutral", "BB": "neutral", "C": "neutral", "BD": "neutral", "A": "neutral"}


def onboarding_complete() -> bool:
    """Single source of truth: the explicit flag, written once by
    /setup/finish at the end of the Ready screen. No shortcut here on
    purpose — current_setup_stage() below is what walks an install through
    wifi/claim/mic/ready from wherever it actually is, including an
    already-working install that's never seen these specific screens
    before. That install sails through wifi/claim instantly (already
    satisfied) and only has to dismiss Mic (skip) and Ready (confirm) once."""
    raw = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    return raw.get("ONBOARDING_COMPLETE") == "1"


def current_setup_stage() -> str:
    """Where a not-yet-onboarded install currently is. Derived from real
    state rather than tracked separately, so a refresh or a return visit
    days later always lands in the right place — there's nothing to fall
    out of sync with.

    Order: Identity (model+name+personality, one combined page) -> Mic ->
    Keys -> Claim -> Activation (presentational, unlabeled) -> Ready.
    Wifi's own check is deliberately not in this chain anymore —
    kyber_netgw.py's captive portal handles first-contact wifi now, so by
    the time anyone reaches the Mainframe at all, wifi already works one
    way or another. The Wifi page and its handler are untouched and still
    reachable directly if ever needed; this chain just never routes to it.

    MODEL_CONFIRMED and PERSONALITY_CONFIRMED are explicit flags rather
    than inferred from ACTIVE_PERSONALITY/DROID_TYPE on purpose: those
    fields always have a default value, so "is it set" can never tell
    "chosen on purpose" apart from "never touched, sitting at default" —
    the exact shape of bug that bit the old DROID_MAC/ACTIVE_PERSONALITY
    checks before they got their own real signals. Both are checked
    together now since Model and Personality live on one combined page.

    ACTIVATION_CONFIRMED is set by the activation page's own JS once its
    readiness poll succeeds (or the person waits it out), and is reset to
    blank by every fresh droid_claim() -- so re-claiming a droid (including
    after a soft Start Over) always shows the activation page again before
    Ready, exactly like a first-time claim would."""
    vals = read_env()
    if not (vals.get("MODEL_CONFIRMED") and vals.get("PERSONALITY_CONFIRMED")):
        return "welcome"
    if not (vals.get("BT_COMLINK_MAC") or vals.get("MIC_SETUP_SKIPPED") == "1"):
        return "mic"
    if not is_setup_complete():
        return "keys"
    if not vals.get("DROID_MAC"):
        return "claim"
    if not vals.get("ACTIVATION_CONFIRMED"):
        return "activation"
    return "ready"


def stop_kyber_service():
    try:
        subprocess.run(['sudo', 'systemctl', 'stop', 'kyber.service'], check=True)
    except Exception:
        pass


def restart_kyber_service() -> bool:
    try:
        subprocess.Popen(['sudo', 'systemctl', 'restart', 'kyber.service'])
        return True
    except Exception:
        return False


def get_service_status() -> str:
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'kyber.service'],
            capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except Exception:
        return False


def start_sound_mapper():
    """Start the sound discovery server as a background process with full environment."""
    venv_python = os.path.join(PROJECT_DIR, 'venv', 'bin', 'python3')
    mapper_script = os.path.join(PROJECT_DIR, 'sound_discovery_server.py')
    env = os.environ.copy()
    env['HOME'] = HOME
    env['PATH'] = f"{os.path.join(PROJECT_DIR, 'venv', 'bin')}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    subprocess.Popen(
        [venv_python, mapper_script],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env
    )


PERSONALITY_DEFAULT_NAMES = {"r2d2": "R2-D2", "bb8": "BB-8", "chopper": "Chopper", "bd1": "BD-1", "neutral": "Neutral"}


def _personality_map_path(slot) -> str:
    """slot is either a custom slot number ("1".."5") or a locked default id
    ("r2d2", "bb8", "chopper") — each addresses a different file."""
    if str(slot).isdigit():
        return os.path.join(MAP_DIR, f'personality_{slot}.json')
    return os.path.join(MAP_DIR, f'personality_default_{slot}.json')


def read_personality_name(slot) -> str:
    """Read the name field from a personality JSON (custom slot or locked
    default), falling back to a sensible default."""
    import json
    fallback = PERSONALITY_DEFAULT_NAMES.get(str(slot), f"Personality Profile {slot}")
    map_path = _personality_map_path(slot)
    if os.path.exists(map_path):
        try:
            with open(map_path) as f:
                data = json.load(f)
            return data.get("name", fallback)
        except Exception:
            pass
    return fallback


def read_personality_traits(slot) -> dict:
    """Read the 5 trait values from a personality JSON (custom slot or
    locked default), falling back to neutral (3) for anything missing or
    for a slot that's never been saved at all."""
    import json
    traits = {"brave": 3, "curious": 3, "sassy": 3, "playful": 3, "sensitive": 3}
    map_path = _personality_map_path(slot)
    if os.path.exists(map_path):
        try:
            with open(map_path) as f:
                data = json.load(f)
            saved = data.get("traits", {})
            traits = {k: saved.get(k, v) for k, v in traits.items()}
        except Exception:
            pass
    return traits


def read_sound_profile_name(slot) -> str:
    """Read the name field from a sound profile JSON, falling back to default.
    Sound profiles have no locked defaults — always a custom numbered slot."""
    import json
    map_path = os.path.join(MAP_DIR, f'sound_profile_{slot}.json')
    if os.path.exists(map_path):
        try:
            with open(map_path) as f:
                data = json.load(f)
            return data.get("name", f"Sound Profile {slot}")
        except Exception:
            pass
    return f"Sound Profile {slot}"


def personality_options(active: str) -> str:
    """Render <option> tags for the Personality dropdown — 5 custom slots
    first (flagged unedited until a file exists for them), then the 3
    locked defaults at the bottom."""
    html = '<optgroup label="Custom">'
    for i in range(1, 6):
        value = str(i)
        name = read_personality_name(value)
        if not os.path.exists(_personality_map_path(value)):
            name = f"{name} (unedited)"
        selected = "selected" if str(active) == value else ""
        html += f'<option value="{value}" {selected}>{name}</option>'
    html += '</optgroup><optgroup label="Defaults">'
    for slot_id, display_name in PERSONALITY_DEFAULT_NAMES.items():
        selected = "selected" if str(active) == slot_id else ""
        html += f'<option value="{slot_id}" {selected}>{display_name}</option>'
    html += '</optgroup>'
    return html


def sound_profile_options(active: str) -> str:
    """Render <option> tags for the Sound Profile dropdown — 5 custom slots,
    no locked defaults (sound profiles aren't pre-seeded yet)."""
    html = ""
    for i in range(1, 6):
        value = str(i)
        name = read_sound_profile_name(value)
        if not os.path.exists(os.path.join(MAP_DIR, f'sound_profile_{value}.json')):
            name = f"{name} (unedited)"
        selected = "selected" if str(active) == value else ""
        html += f'<option value="{value}" {selected}>{name}</option>'
    return html


DROID_TYPE_ICONS = {
    "R": "MODEL_ICON_R2D2", "BB": "MODEL_ICON_BB8", "C": "MODEL_ICON_CHOPPER",
    "A": "MODEL_ICON_ASERIES", "BD": "MODEL_ICON_BD1",
}


def droid_type_options(active: str) -> str:
    """Render the droid chassis-type selector as a row of gradient-card chips
    (R / BB / C / A / BD), icon-only -- the icon alone identifies the chassis,
    so no letter label underneath. Save/persistence logic below is unaffected,
    still keyed on the same R/BB/C/A/BD value strings either way."""
    types = ["R", "BB", "C", "A", "BD"]
    html = ""
    for t in types:
        icon_svg = globals()[DROID_TYPE_ICONS[t]]
        checked = "checked" if t == active else ""
        active_class = "droid-type-option active-droid-type" if t == active else "droid-type-option"
        html += f"""
        <label class="{active_class}" title="{t}">
          <input type="radio" name="DROID_TYPE" value="{t}" {checked} onchange="this.closest('form').querySelectorAll('.droid-type-option').forEach(el=>el.classList.remove('active-droid-type')); this.closest('.droid-type-option').classList.add('active-droid-type')">
          <span class="droid-type-icon">{icon_svg}</span>
        </label>"""
    return html


# ── Shared CSS (no external font imports) ────────────────────────────────────
SHARED_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Quicksand:wght@500;600;700&family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --void: #051222; --deep: #0d1726; --panel: #203955; --edge: #394D6B;
    --blue: #00a8ff; --glow: #0066cc; --dim: #1B3048; --text: #eef1f7;
    --muted: #8FAEC1; --success: #2ecc71; --warning: #ffc857; --error: #e63946;
    --select: #9d4edd; --shadow: rgba(0,5,16,0.5);
    --gold-light: #C79C72; --gold-dark: #7C573D; --gold-text: #ECDBC5;
    --gold-border-light: #F1D3B6; --gold-border-dark: #907060;
    --font-head: 'Quicksand', sans-serif;
    --font-body: 'Quicksand', sans-serif;
    --font-mono: 'Space Mono', monospace;
  }
  html, body { min-height: 100vh; background: radial-gradient(circle at 50% -10%, var(--deep) 0%, var(--void) 55%); color: var(--text); font-family: var(--font-body); font-size: 14px; line-height: 1.6; }
  body::before {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image:
      radial-gradient(1px 1px at 10% 15%, rgba(255,255,255,0.6) 0%, transparent 100%),
      radial-gradient(1px 1px at 25% 40%, rgba(255,255,255,0.4) 0%, transparent 100%),
      radial-gradient(1px 1px at 70% 25%, rgba(255,255,255,0.6) 0%, transparent 100%),
      radial-gradient(1px 1px at 90% 45%, rgba(255,255,255,0.5) 0%, transparent 100%),
      radial-gradient(1px 1px at 55% 80%, rgba(255,255,255,0.3) 0%, transparent 100%);
  }
  .wrap { position: relative; z-index: 1; max-width: 560px; margin: 0 auto; padding: 40px 20px 60px; }
  .header { text-align: center; margin-bottom: 40px; }
  .r2-icon { width: 72px; height: 72px; margin: 0 auto 20px; animation: pulse-r2 2.2s ease-in-out infinite alternate; }
  .r2-icon svg { width: 100%; height: 100%; filter: drop-shadow(0 0 12px rgba(236,219,197,0.45)); }
  @keyframes pulse-r2 { from { filter: drop-shadow(0 0 6px rgba(236,219,197,0.35)); } to { filter: drop-shadow(0 0 16px rgba(236,219,197,0.8)); } }
  h1 { font-family: var(--font-head); font-size: 24px; font-weight: 700; letter-spacing: 0.05em; color: var(--gold-text); text-shadow: 0 0 16px rgba(236,219,197,0.3); text-transform: uppercase; }
  .subtitle { margin-top: 8px; color: var(--muted); font-size: 12px; letter-spacing: 0.04em; font-family: var(--font-mono); }
  .secondary-header { margin-top: 14px; color: var(--text); font-size: 15px; font-family: var(--font-head); font-weight: 600; }
  .panel { position: relative; border: 1px solid transparent; border-radius: 16px; overflow: hidden; margin-bottom: 16px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .panel-header { padding: 14px 20px; font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); border-bottom: 1px solid rgba(143,174,193,0.3); }
  .panel-body { padding: 20px; display: flex; flex-direction: column; gap: 18px; font-size: 12px; }
  .field { display: flex; flex-direction: column; gap: 6px; }
  label.field-label { font-size: 12px; font-weight: 600; color: var(--muted); }
  .field-note { font-size: 12px; color: var(--muted); margin-top: 2px; line-height: 1.5; }
  .field-note a { color: var(--blue); text-decoration: none; }
  .input-wrap { position: relative; }
  input[type="text"], input[type="password"] { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 12px 14px; color: var(--text); font-family: var(--font-mono); font-size: 14px; transition: border-color 0.2s; outline: none; }
  input:focus { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(0,168,255,0.15); }
  input::placeholder { color: var(--muted); }
  .divider { border: none; border-top: 1px solid var(--edge); margin: 4px 0; }
  .btn-row { display: flex; gap: 12px; margin-top: 8px; }
  button.primary, .btn-go {
    flex: 1; border: 1px solid transparent; border-radius: 12px; padding: 14px 20px;
    font-family: var(--font-head); font-size: 14px; font-weight: 700; letter-spacing: 0.02em; cursor: pointer; color: #fff;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); transition: none;
  }
  button.primary:hover, .btn-go:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    background-origin: border-box; background-clip: padding-box, border-box; color: var(--gold-text);
  }
  button.secondary { flex: 1; background: transparent; color: var(--muted); border: 1px solid var(--edge); border-radius: 12px; padding: 14px 20px; font-family: var(--font-head); font-size: 14px; font-weight: 600; cursor: pointer; transition: border-color 0.2s, color 0.2s; }
  button.secondary:hover { border-color: var(--blue); color: var(--blue); }
  .alert { border-radius: 10px; padding: 12px 16px; font-size: 12px; margin-bottom: 16px; }
  .alert.success { background: rgba(46,204,113,0.1); border: 1px solid var(--success); color: var(--success); }
  .alert.error { background: rgba(230,57,70,0.1); border: 1px solid var(--error); color: var(--error); }
  .ti { color: var(--muted); }
  .footer { text-align: center; margin-top: 40px; font-size: 12px; color: var(--muted); font-family: var(--font-mono); line-height: 1.6; }
  /* Restart overlay */
  .overlay { display: none; position: fixed; inset: 0; background: rgba(5,18,34,0.88); z-index: 100; align-items: center; justify-content: center; }
  .overlay.show { display: flex; }
  .overlay-box { position: relative; border: 1px solid transparent; border-radius: 16px; padding: 36px 28px; max-width: 360px; width: 90%; text-align: center;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .overlay-box h2 { font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); margin-bottom: 16px; }
  .overlay-box p { font-size: 13px; color: var(--text); line-height: 1.8; margin-bottom: 28px; }
  .countdown-ring { position: relative; width: 90px; height: 90px; margin: 0 auto 16px; display: none; }
  .countdown-ring.show { display: block; }
  .countdown-ring svg { transform: rotate(-90deg); }
  .countdown-ring circle.track { fill: none; stroke: var(--edge); stroke-width: 4; }
  .countdown-ring circle.fill { fill: none; stroke: var(--blue); stroke-width: 4; stroke-dasharray: 251; stroke-dashoffset: 251; stroke-linecap: round; transition: stroke-dashoffset 1s linear; }
  .countdown-num { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); font-size: 24px; font-family: var(--font-head); color: var(--text); font-weight: 700; }
  .overlay-status { font-size: 12px; color: var(--muted); font-family: var(--font-mono); margin-bottom: 24px; min-height: 18px; display: none; }
  .overlay-status.show { display: block; }
  .btn-overlay-ok {
    width: 100%; border: 1px solid transparent; border-radius: 12px; padding: 14px 20px;
    font-family: var(--font-head); font-size: 14px; font-weight: 700; cursor: pointer; color: #fff;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-overlay-ok:disabled { opacity: 0.4; cursor: default; }
  .btn-overlay-ok:not(:disabled):hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  /* Onboarding wizard — Welcome / Model picker / Handoff */
  .wizard-question { font-family: var(--font-body); font-size: 14px; color: var(--muted); text-align: center; margin: 0 0 24px; line-height: 1.6; }
  .wizard-choice-row { display: flex; flex-direction: column; gap: 12px; }
  .btn-wizard-choice {
    display: block; width: 100%; border-radius: 12px; padding: 16px; text-align: center; text-decoration: none; box-sizing: border-box;
    font-family: var(--font-head); font-size: 15px; font-weight: 700; cursor: pointer; color: #fff;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-wizard-choice:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .model-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 8px; }
  .model-grid > *:nth-child(5):last-child { grid-column: 1 / -1; }
  @media (max-width: 480px) { .model-grid { grid-template-columns: 1fr; } .model-grid > *:nth-child(5):last-child { grid-column: auto; } }
  .model-card {
    display: block; border-radius: 14px; padding: 22px 14px; text-align: center; cursor: pointer; width: 100%;
    border: 1px solid transparent; font-family: var(--font-head); font-size: 14px; font-weight: 700; color: var(--text);
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .model-card:hover:not(:disabled) {
    background-image: linear-gradient(135deg, var(--gold-light) 0%, var(--gold-dark) 100%), linear-gradient(135deg, var(--gold-light), var(--gold-dark));
    color: var(--gold-text);
  }
  .model-card.active-model {
    background-image: linear-gradient(135deg, var(--gold-light) 0%, var(--gold-dark) 100%), linear-gradient(135deg, var(--gold-light), var(--gold-dark));
    color: var(--gold-text);
  }
  .model-card:disabled { opacity: 0.35; cursor: not-allowed; }
  .model-card .model-icon { display: block; width: 56px; height: 56px; margin: 0 auto 8px; }
  .model-card .model-icon svg { width: 100%; height: 100%; }
  .model-card .model-note { display: block; font-family: var(--font-body); font-weight: 400; font-size: 10px; color: var(--muted); margin-top: 5px; letter-spacing: 0.04em; text-transform: uppercase; }
  .wizard-step { text-align: center; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.15em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
  .wizard-crumbs { display: flex; align-items: center; margin: 0 0 20px; }
  .wizard-crumb-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--edge); display: block; }
  .wizard-crumb-dot.active { background: var(--blue); }
  .wizard-crumb-dot.past { background: var(--success); cursor: pointer; }
  .wizard-crumb-line { flex: 1; height: 1px; background: var(--edge); }
  .wizard-back-link { display: block; text-align: center; font-size: 12px; color: var(--muted); text-decoration: none; margin-top: 14px; }
  .wizard-back-link:hover { color: var(--blue); }
  .handoff-list { font-size: 13px; color: var(--text); line-height: 2; margin: 0 0 24px; padding-left: 4px; list-style: none; }
  .handoff-list li:before { content: "▸ "; color: var(--gold-text); }
  .handoff-list a { color: var(--blue); text-decoration: none; font-weight: 600; }
  /* Wizard steps reusing Bluetooth Manager / Network panel styling (Wifi, Claim, Mic) —
     duplicated here rather than moved, so the original Bluetooth Manager and Network
     pages are untouched and can't regress from this change. */
  .net-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid rgba(143,174,193,0.18); }
  .net-row:last-child { border-bottom: none; }
  .net-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .net-dot.on { background: var(--success); box-shadow: 0 0 4px var(--success); }
  .net-dot.off { background: var(--muted); }
  .net-ssid { flex: 1; font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .net-ssid.connected { color: var(--success); }
  .btn-net-add {
    width: 100%; margin-top: 10px; border-radius: 10px; padding: 11px;
    font-family: var(--font-head); font-size: 13px; font-weight: 600; cursor: pointer; color: #fff;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-net-add:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .bt-section { border: 1px solid transparent; border-radius: 16px; overflow: hidden; margin-bottom: 16px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .bt-section-header { padding: 14px 20px; font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); border-bottom: 1px solid rgba(143,174,193,0.3); display: flex; align-items: center; justify-content: space-between; }
  .bt-device { padding: 14px 20px; border-bottom: 1px solid rgba(143,174,193,0.18); display: flex; align-items: center; gap: 12px; }
  .bt-device:last-child { border-bottom: none; }
  .bt-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .bt-dot.connected { background: var(--success); box-shadow: 0 0 6px var(--success); }
  .bt-dot.disconnected { background: var(--muted); }
  .bt-info { flex: 1; min-width: 0; }
  .bt-name { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bt-mac { font-size: 11px; color: var(--muted); margin-top: 2px; font-family: var(--font-mono); }
  .bt-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .btn-scan { background: transparent; color: var(--blue); border: 1px solid var(--blue); border-radius: 8px; padding: 5px 14px; font-family: var(--font-head); font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .btn-scan:hover { background: rgba(0,168,255,0.1); }
  .btn-scan:disabled { border-color: var(--edge); color: var(--muted); cursor: default; background: transparent; }
  .bt-empty { padding: 20px; text-align: center; color: var(--muted); font-size: 12px; }
  .btn-pair { padding: 6px 12px; border-radius: 8px; font-family: var(--font-head); font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.2s; border: 1px solid var(--blue); color: var(--blue); background: transparent; }
  .btn-pair:hover { background: rgba(0,168,255,0.1); }
  .tab-nav { display: grid; grid-template-columns: repeat(6, 1fr); border-radius: 14px; padding: 5px; gap: 3px; margin-bottom: 24px;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  @media (max-width: 480px) {
    .tab-nav { grid-template-columns: 1fr 1fr 1fr; }
  }
  .tab-link { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 9px 4px; border-radius: 10px; font-family: var(--font-head); font-size: 11px; font-weight: 600; color: var(--muted); text-decoration: none; transition: color 0.2s, background 0.2s; gap: 4px; line-height: 1.15; text-align: center; }
  .tab-link:hover { color: var(--gold-text); }
  .tab-link.active { color: var(--gold-text); background: rgba(199,156,114,0.18); }
"""

R2_SVG = """<svg width="100%" height="100%" viewBox="0 0 600 600" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" xml:space="preserve" xmlns:serif="http://www.serif.com/" style="fill-rule:evenodd;clip-rule:evenodd;stroke-linecap:round;stroke-linejoin:round;stroke-miterlimit:1.5;">
    <g id="logo_mobile" transform="matrix(1,0,0,1,-14.4865,4.9984)">
        <g transform="matrix(1,0,0,1,0.585037,0.675305)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        </g>
        <g transform="matrix(1,0,0,1,-284.457,0.675305)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        </g>
        <g transform="matrix(0.427561,0,0,0.427561,262.431,177.22)">
            <circle cx="456.547" cy="308.649" r="33.072" style="stroke:#ECDBC5;stroke-width:14.62px;"/>
        </g>
        <g transform="matrix(0.427561,0,0,0.427561,-22.6108,177.22)">
            <circle cx="456.547" cy="308.649" r="33.072" style="stroke:#ECDBC5;stroke-width:14.62px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,293.205,56.3148)">
            <circle cx="456.547" cy="308.649" r="33.072" style="stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(-0.301909,0,0,0.301909,342.57,67.7304)">
            <circle cx="456.547" cy="308.649" r="33.072" style="stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,371.306,89.6332)">
            <circle cx="456.547" cy="308.649" r="33.072" style="stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,369.128,187.277)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,41.8584,35.6405)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,4.36742,78.8024)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,-16.7306,135.788)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,-22.8366,212.506)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,39.1977,360.789)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <g transform="matrix(0.301909,0,0,0.301909,32.471,293.967)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <path d="M499.776,287.389L488.486,298.28" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <g transform="matrix(0.301909,0,0,0.301909,286.553,267.632)">
            <circle cx="456.547" cy="308.649" r="33.072" style="fill:none;stroke:#ECDBC5;stroke-width:20.7px;"/>
        </g>
        <path d="M361.321,488.991L365.117,496.972L380.958,498.448L380.958,509.866C462.055,466.68 499.398,399.221 509.119,309.34L482.037,341.695L482.037,370.499L464.333,383.707" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M468.334,132.703L529.41,169.653L529.41,417.236L468.334,455.652" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M347.415,502.154L346.608,532.027L366.365,519.537" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M282.614,507.657L283.378,532.027L262.591,517.592" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M425.637,372.639L426.112,404.223L391.686,439.396L391.686,446.258L420.135,446.258L442.735,424.597L442.735,339.347" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M463.59,342.291L461.78,418.803C461.78,418.803 466.721,471.328 402.802,464.861C391.558,463.723 387.429,467.559 378.763,480.05" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M472.94,279.277L509.071,241.661L509.506,194.11" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M447.517,277.328L487.903,236.941L487.903,145.085" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M430.485,268.911L471.365,228.031L471.365,160.87L453.654,143.159L453.654,124.176L339.933,56.471" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M433.405,239.45L453.317,219.538L453.317,173.093L437.541,157.189" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <g transform="matrix(-1,0,0,1,635.775,11.4156)">
            <path d="M433.405,239.45L453.317,219.538L453.317,173.093L437.541,157.189" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        </g>
        <path d="M390.812,309.051C390.812,309.051 423.489,309.051 423.489,308.636" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M207.214,308.644L237.791,308.644" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M179.154,139.926L179.154,173.249L161.174,191.229L161.174,225.83L186.308,252.243" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M186.308,279.744L186.308,252.243" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M141.589,182.389L141.589,265.082L156.055,279.548" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M122.058,240.111L122.058,272.372L143.275,293.59" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M120.85,313.804L164.433,357.387L181.517,361.889L207.681,388.522C206.855,388.86 220.756,422.959 227.428,426.001L227.428,471.296L252.753,471.296" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M169.628,445.877C142.678,408.806 131.167,372.189 122.48,334.598" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M122.48,334.598L163.472,380.787" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M187.16,401.821L209.976,424.261L209.976,484.318L247.968,508.861L247.968,494.711L266.526,494.711L266.526,488.35" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M187.461,339.836L203.664,356.716C203.664,356.716 211.704,364.716 211.704,376.626C211.704,388.536 211.704,398.453 211.704,398.093" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M314.444,39.949L314.444,83.736" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M288.297,56.878L195.888,111.248" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M157.705,132.792L99.563,168.78L99.563,287.253" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M99.679,324.012L101.437,417.758L160.648,455.024" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M144.863,401.768C144.863,401.768 160.379,414.662 176.078,415.306L209.73,449.051" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M298.606,518.567L298.606,540.176L314.732,550.054L331.163,540.176L331.163,510.504" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M314.732,550.054L314.732,518.567" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M310.845,93.132L279.706,113.686L264.291,147.359L264.291,190.453L247.979,190.453L237.983,210.048L237.983,441.686L297.622,503.93L320.094,503.93L380.967,463.679L376.959,371.051L390.456,352.415L390.456,234.506L383.939,216.974L365.286,206.55L365.286,139.431L351.419,113.686L310.845,93.132Z" style="fill:#00a8ff;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M264.291,190.453L276.432,198.874L281.785,219.682L264.291,418.709L277.171,272.184L291.04,295.468L298.98,400.71L291.04,295.468L320.628,237.045L339.862,219.682L365.728,206.739" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M240.553,442.813L264.879,419.193L299.044,398.609L341.83,449.537L301.255,503.761L341.83,449.537L381.792,462.54" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M310.74,94.101L314.945,111.708L364.236,140.551" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M315.35,112.35L295.485,131.634L288.631,151.314L290.832,215.391L328.501,192.717L345.973,172.294L345.973,131.634" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
        <path d="M282.799,220.551L292.14,214.859" style="fill:none;stroke:#ECDBC5;stroke-width:6.25px;"/>
    </g>
    <g transform="matrix(0.988409,0,0,0.989856,3.57176,38.3391)">
        <path d="M299.904,-14.887L541.724,124.728L541.724,403.957L299.904,543.572L58.085,403.957L58.085,124.728L299.904,-14.887Z" style="fill:none;stroke:#ECDBC5;stroke-width:12.64px;"/>
    </g>
    <g id="Layer2">
    </g>
</svg>"""

# Model-picker icons. Source files came in as black-stroke line art on a
# transparent background (fine on a white page, invisible on these dark navy
# cards) — recolored to the same #ECDBC5 cream the header logo above already
# uses, so they read clearly against the card gradient.
MODEL_ICON_R2D2 = """<svg width="100%" height="100%" viewBox="0 0 144 144" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" xml:space="preserve" xmlns:serif="http://www.serif.com/" style="fill-rule:evenodd;clip-rule:evenodd;stroke-linecap:round;stroke-linejoin:round;">
    <g id="Layer_4_2">
        <path d="M59.547,105.85L59.593,105.845M90.626,106.97L90.797,106.754L89.765,97.368C88.265,97.769 84.85,98.683 82.932,99.192C81.161,99.665 77.51,100.69 75.598,101.299C73.829,101.904 70.308,103.236 68.582,103.913C66.334,104.849 63.706,106.306 62.983,106.702C62.854,106.694 62.527,106.598 61.056,106.442C60.373,106.361 59.766,106.103 59.595,105.928C59.567,105.899 59.55,105.873 59.547,105.85C59.547,105.848 59.547,105.846 59.546,105.845C59.541,105.775 59.556,105.703 59.589,105.629C61.402,101.527 119.699,90.718 120.557,92.968C120.561,92.981 120.564,92.994 120.566,93.007M108.246,39.691L108.228,39.613L112.543,39.139C118.564,38.233 118.458,62.222 113.834,63.877M42.655,58.06C42.491,61.045 41.978,66.012 41.993,66.055C42.077,66.281 43.261,66.691 43.675,66.864C43.675,66.863 43.675,66.864 43.675,66.864C43.675,66.864 43.675,66.864 43.675,66.864C43.639,66.908 42.464,74.619 41.306,82.399C40.853,85.445 39.615,93.392 39.348,95.11C39.319,95.294 39.302,95.406 39.298,95.434M29.205,115.562C28.056,115.713 27.099,116.483 26.285,117.718L25.865,123.991L32.412,128.758L32.415,128.753C32.415,128.753 32.7,125.899 32.702,124.829C32.7,123.281 32.174,119.784 31.546,117.907C31.238,117.012 30.468,115.817 30.007,115.639C29.801,115.569 29.509,115.545 29.205,115.562L31.284,115.085L31.764,115.039C35.995,115.043 32.415,128.753 32.415,128.753M20.886,123.555L20.876,123.566L28.949,110.861L29.261,110.369M38.861,139.848L21.058,127.478L20.876,123.566L20.875,123.547L38.592,135.587L38.76,115.265L29.347,110.354C29.839,109.875 31.564,109.722 33.418,110.004L31.875,111.673M38.76,115.265L48.451,113.82L50.791,118.476M102.934,93.344C105.932,92.649 109.068,92.245 112.092,92.114L110.434,81.473C108.134,80.554 104.725,80.828 100.942,81.881L102.934,93.344ZM117.278,91.51L115.701,81.473C114.809,81.125 113.168,81.032 112.092,81.236L113.834,91.71C114.9,91.444 116.27,91.255 117.278,91.51ZM120.566,93.007C120.566,93.007 116.403,101.785 115.923,102.881M107.244,124.573L115.157,122.966C115.188,122.923 115.01,118.903 115.01,118.903L107.017,106.884M80.062,112.971C76.049,113.411 68.546,114.264 68.46,114.271C68.039,114.31 67.613,114.346 67.613,114.346C67.613,114.346 73.269,112.685 77.248,111.576L80.136,112.963L80.062,112.971M77.248,111.576C78.726,111.164 80.264,110.746 81.802,110.345C85.339,109.5 92.866,107.782 96.854,106.906C100.356,106.217 109.495,104.546 113.045,104.222L116.962,103.541L116.942,102.79C116.932,102.78 116.563,102.813 115.923,102.881C113.623,103.127 107.827,103.834 102.631,104.714C98.758,105.446 90.883,106.99 86.88,107.803C82.944,108.655 77.064,109.975 72.982,111.149C71.15,111.715 68.906,112.411 68.496,112.541L68.078,112.505L62.983,106.702M67.526,114.346C67.526,114.346 67.234,113.095 67.238,113.081C67.245,113.067 67.825,112.793 68.392,112.532M93.896,118.431L93.821,118.591L97.013,138.781C97.289,138.716 113.687,134.748 113.687,134.748L101.762,115.918L96.151,113.614L96.145,113.627M69.817,127.858L96.996,140.47C96.991,140.39 97.013,138.781 97.013,138.781L69.934,126.017M101.762,115.918L99.158,116.722M98.256,109.115L101.719,115.931M96.996,140.47L113.72,136.357L113.762,138.428L96.65,143.146C96.586,143.141 69.687,129.905 69.687,129.905L69.934,126.017L80.177,112.949M44.785,120.135C45.106,119.857 47.866,118.694 48.797,118.534C49.673,118.397 50.899,118.63 51.977,119.078C53.119,119.57 54.206,120.221 54.814,120.778C55.454,121.378 57.146,124.301 57.991,125.777L58.242,126.216M46.625,121.123L47.433,122.964C48.362,122.847 49.654,123.144 50.878,123.686C51.238,123.847 53.376,124.906 54.314,125.378C55.586,125.981 56.621,126.324 57.221,126.288C57.226,126.295 57.23,126.3 57.235,126.302C57.242,126.312 57.252,126.322 57.262,126.331C56.782,125.633 55.452,124.546 53.906,122.801C52.93,121.702 51.931,120.77 51.204,120.415C50.741,120.202 50.035,119.971 49.69,119.952C49.31,119.938 48.389,120.156 47.854,120.358C47.249,120.593 45.468,121.536 45.086,121.354C44.772,121.195 44.611,120.953 44.606,120.629C44.606,120.524 44.666,120.359 44.785,120.135L42.943,120.658L45.211,126.055L48.024,125.131L47.904,124.783L47.85,124.573C47.686,124.58 47.54,124.6 47.417,124.634C47.088,124.726 46.438,124.987 46.327,124.961C46.01,124.882 45.782,124.243 45.886,123.79C45.929,123.612 46.15,123.43 46.49,123.182C46.625,123.13 47.078,123.01 47.433,122.964M47.85,124.573C48.725,124.534 50.088,124.852 50.899,125.208C51.06,125.282 55.56,127.543 57.06,128.189L57.281,127.858M55.423,100.943L50.693,99.178M55.236,72.062L52.169,73.937M38.592,70.464C38.078,73.824 36.905,82.874 36.23,87.298C36.115,88.063 35.945,89.923 35.945,89.923C35.642,90.014 34.781,90.78 34.531,91.152C32.742,93.815 42.264,99.939 42.655,97.982L41.179,111.118M34.37,91.51L33.492,101.688M33.473,110.429C33.449,110.566 34.068,111.218 34.579,111.751C35.321,112.526 36.562,113.825 37.061,114.346L38.861,115.265C38.638,114.919 36.828,113.529 36.696,113.33C35.97,112.239 38.786,109.572 39.612,110.29C39.708,110.373 40.853,111.022 41.179,111.149C40.642,110.666 38.086,108.499 37.978,108.098C37.865,107.659 33.49,101.654 33.49,101.654C33.48,101.662 32.407,101.052 32.299,101.026C31.882,100.93 30.034,103.049 30.125,103.102C30.168,103.267 32.167,106.385 32.345,106.831C33.105,108.764 33.39,109.781 33.458,110.21C33.476,110.319 33.479,110.39 33.473,110.429ZM52.195,121.27L54.118,125.098M50.726,97.733C50.227,101.376 49.181,109.018 48.634,113.016C48.607,113.213 48.578,113.41 48.552,113.609M55.469,127.786L55.934,128.712L59.266,129.175L59.849,130.91C56.647,131.652 38.592,135.587 38.592,135.587L38.861,139.848L60.305,134.731L59.849,130.91M104.868,94.985L103.654,104.352M101.892,95.434L101.719,104.683M91.296,97.229L92.388,106.435M96.854,106.884L96.053,115.202L99.158,116.722L100.922,121.55L98.702,122.054L96.518,117.535C96.521,117.494 93.97,116.172 93.881,116.102L94.69,107.386M96.574,109.798C98.086,109.227 105.355,105.492 105.552,105.326M100.183,108.098C101.867,107.845 105.698,107.455 106.524,107.076C107.51,106.62 114.672,104.501 115.481,104.083M96.996,140.47L96.65,143.146M105.13,121.235L115.01,118.903M43.416,96.122L46.555,74.102M49.238,36.314C50.902,34.922 52.474,33.79 54.689,32.616C56.335,31.788 60.072,30.067 61.872,29.306C63.559,28.62 66.422,27.665 68.592,27.06C71.023,26.446 73.001,26.023 75.514,25.565C79.956,24.852 86.707,24.086 89.722,24.103C91.531,24.118 94.894,24.422 97.31,24.782C103.284,25.826 104.57,26.268 104.969,26.419M47.926,35.201C49.054,34.243 52.466,31.358 53.527,30.545C55.193,29.278 58.133,27.473 59.796,26.681C61.658,25.81 63.943,24.89 66.254,24.072C68.352,23.386 71.911,22.392 73.298,22.114C74.753,21.833 78.866,21.238 80.734,21.046C82.567,20.894 86.222,20.806 88.039,20.827C89.618,20.873 93.312,21.206 95.777,21.598C101.179,22.543 103.219,23.198 104.378,23.597M47.714,33.254C48.752,31.593 56.952,26.746 56.952,26.746C56.796,26.558 55.724,18.583 56.218,18.449C56.218,18.449 48.758,24.271 48.271,24.782M67.238,14.052C67.115,13.892 68.493,22.17 68.683,22.404C68.726,22.457 68.676,22.399 68.676,22.399L63.504,24.024C63.047,24.128 62.342,15.638 62.594,15.593L67.238,14.052ZM80.654,14.424C80.712,14.467 81.802,19.699 81.802,19.699C77.998,19.732 74.405,20.25 71.006,21.218L70.334,16.474C70.207,15.607 80.281,13.707 80.654,14.424ZM87.868,11.748L86.928,12.394L83.887,12.422C83.887,12.422 83.234,6.91 82.896,5.503C82.764,4.954 82.234,3.425 82.178,3.446C81.744,3.444 69.334,4.704 69.322,4.894C69.319,4.997 68.655,14.194 68.683,14.052C68.935,12.774 83.803,12.422 83.803,12.422C81.088,10.856 70.841,12.177 68.683,14.023M82.5,4.049L82.514,4.013C82.543,4.013 82.584,4.015 82.639,4.02C83.05,4.054 83.479,4.037 83.642,4.051C83.794,4.07 86.335,8.105 86.945,9.514C87.175,10.046 87.789,11.506 87.868,11.748C86.911,11.707 85.414,11.587 84.643,11.57C84.415,11.568 84.009,11.573 83.784,11.57M93.629,44.846C95.15,44.863 100.483,44.998 100.483,44.998L100.538,43.555C100.603,43.402 102.9,43.654 103.298,43.944C103.45,44.059 104.95,45.49 105.24,45.636C105.494,45.756 108.341,45.497 108.54,45.49C108.509,45.528 108.271,42.833 108.269,42.799C108.254,42.749 104.513,40.368 104.222,40.171C103.802,40.526 102.823,41.316 102.823,41.316C102.823,41.316 100.375,41.203 100.322,41.134L100.034,39.962C100.046,39.982 95.532,38.249 87.35,39.78C86.966,39.862 85.234,41.388 85.109,41.479C84.494,41.921 80.782,42.23 80.702,42.245C80.462,42.3 80.364,42.578 80.326,43.152C80.306,43.754 80.386,44.263 80.566,44.366C80.609,44.39 83.27,44.182 83.803,44.155C84.794,44.107 85.879,44.227 86.755,44.666C87.113,44.849 87.466,45.084 87.449,45.101C87.917,45.137 91.747,44.827 93.629,44.846ZM93.653,35.309L94.032,36.638C94.02,35.971 103.861,37.102 104.611,37.939L104.942,37.488C105.605,37.721 105.991,37.762 106.078,37.519C106.078,37.519 106.25,36.329 106.248,36.283C106.226,35.669 106.003,35.258 105.533,34.966C104.875,34.565 103.577,32.945 103.56,32.928C103.296,32.722 93.497,31.416 92.818,31.33L93.022,33.254C93.014,33.259 89.65,33.257 89.251,33.098C88.81,32.916 86.695,31.711 86.628,31.694C86.086,31.565 79.03,34.152 78.737,34.265C78.778,34.198 79.313,36.377 79.296,36.655C79.294,36.73 79.294,36.768 79.298,36.768C79.942,36.775 87.223,37.33 87.49,37.351C88.19,36.854 90.346,35.352 90.377,35.34C90.862,35.162 92.095,35.242 93.653,35.309ZM55.133,38.964C62.7,37.79 63.774,57.482 56.909,60.43M96,48.941C96.119,48.471 100.512,48.205 101.074,48.648L103.166,50.311L104.904,58.478C104.028,59.395 103.592,59.806 103.596,59.645C103.603,59.378 98.157,59.341 98.052,59.645L96.437,58.661C96.437,58.661 94.985,50.638 95.076,50.244L96,48.941ZM105.775,62.698C105.775,62.698 107.609,70.745 107.546,70.97C107.141,71.398 106.304,72.172 106.078,72.062C105.205,71.64 100.607,72.066 101.256,72.381L99.031,70.81L97.454,63.077L98.75,61.356C100.422,60.987 102.165,60.895 104.035,61.306L105.775,62.698ZM81.022,77.359L72.382,31.944C68.857,32.258 65.334,33.235 61.812,35.162L70.416,80.702C73.663,78.923 77.154,77.707 81.022,77.359ZM103.817,47.657L109.205,73.236C105.272,73.239 101.54,73.555 97.978,74.134L92.446,47.815C92.395,46.983 102.772,46.519 103.817,47.657ZM99.031,93.784C95.007,94.055 90.985,95.039 87.187,96.242C87.187,96.242 84.901,84.799 84.893,84.744C84.696,83.482 96.639,81.299 96.854,82.661L99.031,93.784ZM56.474,128.786L56.782,128.652L61.03,121.567L59.885,120.626L59.566,104.46C59.688,99.885 50.114,99.499 49.77,104.714M88.332,132.607L76.277,126.257C73.995,125.484 78.084,118.554 81.278,118.307C85.764,117.792 90.216,130.491 88.332,132.607C92.702,128.413 88.889,117.385 85.603,118.02L81.278,118.307M80.035,112.961L93.782,118.464L96.535,117.662M53.906,72.875C55.265,80.082 58.016,96.262 59.566,104.477M44.918,54.386C39.622,56.047 38.847,43.645 44.818,46.368C45.981,46.893 46.11,54.013 44.918,54.386ZM48.802,95.796L52.169,73.937C50.402,73.927 43.356,73.824 43.356,73.824L47.434,72.062L43.416,73.824C43.416,73.824 44.753,66.622 45.024,63.305C45.125,62.059 45.302,57.425 45.233,57.362C45.18,57.334 43.49,57.84 42.545,58.09C41.537,58.351 40.824,58.409 40.344,58.164C39.797,57.862 37.829,54.182 37.79,54.175C37.776,54.36 38.592,70.464 38.592,70.464L36.058,62.299L38.592,41.918C40.068,32.786 51.519,35.562 49.416,48.811L47.434,72.062L55.236,72.062C55.236,72.062 58.26,48.385 58.289,48.343C59.073,47.199 55.055,37.244 53.117,36.672C52.744,36.562 52.74,36.355 52.666,36.434L42.974,36.598M45.209,57.362L45.25,53.755C42.164,53.981 41.691,47.206 44.818,46.368M113.687,134.748L113.72,136.357M120.557,92.968C119.465,88.244 111.97,55.81 108.246,39.691L108.228,39.613C107.338,35.761 106.666,32.852 106.351,31.49C105.192,26.465 105.168,26.342 105.13,26.098L104.618,23.981C104.51,23.58 102.662,19.152 102.442,18.65C101.972,17.591 99.059,12.683 98.606,12.12C97.62,10.903 95.064,8.443 93.646,7.37C92.126,6.23 88.807,4.253 87.276,3.612C85.733,2.974 82.296,2.098 80.342,1.754C78.478,1.445 75.451,1.315 73.838,1.476C72.379,1.63 68.69,2.494 67.214,2.962C65.328,3.564 62.366,4.985 60.634,6.12C58.963,7.224 56.244,9.66 55.003,11.131C53.882,12.466 51.792,15.54 50.969,17.086C50.035,18.854 48.922,22.042 48.274,24.773C48.273,24.776 48.272,24.779 48.271,24.782C48.097,25.508 47.714,27.427 47.714,27.427C47.71,27.529 47.724,30.805 47.736,33.221C47.742,34.4 47.746,35.374 47.748,35.676C47.748,36.074 47.75,36.308 47.755,36.517C47.758,36.614 47.761,36.706 47.765,36.806L47.777,36.79M35.945,89.923C35.755,90.492 36.025,90.977 35.76,91.291C35.199,91.956 37.584,94.706 38.203,94.541C38.411,94.485 41.63,96.137 42.394,96.473L42.655,96.473L48.996,95.89C49.159,95.858 49.253,95.844 49.272,95.844C49.279,95.844 49.262,95.87 49.224,95.923C49.186,95.995 50.1,97.14 50.558,97.711L42.655,97.982M74.612,4.641C77.259,4.389 79.555,5.726 79.736,7.625C79.916,9.524 77.913,11.27 75.266,11.522C72.618,11.773 70.322,10.436 70.141,8.537C69.961,6.638 71.964,4.892 74.612,4.641ZM74.675,5.358C77.323,5.106 79.599,6.231 79.754,7.87C79.91,9.508 77.887,11.042 75.239,11.294C72.592,11.546 70.316,10.42 70.16,8.782C70.004,7.144 72.027,5.609 74.675,5.358ZM90.942,12.132C93.044,12.132 94.751,13.839 94.751,15.941C94.751,18.044 93.044,19.751 90.942,19.751C88.84,19.751 87.133,18.044 87.133,15.941C87.133,13.839 88.84,12.132 90.942,12.132ZM91.898,11.934C93.471,11.934 94.748,13.211 94.748,14.784C94.748,16.357 93.471,17.634 91.898,17.634C90.325,17.634 89.048,16.357 89.048,14.784C89.048,13.211 90.325,11.934 91.898,11.934ZM98.803,14.949L100.034,20.827C98.916,20.079 97.335,19.623 95.777,19.751L94.924,14.443C96.455,14.243 97.697,14.216 98.803,14.949ZM88.77,59.766L93.189,77.382C88.615,77.862 86.345,78.394 84.827,79.392L80.428,61.444C82.969,60.615 85.58,58.609 88.77,59.766ZM39.089,110.347C40.298,110.347 41.279,111.328 41.279,112.537C41.279,113.746 40.298,114.727 39.089,114.727C37.88,114.727 36.899,113.746 36.899,112.537C36.899,111.328 37.88,110.347 39.089,110.347ZM74.004,15.811C75.186,15.595 76.299,16.258 76.488,17.291C76.678,18.325 75.872,19.339 74.69,19.556C73.509,19.772 72.396,19.109 72.206,18.076C72.017,17.043 72.823,16.028 74.004,15.811ZM105.949,82.368C107.846,82.044 109.727,83.778 110.147,86.237C110.568,88.697 109.368,90.956 107.471,91.281C105.574,91.605 103.692,89.871 103.272,87.412C102.852,84.952 104.051,82.692 105.949,82.368Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:1.25px;"/>
    </g>
</svg>"""

MODEL_ICON_BB8 = """<svg width="100%" height="100%" viewBox="0 0 144 144" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" xml:space="preserve" xmlns:serif="http://www.serif.com/" style="fill-rule:evenodd;clip-rule:evenodd;stroke-linecap:round;stroke-linejoin:round;">
    <g transform="matrix(1,0,0,1,-0.04843,-0.25199)">
        <g id="Layer_5_2">
            <path d="M114.022,110.856C113.156,111.789 109.606,115.438 109.606,115.438L108.442,113.578L112.133,107.484C111.967,107.232 111.019,106.022 110.237,105.53C109.766,105.24 107.789,104.453 106.966,104.369C106.037,104.285 104.194,104.587 103.126,104.945C100.879,105.732 100.158,105.996 100.081,106.01C100.055,106.054 101.376,112.582 101.376,112.582L98.09,114.859L95.318,109.318C93.588,110.89 92.102,112.493 90.773,114.194C89.422,115.975 87.658,118.774 86.861,120.482C84.893,124.733 84.746,128.362 84.746,128.362L91.265,127.126L91.493,129.214L84.72,133.094C84.662,133.186 84.461,134.822 85.774,136.39C86.328,137.035 87.482,138.061 88.886,138.97M64.248,23.804C62.775,23.66 61.241,23.589 60.233,23.623C59.79,23.641 59.178,23.705 58.493,23.8C57.094,23.994 55.392,24.313 54.209,24.612C53.902,24.689 53.63,24.765 53.407,24.838L53.152,24.924C51.95,25.346 50.194,26.118 48.729,26.877C48.121,27.192 47.563,27.505 47.117,27.79C45.518,28.814 42.989,30.982 41.796,32.261C40.721,33.422 38.546,36.535 37.829,37.946C37.039,39.506 35.878,42.629 35.467,44.482C35.062,46.332 34.8,49.692 34.865,51.401C34.932,53.05 35.527,56.582 36.108,58.267C36.42,59.165 37.493,61.709 37.589,61.939L38.602,62.366C39.89,62.446 43.181,62.647 45.18,62.767C47.258,62.842 50.904,62.873 53.15,62.753C55.02,62.623 58.666,62.165 60.576,61.86C64.169,61.205 72.026,59.261 75.456,58.123C77.306,57.482 80.767,55.98 82.812,54.929C84.312,54.134 86.966,52.651 88.483,51.247C88.618,51.122 88.752,51 88.886,50.882L89.198,50.614C89.214,50.601 87.308,44.045 86.985,42.915L86.822,42.408C86.789,42.322 85.27,38.045 83.858,35.861C82.858,34.32 80.693,31.774 79.375,30.55C78.206,29.472 75.101,27.305 73.692,26.587C72.132,25.798 69.01,24.636 67.157,24.226C66.373,24.054 65.326,23.909 64.248,23.804M61.345,35.068L62.117,35.066C64.476,34.363 66.66,33.624 68.652,32.686C69.571,32.251 71.282,31.351 72.132,30.854C73.807,29.863 75.353,28.366 75.6,28.13M45.18,62.767C43.771,63.809 42.202,65.321 40.392,67.138C38.592,68.998 36.547,71.347 35.534,72.761C34.57,74.112 32.705,77.299 32.028,78.677C31.332,80.098 29.986,83.482 29.455,85.133C28.946,86.782 28.186,90.158 27.926,91.896C27.641,93.828 27.439,96.847 27.463,98.801C27.49,100.56 27.794,104.006 28.078,105.706C28.382,107.414 29.282,111.014 29.765,112.45C30.252,113.882 31.718,117.29 32.513,118.834C33.319,120.36 35.167,123.283 36.214,124.697C37.378,126.266 39.37,128.544 40.769,129.907C42.031,131.131 44.683,133.354 46.087,134.354C47.51,135.348 50.693,137.258 52.049,137.93C53.405,138.6 56.854,139.973 58.505,140.501C60.154,141.01 63.528,141.77 65.268,142.032C67.2,142.318 70.219,142.519 72.173,142.495C73.93,142.469 77.376,142.164 79.078,141.878C80.786,141.576 84.386,140.676 85.822,140.191C87.254,139.706 90.662,138.238 92.206,137.443C93.73,136.639 96.655,134.789 98.069,133.745C99.588,132.617 101.933,130.579 103.26,129.24C104.537,127.951 106.706,125.333 107.76,123.792C108.598,122.561 110.998,118.493 111.302,117.91C112.042,116.486 113.052,113.813 113.513,112.536C114.31,110.15 114.802,107.611 115.116,105.996C115.512,103.8 115.896,100.392 115.915,98.546C115.925,97.003 115.634,93.259 115.392,91.73C115.116,90.002 114.382,86.83 113.825,84.958C113.383,83.518 111.979,80.095 111.206,78.528C110.342,76.781 108.518,73.841 107.63,72.598C106.742,71.354 104.554,68.678 103.176,67.291C101.952,66.062 99.242,63.689 97.968,62.736C96.396,61.582 93.794,59.897 92.138,59.069C89.786,57.9 83.738,55.248 82.812,54.929M36.85,59.158C38.141,59.249 41.438,59.479 43.45,59.618C45.588,59.712 49.306,59.69 51.482,59.546C53.299,59.398 56.904,58.913 58.726,58.603C62.345,57.907 68.76,56.194 73.19,54.778C74.834,54.209 78.49,52.879 80.4,52.121C85.637,49.874 86.978,49.162 88.474,48.199M42.358,32.22C43.044,33.07 43.654,33.629 44.434,33.955C45.922,34.548 49.106,34.997 51.098,34.961C52.824,34.925 56.34,34.457 58.106,34.078C61.411,33.362 66.696,31.166 67.982,30.418C69.598,29.465 71.011,27.998 72.456,26.395M65.21,24.617C65.158,24.398 64.632,23.947 64.332,23.83C63.614,23.57 59.611,23.453 57.612,23.774C55.932,24.05 52.154,25.102 50.63,25.795C49.74,26.208 48.24,27.142 48.014,27.418C47.722,27.799 47.582,28.493 47.796,28.793C48,29.062 48.598,29.369 48.998,29.45C50.592,29.729 54.074,29.544 56.028,29.177C57.866,28.824 61.322,27.751 62.947,26.834C63.266,26.652 63.977,26.213 64.274,26.023C64.882,25.644 65.254,24.838 65.21,24.617ZM40.226,34.769C41.359,35.424 42.283,35.825 43.31,36.074C45.214,36.518 48.442,36.878 50.532,36.806C51.334,36.778 54.586,36.516 54.859,36.492M38.225,37.987C38.657,38.17 40.022,38.748 40.56,38.887C42.866,39.475 50.942,39.262 52.951,39.206M64.668,36.619C65.993,36.098 69.989,34.507 71.119,33.984C73.248,32.993 75.446,31.57 77.453,30C77.575,29.904 77.7,29.808 77.822,29.712M54.209,3.242L54.01,2.94C54.696,6.166 56.371,14.004 57.355,18.619C58.632,24.55 59.004,25.999 59.114,26.422M51.722,15.11L53.371,26.429L54.377,25.255L51.722,15.11ZM43.037,65.05C44.93,65.688 47.042,66.058 49.517,66.324C51.403,66.48 54.439,66.643 56.753,66.641C58.807,66.583 62.806,66.271 64.018,66.115C65.861,65.863 69.854,65.22 71.794,64.846C74.033,64.363 77.57,63.338 79.296,62.616C81.084,61.858 84.017,60.118 85.738,58.682C86.316,58.202 87.351,57.106 87.543,56.947M73.879,64.723C75.432,66.418 76.728,67.711 78.389,69.163C80.045,70.565 82.738,72.446 84.77,73.692C86.27,74.575 89.462,76.411 91.231,77.374C93.194,78.389 96.427,79.841 98.122,80.436C100.272,81.187 103.176,81.768 105.814,82.128C108,82.378 110.215,82.37 112.378,82.003M80.4,62.098C81.83,63.48 83.539,65.321 85.517,66.586C88.378,68.39 89.153,68.551 89.153,68.551C90.132,67.447 91.723,65.275 91.723,65.275L94.954,67.056L95.124,71.698C95.124,71.698 99.586,73.853 101.47,74.232C103.169,74.566 107.172,74.832 108.775,74.935M51.355,69.238C49.877,69.19 46.656,69.706 44.974,70.344C43.421,70.942 40.368,72.838 39.132,73.946C37.702,75.242 35.69,77.678 34.723,79.188C33.859,80.549 32.354,83.681 31.829,85.154C31.308,86.628 30.449,90.142 30.214,91.874C29.964,93.768 29.849,96.994 29.966,98.899C30.077,100.644 30.684,104.21 31.102,105.718C31.522,107.222 32.798,110.455 33.566,111.874C34.423,113.446 36.257,116.018 37.594,117.413C38.748,118.608 41.657,120.715 43.164,121.421C44.798,122.177 47.974,122.918 49.452,122.974C50.93,123.022 54.151,122.508 55.836,121.87C57.389,121.272 60.442,119.376 61.675,118.267C63.108,116.969 65.119,114.533 66.084,113.026C66.95,111.665 68.453,108.53 68.978,107.059C69.502,105.583 70.361,102.07 70.594,100.337C70.843,98.446 70.958,95.218 70.843,93.312C70.73,91.567 70.123,88.003 69.706,86.494C69.286,84.989 68.009,81.756 67.241,80.338C66.384,78.766 64.55,76.193 63.214,74.798C62.062,73.606 59.15,71.498 57.643,70.793C56.009,70.037 52.834,69.295 51.355,69.238ZM59.285,109.255C59.326,109.2 53.407,103.43 53.407,103.43L55.836,100.178L62.345,103.817C63.103,101.179 63.614,99.228 63.754,97.26C63.886,95.294 63.569,91.522 63.048,89.594C61.862,85.298 59.873,82.342 59.033,81.338L54.113,86.246L50.969,84.288L55.327,77.909C54.41,77.321 53.563,76.831 52.795,76.531C52.025,76.238 50.537,75.826 49.8,75.725C49.092,75.631 47.556,75.61 46.826,75.696C46.08,75.79 44.616,76.133 43.906,76.404C43.171,76.69 41.83,77.378 41.16,77.827C39.742,78.797 37.591,81.173 37.154,81.89L39.506,87.238L37.459,89.381L34.361,87.528C33.706,90.019 33.341,92.53 33.331,94.714C33.329,95.854 33.456,97.678 33.629,98.686C33.982,100.697 35.03,103.447 36.182,106.01C36.449,106.608 37.166,107.681 37.337,107.933L40.392,104.537L42.703,106.01L41.573,112.039C41.777,112.325 43.142,113.177 43.646,113.417C44.033,113.599 45.881,114.233 46.558,114.382C47.275,114.533 48.854,114.641 49.546,114.605C50.23,114.566 51.84,114.278 52.495,114.079C53.155,113.875 54.65,113.213 55.294,112.826C56.69,111.972 58.663,110.098 59.285,109.255ZM115.625,102.098C115.337,101.453 114.581,99.787 114.194,99.187C113.76,98.525 112.668,97.447 112.08,97.03C111.502,96.624 110.023,95.904 109.363,95.707C108.703,95.513 106.908,95.268 106.15,95.27C104.467,95.282 101.23,96.06 99.509,96.758C98.083,97.342 94.939,99.154 93.533,100.178C92.126,101.208 89.82,103.21 88.51,104.537C87.439,105.624 84.977,108.542 84.036,109.848C83.095,111.154 81.108,114.413 80.412,115.774C79.567,117.434 78.398,120.257 77.866,121.915C77.34,123.574 76.615,127.13 76.512,128.666C76.394,130.541 76.67,133.649 77.201,135.439C77.462,136.294 78.055,137.638 78.631,138.348C79.198,139.03 81.677,141.096 81.879,141.269M74.366,39.979C77.007,39.979 79.152,42.615 79.152,45.862C79.152,49.11 77.007,51.746 74.366,51.746C71.725,51.746 69.58,49.11 69.58,45.862C69.58,42.615 71.725,39.979 74.366,39.979ZM59.664,34.863C63.49,34.863 66.597,37.952 66.597,41.757C66.597,45.561 63.49,48.65 59.664,48.65C55.837,48.65 52.73,45.561 52.73,41.757C52.73,37.952 55.837,34.863 59.664,34.863Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:1.25px;"/>
        </g>
    </g>
</svg>"""

MODEL_ICON_CHOPPER = """<svg width="100%" height="100%" viewBox="0 0 144 144" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" xml:space="preserve" xmlns:serif="http://www.serif.com/" style="fill-rule:evenodd;clip-rule:evenodd;stroke-linecap:round;stroke-linejoin:round;">
    <g id="Layer_3_2" transform="matrix(0.24,0,0,0.24,0,0)">
        <g transform="matrix(4.16667,0,0,4.16667,-0.754491,-0.263051)">
            <path d="M49.368,103.099L49.308,103.099C49.322,103.961 49.31,105.242 49.296,106.301L49.176,113.285M116.316,78.384L110.671,78.814C110.67,78.814 105.765,81.444 105.764,81.444C105.729,81.427 110.671,78.814 110.671,78.814C110.671,78.814 109.598,62.023 109.646,61.049L109.634,60.725L109.598,60.475C109.442,59.453 108.293,55.294 107.87,54.264C107.431,53.206 106.577,51.912 106.01,51.211C105.538,50.633 104.328,49.385 103.874,49.01C103.183,48.446 101.731,47.57 101.035,47.321C100.169,47.021 99.727,46.966 99.266,46.958L106.618,46.152C106.618,46.152 109.603,45.91 109.639,45.931C109.776,46.018 111.17,46.776 111.874,47.472C112.862,48.458 114.984,51.463 115.726,52.994C116.405,54.41 117.374,57.732 117.643,59.882L117.835,61.493C117.794,61.656 116.316,78.384 116.316,78.384ZM81.207,106.848L82.377,111.313M82.377,111.313C80.777,113.038 79.108,114.699 77.652,115.831C76.783,116.501 75.72,117.235 75.271,117.305C75.242,117.307 74.131,117.331 73.99,117.247C73.53,116.955 73.197,115.333 73.197,115.333L73.147,115.061L72.089,109.106C71.834,108.747 87.677,105.223 87.975,105.601L87.974,105.6C87.975,105.6 87.975,105.6 87.975,105.601L89.218,109.606L94.322,113.971C94.325,114.07 94.476,116.285 94.536,116.362C94.584,116.417 93.918,127.653 93.922,127.682L98.779,130.654L118.416,126.818L118.356,125.033M79.896,116.743L82.377,111.313M71.078,119.287L73.197,115.061M35.009,90.869L33.962,105.346C33.982,105.516 34.109,105.636 34.531,105.842C34.757,105.95 36.614,106.812 37.478,106.944C38.436,107.081 39.766,106.954 40.08,106.714C40.092,106.704 40.105,106.692 40.12,106.676M106.71,100.239L106.761,100.189M107.117,92.693L107.066,92.664C106.99,94.377 106.836,98.13 106.762,100.17L106.761,100.189M106.758,100.285L106.71,101.809M92.928,58.35C93.036,58.325 93.201,58.297 93.466,58.258C96.014,57.893 97.45,57.986 97.5,57.989C97.656,57.912 97.728,57.869 97.858,57.715C98.954,56.41 99.182,56.074 99.324,55.752C99.18,55.615 98.962,55.558 98.359,55.538C96.785,55.483 96.458,55.483 96.12,55.502C95.825,55.464 95.753,55.308 95.892,54.862C96.031,54.418 95.954,53.604 95.95,53.304C95.95,53.23 95.981,53.23 96.043,53.304L98.974,53.158C98.827,53.026 98.626,52.862 98.34,52.656C97.812,52.274 96.715,51.569 96.701,51.564C96.238,51.401 91.915,51.612 91.783,51.715C91.85,51.662 92.134,52.992 92.158,53.146C92.194,53.383 92.203,53.546 92.189,53.638C92.182,53.659 89.741,53.916 89.568,53.933C89.508,53.938 89.438,53.935 89.354,53.926C89.35,53.909 89.134,53.117 89.074,52.942C89.03,52.817 88.961,52.663 88.862,52.478C88.625,52.418 84.768,52.992 82.824,53.287C77.398,54.262 67.469,56.914 67.334,56.954L67.181,56.99C67.049,57.175 65.928,59.4 65.918,59.494C65.906,59.664 66.259,60.542 66.326,60.742C66.449,61.073 66.514,61.414 66.528,61.507C66.547,61.565 66.766,61.73 67.368,62.155C67.858,62.503 68.678,63.084 69.01,63.317C69.252,63.47 70.963,62.402 83.633,59.868C88.596,58.978 89.882,58.966 90.041,58.937L89.916,57.307C89.921,57.288 92.436,56.839 92.527,56.82C92.818,56.767 92.923,56.81 92.942,56.971C92.938,57.156 92.981,58.246 92.978,58.253C92.972,58.292 92.955,58.325 92.928,58.35M62.167,18.249C62.041,17.671 61.691,16.011 61.538,15.266C61.43,14.762 61.442,14.566 61.558,14.338C60.626,14.304 57.9,15.036 57.406,15.319C57.346,15.355 57.25,15.42 57.12,15.516C57.144,15.576 58.032,17.405 58.349,18.96L58.377,19.119M40.518,92.584C41.592,93.274 42.258,93.576 42.577,93.713L46.183,91.265C46.201,91.26 46.252,91.258 46.362,91.254M53.746,90.074C53.738,90.029 54.809,71.926 54.823,71.846C54.811,71.158 53.777,66.701 53.347,65.419C53.062,64.586 52.505,63.473 51.919,62.633C51.451,61.968 50.369,60.646 49.38,59.51C48.408,58.414 48.19,58.241 47.582,57.854L47.556,57.982M62.815,113.942L49.285,106.848M63.363,111.663L57.437,103.879C57.401,103.86 57.929,103.234 59.054,102.821C60.427,102.341 69.226,99.984 73.226,98.945C73.393,98.905 73.567,98.864 73.747,98.822C77.786,97.872 84.969,96.244 88.565,95.465C90.629,95.064 94.891,94.289 96.775,94.003C99.262,93.648 99.434,93.658 99.768,93.742L97.925,104.004M88.362,106.848C90.037,106.353 93.994,105.125 96.053,104.503C97.14,104.172 98.05,103.658 97.982,103.546C97.824,103.334 96.497,103.418 95.227,103.603C91.464,104.21 83.942,105.487 81.511,106.063C77.738,107.018 70.205,108.982 66.439,109.99C65.561,110.232 64.056,110.82 63.3,111.238C63.232,111.278 63.177,111.322 63.136,111.365M63.363,111.663C64.426,111.63 71.484,110.536 72.089,110.443M56.662,118.13L55.877,131.88C55.927,132.238 55.932,132.718 55.954,132.881C55.954,132.883 55.991,132.903 56.056,132.938C56.469,133.156 58.004,133.927 58.421,134.016C58.793,134.093 59.323,134.018 59.734,133.865C60.454,133.57 61.754,132.61 62.27,131.75C62.678,131.057 63.413,129.122 63.463,128.779L63.854,121.116L64.006,118.061C63.998,117.982 63.974,118.217 63.962,118.296L63.931,117.886C63.977,116.645 63.876,115.411 63.535,114.857C63.355,114.583 62.794,114.029 62.503,113.844C62.203,113.662 61.409,113.398 61.07,113.381C60.722,113.371 59.803,113.573 59.417,113.741C58.603,114.108 57.641,115.169 57.233,115.98C56.951,116.564 56.639,117.683 56.507,118.224L50.854,114.946M56.056,132.938C56.056,132.938 55.274,132.466 55.121,132.252C54.797,131.801 54.66,130.987 54.677,130.901L54.094,116.897L51.386,117.365L51.67,125.326L48.624,125.844L48.451,117.818C48.451,117.818 45.574,118.464 45.425,118.376C44.146,117.624 38.083,113.979 32.102,110.364L31.891,110.237M45.432,118.342L45.425,118.376C44.773,121.657 41.179,139.714 41.179,139.714C41.206,139.874 41.338,139.896 41.868,139.788C41.963,139.767 60.71,136.327 60.71,136.327L60.739,137.923C60.583,137.83 44.274,140.788 40.83,141.415L40.735,141.432C40.735,141.432 22.279,130.13 22.246,129.914L22.142,128.381L41.153,139.788M41.153,139.788L40.823,141.416M40.481,110.417L46.126,114.115L47.648,115.031M42.053,111.137C42.319,107.714 42.907,100.001 43.231,95.707C43.327,94.346 43.358,93.768 43.366,93.73C43.387,93.646 43.459,93.595 43.579,93.578C46.099,93.374 46.966,93.242 47.045,93.211L47.738,91.822C48.384,91.675 50.366,91.596 50.902,91.685M107.429,92.191L107.354,92.17C107.426,92.079 107.518,91.963 107.62,91.834C107.894,91.487 108.239,91.05 108.444,90.792C108.595,90.602 108.701,90.382 108.809,90.103C108.816,90.082 108.708,89.818 108.566,88.459L108.703,88.418L108.163,88.322L107.717,88.308C105.113,88.349 103.332,88.452 101.599,88.721C98.009,89.352 90.194,90.739 85.97,91.495C80.174,92.508 78.29,92.952 73.286,94.25C65.203,96.406 54.816,99.216 53.873,99.574C53.156,99.847 51.22,100.803 50.603,101.024C50.559,101.039 50.521,101.051 50.491,101.059C50.34,101.042 50.018,101.086 49.428,101.206C48.782,101.33 48.358,101.297 47.945,101.095C47.803,101.021 47.458,100.817 47.374,100.778C47.222,100.104 47.153,96.696 47.112,94.658C47.101,94.159 47.089,93.583 47.08,93.141M40.518,92.584C40.562,92.457 40.589,92.359 40.598,92.287C40.615,92.165 40.92,84.271 40.944,83.626L39.6,83.702L39.578,84.691C39.571,84.778 39.566,84.845 39.569,84.888C39.566,85.15 39.54,85.291 39.384,85.822C38.887,87.511 38.462,89.189 38.405,89.395L34.013,86.851C33.965,87.138 33.525,89.811 33.518,89.903L38.196,92.796L40.512,92.599C40.514,92.594 40.516,92.589 40.518,92.584M35.436,82.666L35.393,82.698M99.842,50.232C99.85,50.233 99.858,50.234 99.866,50.235C100.277,50.285 101.321,50.401 101.563,50.455C102.074,50.568 102.907,50.933 103.277,51.194C103.922,51.674 105.074,53.016 105.482,53.695C106.282,55.049 107.299,58.082 107.614,60.002C107.875,61.637 107.861,65.294 107.566,66.881C107.422,67.632 106.906,69.185 106.38,69.958C105.831,70.743 105.352,71.179 104.918,71.45M33.518,89.903L30.634,82.32L31.704,67.032C32.237,54.014 46.387,58.006 47.03,72.07C47.052,72.544 47.074,73.39 47.052,73.898L46.361,91.284M33.48,109.709L33.518,109.649C33.401,109.697 33.194,109.764 32.705,109.908C32.215,110.052 32.026,110.105 31.951,110.117L22.142,128.381M59.7,134.314L60.739,136.327M38.405,118.13C38.443,118.255 38.098,119.774 38.098,119.774L34.097,132.144L30.991,130.471C32.162,127.001 34.308,120.631 35.285,117.737C35.302,117.682 35.544,117.367 35.544,117.367C35.887,117.046 36.444,116.765 37.14,116.897C37.553,116.988 38.263,117.698 38.405,118.13ZM34.066,109.642C37.349,111.608 48.451,117.818 48.451,117.818M58.02,104.947L49.49,105.9M49.454,105.187C53.158,103.658 57.778,102.139 62.998,100.723C66.348,99.854 73.747,97.98 77.798,96.972C81.778,96.067 89.926,94.296 93.941,93.523C97.289,92.928 105.564,91.514 107.016,91.668C107.417,91.716 107.633,91.793 107.664,91.896C107.726,92.194 105.689,93.022 104.796,93.389C102.535,94.267 100.394,94.915 99.775,95.098M73.646,98.527L76.586,107.129M41.038,93.094L40.114,106.778C40.07,106.982 40.246,107.94 40.315,107.995C40.414,108.082 40.608,108.031 40.678,108.142C40.742,108.245 40.939,109.747 40.908,109.889C40.855,110.1 40.293,109.976 40.246,110.078C39.2,112.334 33.006,109.941 33.48,108.142C33.515,108.009 33.753,106.118 33.983,105.431M53.746,90.103L50.902,91.685L50.707,100.085C50.7,100.169 50.522,101.23 50.597,102.125C50.614,102.427 50.388,102.725 50.028,102.862C49.706,102.974 49.039,103.046 48.593,103.032C48.166,103.013 47.827,102.905 47.436,102.696L47.815,102.854C47.796,103.392 47.76,104.971 47.738,106.013C47.71,108.082 47.556,114.115 47.556,114.115M49.944,113.225C49.704,113.119 49.046,113.15 48.773,113.285C48.502,113.426 47.966,114.002 47.818,114.307C47.676,114.617 47.575,115.397 47.638,115.697C47.71,115.992 48.103,116.52 48.338,116.638C48.581,116.746 49.238,116.712 49.512,116.58C49.781,116.436 50.318,115.862 50.467,115.555C50.609,115.246 50.71,114.466 50.647,114.166C50.575,113.873 50.182,113.345 49.944,113.225ZM96.223,44.393C96.732,43.764 97.792,43.02 97.711,43.003C96.468,42.749 92.037,42.621 88.805,43.003C71.605,45.037 34.265,54.911 37.022,57.079C36.878,49.711 36.581,34.591 36.427,26.842C36.418,26.326 36.41,26.182 36.386,26.114C35.71,23.726 84.307,12.403 85.246,15.038L97.711,43.003M37.006,56.714L39.492,57.701M50.83,60.646C51.149,60.778 52.207,61.214 52.438,61.315C53.398,61.747 54.458,62.554 55.063,63.17C55.62,63.749 56.393,64.812 56.753,65.527C57.115,66.264 57.703,67.735 57.888,68.597C58.07,69.478 58.14,71.074 58.073,71.938C58.001,72.792 57.648,74.386 57.312,75.194C56.633,76.781 54.811,78.66 54.408,78.931M36.672,59.038C36.763,59.09 36.83,59.107 36.871,59.093C36.946,59.062 38.232,57.826 38.928,57.511C40.19,56.94 44.009,55.354 45.578,54.78C49.169,53.556 55.164,51.977 60.017,50.866C63.71,50.059 71.117,48.482 74.827,47.714C78.398,47.052 86.071,45.737 89.762,45.23C94.802,44.64 98.46,44.666 98.539,44.693C98.561,44.702 98.602,44.724 98.657,44.76L98.542,44.782L98.635,45.067C100.356,52.291 103.966,67.45 105.854,75.382C105.996,75.97 106.159,76.663 106.178,76.98L105.072,77.813C105.072,77.813 104.952,78.041 104.964,78.053C104.993,78.079 106.942,86.539 107.309,88.128M94.608,36.468L91.891,36.461C90.432,33.091 85.299,21.743 85.21,21.576C84.919,21.035 81.545,21.66 81.751,22.078L88.118,37.042C88.118,37.042 77.146,38.818 72.977,39.526C69.437,40.202 62.23,41.688 58.44,42.617C54.648,43.632 47.294,45.818 43.891,46.87C40.987,47.827 39.281,48.55 37.042,49.558M84.372,37.414L78.667,22.543C76.795,22.323 74.921,22.557 73.046,23.479L77.892,38.474M73.514,39.25L69.269,24.211C69.929,24.262 65.513,23.384 63.089,25.531L66.238,40.668M86.112,17.321C84.32,14.793 40.197,24.805 36.545,28.31M98.779,46.697C95.758,46.877 90.11,47.698 86.047,48.394C82.202,49.121 74.242,50.669 70.126,51.49C66.605,52.226 57.533,54.226 55.272,54.869C51.902,55.898 47.83,57.658 47.729,57.746L36.833,58.784M99.079,99.322L104.035,101.59M98.587,101.314L98.582,101.335C98.65,101.417 98.774,101.508 99.199,101.77C101.726,103.349 102.883,104.086 103.757,104.638C104.174,104.899 104.969,105.398 105.348,105.636L106.387,114.854L109.253,114.463L108.85,106.344L107.054,105.482M90.954,106.063L97.898,110.479M105.401,107.609C104.883,103.265 97.5,105.691 97.973,110.244L97.877,110.906C97.831,111.103 97.109,122.604 97.109,122.746C97.121,123.578 97.339,124.874 97.579,125.347C97.776,125.717 98.194,126.106 98.578,126.278C99.053,126.475 99.581,126.588 99.998,126.557C100.718,126.492 102.154,125.87 102.542,125.57C103.858,124.534 104.563,122.628 104.57,122.563L104.618,121.61L105.401,107.609ZM107.98,103.879C108.158,103.958 111.619,106.209 111.619,106.209M99.197,126.626L99.134,126.658C99.05,127.471 98.902,128.875 98.892,128.945C98.892,128.952 98.875,128.969 98.875,128.962L118.356,125.033L111.605,106.169L111.619,106.162C110.786,106.217 108.862,106.382 108.862,106.382M94.297,121.116L97.236,123.062M78.9,131.818C79.066,131.422 79.26,131.179 79.358,131.04C79.678,130.37 79.826,126.19 79.889,124.397C80.062,117.175 80.023,116.957 79.896,116.743C79.666,116.642 71.078,119.268 71.078,119.268C71.078,119.268 71.383,121.39 71.878,122.539C72.305,123.514 77.263,131.414 77.714,131.695C78.485,132.166 78.907,131.813 78.9,131.818ZM83.304,118.829C83.312,118.839 84.516,120.202 84.809,120.588C85.714,121.783 86.966,124.886 87.329,126.456C87.72,128.189 87.898,131.441 87.73,132.744C87.617,133.582 87.127,135.343 86.866,135.878C86.664,136.274 86.16,136.999 85.865,137.309C85.553,137.628 85.003,137.983 84.631,138.11C84.286,138.216 83.544,138.29 83.218,138.242C82.5,138.12 81.053,137.225 80.448,136.586C79.262,135.305 78.331,133.433 77.674,131.897L77.263,131.287M94.005,126.278L98.974,128.962C98.93,129.089 98.772,130.616 98.772,130.616L98.777,130.592M105.764,81.443C105.764,81.443 110.657,81.35 111.605,81.372M47.347,100.843L47.316,100.865C47.311,101.107 47.299,101.77 47.299,101.921C47.299,102.019 47.302,102.199 47.306,102.458M78.415,132.07L78.408,132.036L82.063,131.306C82.159,131.282 82.742,129.494 82.975,127.051C83.285,123.698 83.297,118.106 83.146,116.522C83.107,116.078 83.22,116.006 83.254,115.958C83.395,115.769 83.875,114.706 84.106,114.389C84.456,113.909 86.009,112.961 86.741,112.86C87.511,112.771 88.793,113.146 89.561,113.671C91.039,114.722 91.603,116.566 91.622,116.71C91.627,116.75 91.62,116.786 91.603,116.818L91.562,116.712C91.526,118.094 91.429,121.424 91.431,122.141M84.372,138.11C88.138,138.226 90.413,137.013 91.13,136.375C91.046,136.296 92.534,133.45 92.808,130.958C92.854,130.52 92.864,129.928 92.848,129.273C92.809,127.649 92.604,125.642 92.354,124.654C91.937,123.043 90.694,120.372 89.866,119.287C89.448,118.747 88.202,117.583 87.727,117.247C87.014,116.748 86.071,116.249 85.841,116.316C85.745,116.345 83.549,116.988 83.167,117.098L83.16,117.113L83.201,117.168M69.433,64.429C71.531,72.865 73.684,81.927 75.654,91.563C71.83,91.745 68.202,92.576 64.77,94.058C62.38,84.15 60.203,75.282 58.55,66.923C62.065,65.053 65.692,64.211 69.433,64.429ZM38.174,92.712L38.405,89.395M38.863,81.672L38.755,81.487C39.007,81.487 39.708,81.492 39.797,81.499C39.857,81.502 39.912,81.514 39.965,81.53C39.974,81.526 39.941,81.677 39.967,81.756C40.085,82.08 40.699,82.982 40.999,83.378L40.997,83.318M39.967,81.406L39.888,81.444C39.855,81.138 40.032,78.336 40.034,78.194M34.013,86.851C34.27,86.119 34.848,84.422 35.17,83.46C35.246,83.234 35.321,83.011 35.395,82.786L35.362,81.631L35.369,81.372C35.366,81.334 35.395,81.283 35.501,81.226C35.863,81.024 36.576,80.628 36.926,80.434C36.852,80.131 36.809,79.93 36.802,79.824C36.792,79.718 37.049,77.482 37.14,76.735L37.13,76.745C36.821,76.49 36.146,75.089 36.134,74.834C36.13,74.594 36.226,70.642 36.242,69.948C36.233,69.989 36.228,70.008 36.226,70.008C36.223,70.008 36.218,69.986 36.214,69.946C36.788,63.583 42.62,66.348 41.978,72.446C41.977,72.458 41.611,76.243 41.53,77.086C41.542,77.1 38.875,78.115 38.868,78.149C38.844,78.547 38.712,81.49 38.71,81.634C38.707,81.674 38.698,81.715 38.681,81.756C38.688,81.847 39.427,82.93 39.554,83.071C39.638,83.165 39.802,83.323 40.044,83.544M57.35,16.375C53.58,17.095 47.407,18.252 47.136,18.038C46.93,17.844 47.076,17.597 47.518,17.431C47.551,17.419 52.135,15.725 54.437,15.062C57.95,14.131 64.553,12.636 68.861,11.885C69.588,11.762 71.345,11.659 71.402,11.695C71.642,11.868 71.458,12.137 70.958,12.319C66.06,14.167 62.635,15.07 61.682,15.317M46.908,17.983C46.728,17.839 46.625,17.736 46.598,17.674C46.49,17.407 47.078,16.375 47.662,15.667C48.204,15.041 49.702,14.251 50.635,13.951C52.102,13.488 56.141,12.482 58.25,11.978C63.912,10.738 68.033,10.26 68.861,10.25C69.727,10.243 70.819,10.51 71.203,10.918C71.338,11.064 71.77,11.719 71.921,11.954L71.786,11.83M56.376,4.488L56.338,4.716C55.79,4.349 55.495,4.013 55.267,3.43C54.929,2.554 54.895,2.352 54.91,2.316L57.809,1.536C58.02,2.057 58.296,2.846 58.33,3.154C58.363,3.461 58.231,3.962 58.111,4.253L56.21,4.759L56.191,4.805L56.215,4.831L56.268,5.011C56.347,5.383 56.758,6.955 56.978,7.795C57.245,8.82 57.739,10.757 57.775,11.311C57.79,11.594 57.672,11.767 57.439,11.93L57.466,11.842M57.898,4.378C57.857,4.618 57.84,4.783 57.847,4.872C57.857,4.987 59.261,9.59 59.335,9.847C59.472,10.332 59.657,10.613 60.158,11.162L60.178,11.105M76.656,83.774L76.644,83.748L93.725,80.273L94.474,80.218L94.006,80.371C93.998,80.498 93.998,80.597 94.006,80.669C94.02,80.798 95.657,89.53 95.71,89.729L95.707,89.395M116.316,78.384L111.619,81.372C111.619,81.372 112.476,89.222 110.671,97.234L106.738,101.786C106.178,102.274 104.738,102.002 104.741,102.005C104.664,102.014 103.85,101.698 104.035,101.405L104.796,93.389L104.1,100.447M67.61,26.667C68.843,26.667 69.844,27.684 69.844,28.936C69.844,30.189 68.843,31.206 67.61,31.206C66.377,31.206 65.376,30.189 65.376,28.936C65.376,27.684 66.377,26.667 67.61,26.667ZM77.217,25.022C78.192,24.764 79.245,25.538 79.565,26.749C79.886,27.96 79.354,29.152 78.378,29.411C77.402,29.669 76.35,28.895 76.029,27.684C75.709,26.473 76.241,25.281 77.217,25.022ZM96.151,68.456C98.169,67.922 100.374,69.629 101.072,72.267C101.77,74.904 100.698,77.479 98.68,78.013C96.661,78.547 94.456,76.839 93.758,74.202C93.06,71.565 94.132,68.99 96.151,68.456ZM85.258,23.869C85.927,23.692 86.695,24.399 86.973,25.446C87.25,26.494 86.932,27.489 86.263,27.666C85.593,27.843 84.825,27.136 84.548,26.088C84.27,25.04 84.589,24.046 85.258,23.869Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:1.25px;"/>
        </g>
        <g transform="matrix(2.63651,0,0,3.16581,157.74,97.1)">
            <circle cx="108.318" cy="105.533" r="2.472" style="fill:none;stroke:#ECDBC5;stroke-width:1.43px;stroke-miterlimit:1.5;"/>
        </g>
    </g>
</svg>"""

MODEL_ICON_BD1 = """<svg width="100%" height="100%" viewBox="0 0 144 144" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" xml:space="preserve" xmlns:serif="http://www.serif.com/" style="fill-rule:evenodd;clip-rule:evenodd;stroke-linecap:round;stroke-linejoin:round;">
    <g id="Layer_2_3" transform="matrix(0.24,0,0,0.24,0,0)">
        <path d="M218.52,224.16L247.41,164.76L266.71,168.88" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M290.4,182.75C288.06,182.08 283.18,181.73 280.74,182.05C278.3,182.4 273.59,184.08 271.41,185.37C269.25,186.68 265.43,190.12 263.85,192.19C262.28,194.27 259.93,198.95 259.18,201.47C258.46,204 257.94,209.21 258.14,211.81C258.37,214.4 259.75,219.35 260.87,221.62C262.02,223.88 265.1,227.82 266.97,229.42C268.86,231 273.16,233.32 275.5,234.02C277.84,234.68 282.71,235.03 285.16,234.71C287.6,234.36 292.3,232.69 294.48,231.39C296.65,230.08 300.47,226.64 302.05,224.58C303.62,222.5 305.97,217.81 306.72,215.29C307.44,212.76 307.96,207.55 307.76,204.96C307.53,202.37 306.15,197.41 305.02,195.14C303.88,192.88 300.8,188.95 298.93,187.34C297.04,185.76 292.74,183.44 290.4,182.75Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <g transform="matrix(3.39466,0,0,3.42246,47.9958,31.4964)">
            <circle cx="69.96" cy="51.807" r="4.605" style="fill:none;stroke:#ECDBC5;stroke-width:1.22px;stroke-miterlimit:1.5;"/>
        </g>
        <path d="M231.88,236.21L231.75,236.05C240.942,247.276 292.334,248.622 286.65,236.05L365.129,236.05" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M292.11,184.02C292.06,183.75 291.64,183.25 290.81,183.04C284.27,181.42 273.07,180.26 267.65,180.28C265.16,180.29 261.42,180.77 260.67,180.91" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M276.54,235.35C276.19,235.2 272.19,234.06 269.71,233.22C263.65,231.41 255.73,227.63 250.61,224.22C244.92,220.32 242.98,219.3 242.71,219.34C242.42,219.38 242.23,219.5 242.15,219.67" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M423.08,222.5L403.551,166.994" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M331.76,272.81L349.19,271.56L310.07,235.95L287.18,235.95L331.76,272.81Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <g transform="matrix(1.16095,0,0,1.17911,-45.6589,-48.6914)">
            <path d="M266.17,249.46L265.49,249.12C265.76,249.29 278.14,261.83 282.34,266.13C283.84,267.74 283.89,269.15 283.4,271.85" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        </g>
        <path d="M173.97,374.15L201.41,349.66L200.95,334.24C201.34,333.94 247.3,295.77 247.5,295.59C249.61,293.81 258.39,290.33 261.45,289.5C264.15,288.79 269.01,288.09 272,288.01C275.18,287.95 280.95,288.69 284.06,289.52C287.33,290.42 293.01,293.14 295.43,294.89C297.5,296.41 302,301.04 303.46,303.07C305.05,305.31 307.34,310.04 308.14,312.81C309.49,317.58 310.24,327.1 309.78,331.9C309.22,337.48 307.32,347.13 305.63,350.87C304.11,354.19 299.65,360.42 294.92,364.56C291,367.96 256.96,396.04 256.68,396.26L257.25,396.37" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M173.79,375.12C170.59,375.72 166.09,379.52 164.68,381.55C163.5,383.33 161.7,387.6 161.12,390.02C160.56,392.45 160.11,397.69 160.24,400.4C160.39,403.11 161.37,408.52 162.18,411.11C163.02,413.69 165.27,418.44 166.65,420.52C168.04,422.58 171.23,425.95 172.96,427.19C175,428.6 180.54,430.58 183.75,430.02C186.95,429.41 191.45,425.62 192.86,423.59C194.05,421.81 195.85,417.53 196.43,415.12C196.99,412.69 197.43,407.45 197.31,404.74C197.16,402.02 196.18,396.62 195.36,394.03C194.53,391.44 192.28,386.7 190.89,384.62C189.5,382.56 186.32,379.19 184.58,377.94C182.55,376.53 177,374.56 173.79,375.12Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M238.24,398.27L237.81,398.04C238.74,398.21 252.41,398.14 254.85,398.36C255.16,398.37 255.47,398.09 255.51,398.01" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M213.53,400.4C213.45,400.18 213.38,400.03 213.33,399.94C213.33,399.93 213.34,399.93 213.34,399.93C216.94,405.72 224.7,418.3 227.64,423.48C230.68,428.87 236.72,439.33 240.36,445.29C241.94,447.79 244.53,451.97 244.56,453.56C244.57,454.92 243.8,458.9 243.77,459.33C243.67,460.96 249.18,470.43 252.21,475.67C258.15,486.2 272.54,513.39 273.01,512.2C273.02,512.16 273.05,512.19 273.09,512.29C272.57,512.55 268.01,515.09 265.33,516.45C264.19,517.02 262,516.27 258.22,515.11C257.72,514.97 256.49,514.63 256.35,514.76C255.33,515.83 253.47,517.04 251.8,517.46C246.34,518.8 234.72,518.07 231.3,517.04C224.81,515.02 218.75,509.14 217.92,509.21C216.23,509.25 205.38,508.26 201.38,508.28L196.61,510.05" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M316.44,395.12C316.36,395.3 316.53,395.95 318.83,398.21C331.99,411.1 389.71,462.64 391.07,464.3C392.31,465.83 393.6,469.36 393.77,471.68C393.96,474.78 391.35,480.85 388.83,484.3C387.08,486.68 385.34,488.56 384.09,489.82C382.84,491.62 380.31,494.46 379.7,494.84C379.29,495.09 373.6,496.41 368.16,495.58C361.13,494.48 354.39,492.31 354.15,492.34L353.98,492.49" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M270.923,385.333L278.11,393.39L338.24,394.19C341,395.68 341.65,396.25 342.48,397.38C342.85,397.89 343.33,398.42 343.91,398.99C353.61,408.11 367.63,421.1 371.95,424.96C374.2,427 379.26,427.74 381.94,429.72C385.49,432.36 404.95,451.02 409.47,455.56C411.63,457.83 412.79,460.16 413.14,461.37C413.52,462.79 413.92,468.15 413.57,471.44C412.93,477.07 409.24,485.61 406.12,490.61" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M233.66,399.06L233.78,399.34" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M273.68,512.69L273.51,512.94C273.65,512.91 277.68,510.75 279.37,509.53C280.52,508.69 284.69,503.88 285.56,502.15C286.9,499.44 286.48,495.16 286.07,493.38L285.47,491.42L235.01,397.87" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M168.57,528.06L168.73,528.36C168.43,528.77 157.02,528.92 152.08,529.29C147.3,529.68 144.04,530.2 142.84,529.68L142.75,529.19C142.5,529.45 140.23,531.22 136.43,533.26C135.16,533.95 128.86,537.38 128.44,537.57L128.97,546.12L128.25,546.23C128.89,546.78 135.63,546.69 146.58,550.76C148.45,551.51 150.91,552.48 151.83,553.14C153.61,554.37 158.59,555.1 160.1,555.14C160.68,555.15 170.03,555.11 170.03,555.11C169.92,554.8 169.8,554.49 169.69,554.19C169.57,553.87 169.45,553.55 169.33,553.23" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M248.58,295.27C251.16,294.48 258.08,292.4 260.12,291.98C263.07,291.4 269.86,291.44 272.19,291.82C275.61,292.41 282.7,295.9 284.68,297.41C286.71,299 290.57,303.94 291.8,306.29C293.15,308.91 294.76,313.71 295.4,317C295.96,319.99 296.18,327.11 295.87,330.11C295.53,333.22 293.77,340.53 292.56,343.79C290.39,349.47 282.15,359.88 282.06,359.92L236.85,397.56L213.73,399.86L213.65,409.44L183.95,431.06C183.59,432.05 184.73,434.98 187.03,439.68C190.11,446.07 196.52,456.87 198.43,460.46C200.36,464.13 201.55,465.54 203.04,466.15C205.31,467.09 206.84,469.06 207.9,471.55C208.95,474.04 211.61,479.77 212.82,482.36C214.03,484.91 214.3,486.31 213.79,487.53C212.75,489.99 212.52,493.38 212.54,493.55C212.91,495.74 213.91,499.8 213.93,499.83C213.99,499.88 213.58,501.42 213.25,502.7C213.28,502.5 213.38,501.92 213.36,502.11C212.94,502.97 212.62,503.22 210.55,504.09C195.03,510.93 170.83,521.6 169.49,522.51L168.72,524.71C168.57,526.88 168.64,528.64 168.72,528.35C168.82,527.95 168.77,526.73 168.8,527.14C168.82,527.32 169.17,551.05 170.37,566.08L170.58,567.11L171.68,567.92L198.49,570.53L216.28,561.84L246.82,567.98L250.38,574.67L268.93,579.84L272.33,588L322.92,597.08L324.39,596.92L353.81,580.4L354.3,550.12C351.37,549.37 344.12,547.52 339.79,546.42C334.03,544.95 333.78,544.87 333.75,544.28C333.39,541.45 333.03,538.45 333.09,538.19C332.21,536.56 311.69,515.2 304.79,508.22C303.96,507.38 302.75,507.35 301.21,508.15C298.37,509.63 288.25,502.6 288.25,502.6C289.73,505.53 303.51,525.32 304.24,526.53C305.94,529.38 307.04,533.03 306.87,534.07C306.68,535.01 305.45,536.23 304.63,536.46C302.57,536.98 298.96,536.28 297.27,534.85C295.35,533.18 281.15,512.97 280.86,512.52C280.79,512.41 280.72,512.26 280.64,512.06" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M351.24,549.68C344.82,553.2 330.81,560.89 323.23,565.04C322.47,565.46 321.71,565.88 320.95,566.3L294.15,560.37L262.21,514.86" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M323.68,564.38C323.85,565.02 324,565.77 324.09,566.69C324.71,573.68 323.35,596.95 323.4,597.98L323.92,596.6" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M302.66,507.24C302.58,507.22 302.2,506.9 302.1,506.68C301.97,506.33 302.22,502.23 302.21,502.11C302.14,501.42 301.91,500.32 301.8,500.11C301.72,499.9 316.62,494.13 321.64,492.36C331.43,489.13 333.56,488.24 334.12,488.54C336.87,490.01 340.07,491.25 344.44,492.3C348.54,493.26 351.8,493.36 354.62,492.99C355.5,492.86 356.38,491.69 355.71,490.84C355.64,490.76 335.01,470.19 331.82,466.4C329.3,463.35 328.88,463.19 328.22,463.17C325.97,463.12 322.76,461.98 319.45,459.2C319.04,458.86 284.74,427.76 283.06,425.79C279.49,421.49 275.84,413.49 274.94,409.84C273.79,405.05 273.44,396.67 274.54,394.16C274.76,393.67 275.11,393.28 275.27,393.26C275.34,393.26 275.52,393.29 275.6,393.27" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M374.98,496.41L400.61,497.72L422.13,531.79" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M406.67,493.29L406,492.96C406.15,492.72 406.36,492.73 407.01,493.06C407.56,493.34 413.96,495.95 414.55,496.58C415.96,498.27 423.47,509.36 426.01,512.31C427.33,513.83 429.65,515.66 430.84,516.12C432.18,516.62 433.68,516.26 434.18,515.69C434.62,515.1 434.87,513.02 434.3,511.14C433.4,508.28 426.5,496.98 424.9,493.97C423.77,491.86 422.24,490.33 420.27,489.08C415.46,486.06 411.65,485.96 411.11,486.27C410.5,486.65 410.2,487.05 410.01,487.7L411.42,485.61L430.95,489.82L456.01,521.39" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M453.86,542.17L424.08,533.54L456.34,523.84L481.72,534.51" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M455.42,568.57L400.7,561.07L397.84,556.58L396.11,553.51L388.67,523.86L315.18,516.75" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M295.87,562.03L335.18,541.96" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M394.94,553.13L379.42,549.72L374.25,542.01L353.56,538.99L352.04,538.83L334.84,544.55" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M481.16,534.32L454.01,542.35L454.71,568.21L481.85,559.68L481.16,534.32Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M162.85,152.95C162.05,152.98 160.49,154.01 159.75,155C157.51,158.11 155.23,169.37 154.92,174.6C154.62,179.84 155.27,190.32 156.17,194.78C156.7,197.32 158.49,202.81 159.83,204.67C160.58,205.66 162.15,206.69 162.95,206.71C163.75,206.68 165.31,205.65 166.05,204.66C168.29,201.55 170.56,190.29 170.88,185.06C171.18,179.82 170.53,169.34 169.63,164.88C169.1,162.34 167.31,156.85 165.97,154.99C165.22,154 163.65,152.97 162.85,152.95Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M165.67,158.25C164.83,159.39 163.6,161.19 163.08,162.16C161.89,164.46 160.47,170.76 160.07,174.34C159.74,177.43 159.85,183.12 160.09,185.53C160.92,193.19 164.46,200.58 166.69,203.91" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M224.78,209.83C226.32,203 225.42,165.92 226.6,159.86C227.23,156.73 229.44,150.24 231.76,147.76C232.84,146.62 234.32,145.91 235.84,145.54L240.98,144.55C241.41,144.45 398.19,145.01 398.2,145.01C398.49,145.01 401.09,146.08 401.85,146.84C404.14,149.24 405.67,155.61 405.99,158.04L406.39,159.2L415.39,164.23" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M393.61,144.68L353.66,129.5L272.92,130.65" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M228.66,156.61L193.18,146.02" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M276.81,144.18L275.63,126.89L253.8,119.04L207.26,122.2L205.76,122.72C205.9,123.06 202.01,126.59 200.75,128.35C199.58,130 197.79,134.19 197.29,136.31C196.17,141.25 197.69,148.2 198.52,149.62" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M224.31,157.18C224.19,157 223.55,146.22 223.52,138.92C223.52,138.28 223.71,134.64 224.75,133.2C225.09,132.74 228.3,129.93 228.25,129.99C228.4,129.9 229.47,129.88 229.48,129.88C230.83,129.71 267.49,129.5 276.36,129.45" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M247.58,162.89C276.14,162.27 343.1,160.83 381.48,160C393.86,159.73 407.43,159.44 408.62,159.41" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M385.11,174.93C382.4,175 377.09,176.23 374.59,177.37C372.11,178.53 367.66,181.81 365.77,183.87C363.9,185.95 360.98,190.78 359.98,193.45C359.01,196.13 358.07,201.78 358.11,204.64C358.19,207.51 359.37,213.11 360.45,215.75C361.56,218.37 364.69,223.07 366.64,225.07C368.62,227.04 373.21,230.14 375.74,231.19C378.28,232.22 383.63,233.23 386.35,233.19C389.07,233.11 394.38,231.88 396.87,230.74C399.36,229.58 403.81,226.3 405.7,224.24C407.57,222.16 410.49,217.33 411.49,214.66C412.46,211.98 413.4,206.34 413.35,203.47C413.28,200.6 412.1,195 411.01,192.37C409.9,189.74 406.78,185.04 404.82,183.04C402.85,181.07 398.26,177.98 395.73,176.92C393.19,175.89 387.83,174.88 385.11,174.93Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M396.05,185.52C394.09,185.11 390.07,185.03 388.09,185.38C386.12,185.75 382.38,187.22 380.69,188.3C379,189.39 376.11,192.18 374.96,193.82C373.82,195.48 372.22,199.16 371.78,201.12C371.37,203.08 371.3,207.1 371.65,209.08C372.01,211.05 373.49,214.79 374.56,216.48C375.66,218.16 378.45,221.05 380.09,222.21C381.75,223.34 385.44,224.94 387.4,225.38C389.36,225.8 393.38,225.87 395.36,225.52C397.33,225.15 401.07,223.68 402.76,222.61C404.45,221.51 407.34,218.72 408.49,217.08C409.63,215.42 411.23,211.74 411.66,209.78C412.08,207.82 412.15,203.8 411.8,201.83C411.44,199.85 409.96,196.12 408.89,194.42C407.79,192.74 405,189.85 403.36,188.7C401.7,187.56 398.01,185.96 396.05,185.52Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <g transform="matrix(2.54276,0,0,2.60525,110.835,76.3883)">
            <ellipse cx="111.859" cy="49.903" rx="4.362" ry="5.075" style="fill:none;stroke:#ECDBC5;stroke-width:1.62px;stroke-miterlimit:1.5;"/>
        </g>
        <path d="M187.26,380.24L224.74,347.5L228.17,339.42L256.41,326.04L262.9,339.69L236.23,365.87L237.33,369.28" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M198.29,401.26L239.8,366.76" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M228.76,377.09L245.75,376.74L273.94,353.07C276.63,350.59 280.23,345.3 280.81,344.17C282.09,341.61 283.27,334.17 282.9,330.73C282.69,328.92 280.76,322.28 279.11,319.56C277.36,316.77 273.45,313.1 270.59,311.77C266.92,310.13 261.56,308.87 258.34,309.37C256.96,309.59 251.3,310.97 251.03,310.89L219.31,337.51L212.83,355.03" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M357.3,129.29L357.49,124.06C357.78,123.8 365.19,123.52 365.4,123.53C366.44,123.59 367.33,126.82 367.48,129.37C367.57,131 367.56,133.71 367.5,134.7L367.42,134.42" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M359.77,122.44L362.95,76.13L364.92,123.35L359.77,122.44Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M363.73,11.33C362.54,11.2 361.23,13.86 361.37,15.81C361.53,17.55 363.1,19.04 364.1,18.69C364.61,18.5 365.91,17.05 366.11,16.02C366.31,14.85 365.78,13.01 365.36,12.66C364.42,11.96 363.13,13.74 363,13.81C362.76,14.43 363.37,13.47 362.96,15.93C362.94,16.04 362.92,16.31 362.9,16.72C363.58,16.94 363.8,17.26 363.78,18.39C363.74,22.58 362.85,44.22 362.98,66.63C362.95,70.28 362.86,78.14 362.91,78.19L363.01,78.08" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M179.51,78.74L183.718,148.956L172.7,148.956L179.51,78.74Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M225.39,122.33C224.89,121.99 222.43,122.38 222.08,122.44C217.79,123.02 207.75,124.71 205.37,125.9C202.29,127.47 199.82,129.73 199.15,130.7C198.04,132.38 197.16,136.67 197.18,144.15C197.3,151.28 195.34,213.56 195.34,213.56L197.4,216.6L218.93,226.73C219.13,227.09 230.29,232.57 231.46,233.03L233.1,233.19C233.19,232.98 248.45,204.36 263.62,175.94C264.99,173.38 266.93,169.75 267.49,168.69L268.97,168.37C298.59,167.86 364.28,166.72 400.37,166.09C406.02,166 412.86,165.88 414.05,165.86L434.5,226.54L423.24,223.07C425.657,243.039 370.287,244.707 359.24,232.09C358.275,230.987 354.31,228.44 349.17,222.01C347.38,219.73 344.15,215.13 343.4,213.71C342.69,212.35 342.56,210.8 343.09,207.9C344.12,202.08 345.32,194.36 345.68,191.89C344.67,191.67 340.74,187.54 340.4,185.56C340.26,184.68 340.62,182.76 341.06,182.03C343.19,178.64 346.96,180.89 347.25,181.32L355.79,169.1" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M221.99,118.41L222.16,114.53C221.61,113.85 206.86,114.67 202.42,116.52C201.22,117.03 198.34,119.11 196.72,121.03C195.08,122.98 192.74,128.41 192.38,130.03C191.29,135.13 191.46,146.98 191.46,146.96L161.8,152.82" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M388.31,371.01L388.77,342.27L390.4,338.08L393.76,330.94L399.16,301.15L364.88,310.14L353.43,339.48L352.91,340.71L358.81,354.45L358.91,377.75L358.73,378.46C358.88,378.61 359.67,379.1 360.41,379.43C360.72,379.56 361.35,379.81 362.31,380.18L363.86,380.25L387.41,371.4L388.43,371.57C389.14,372.72 388.83,380.13 387.84,380.81C387.01,381.34 382.59,382.92 382.06,383.26C380.78,384.15 379.57,389.36 378.99,389.72C378.48,390.01 362.61,394.62 359.1,395.74L338.49,392.84C338.17,392.73 337.83,392.62 336.22,391.19C334.36,389.52 332.2,385.15 331.45,379.61C330.74,374.23 330.95,363.59 331.09,363.59C325.3,363.38 312.37,362.96 305.23,362.76C299.34,362.66 298.18,363.27 297.94,363.53" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M398.67,315.67L408.77,296.33L364.68,270.8L344.01,271.15L341.14,270.88L252.99,276.19L241.39,287.37L240.25,298.21" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M401.508,292.099L359.17,298.96L323.74,272.54" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M388.96,305.3L388.7,304.93C388.52,307.22 389.86,339.23 389.79,339.87C389.75,340.25 389.64,340.47 389.46,340.53C389.44,340.54 389.39,340.53 389.33,340.5" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M289.45,289.69C289.88,289.47 290.76,289.42 292.18,289.6C296.92,290.2 310.34,293.36 316.94,294.97C323.88,296.89 339.91,301.24 341.41,301.59C346.33,302.75 349.12,303.64 349.76,304.83C350.33,306.14 350.64,309.82 350.27,313.77C349.7,319.68 345.37,350.11 345.37,350.11L310.22,350.18L308.09,350.27" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M230.13,460.07C230.75,461.31 231.06,462.22 231.06,462.6C231.1,463.48 231.65,467.83 232.2,468.78C232.26,468.87 235.34,472.33 235.84,473.67C236.4,475.26 236.77,478.79 236.65,480.57C236.5,482.48 235.39,484.68 234.15,485.59C232.7,486.59 228.87,487.66 226.79,487.56C224.97,487.43 220.51,485.44 219.39,484.15C218.25,482.79 216.69,478.81 216.2,477.53L195.27,438L212.57,427.19C213.07,427.84 230.07,459.96 230.13,460.07Z" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M316.46,400.55C315.24,400.54 308.98,409.4 307.61,412.72C307.26,413.76 309.33,416.88 313.07,420.14C330.91,434.98 333.38,437.51 333.56,437.49C334.72,437.1 346.45,426.9 346.98,426.22C347.19,425.93 347.3,425.26 347.2,425.17C347.16,425.14 347.04,425.1 346.86,425.05" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M359.26,437.87L359.17,437.46C360.39,441.04 360.55,444.64 360.16,446.91C359.42,450.94 353.46,462.36 353.37,462.89C353.22,464.04 357.32,468.64 361.72,472.59C362.53,473.29 370.61,472.05 380.55,470.05C390.86,467.79 393.78,467.01 393.85,467.27L393.85,467.59C394.57,467.71 401.59,468.09 402.22,468.07C406.4,467.92 411.79,464.89 413.48,463.52L413.13,463.28" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M269.38,580.36C267.24,573.59 262.58,558.82 260.05,550.83C259.8,550.03 259.55,549.23 259.29,548.42L195.23,535.38L178.7,568.13" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M161.41,207.07L196.75,217.12" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M332.74,488.36L350.46,485.76" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
        <path d="M399.47,498.1L399.1,498.13C399.39,497.8 399.7,497.58 400.02,497.46C400.52,497.27 410.64,494.67 410.64,494.71" style="fill:none;fill-rule:nonzero;stroke:#ECDBC5;stroke-width:5.2px;"/>
    </g>
</svg>"""


MODEL_ICON_ASERIES = """<svg width="100%" height="100%" viewBox="0 0 144 144" version="1.1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" xml:space="preserve" xmlns:serif="http://www.serif.com/" style="fill-rule:evenodd;clip-rule:evenodd;stroke-linecap:round;stroke-linejoin:round;">
<g id="Layer1">
        <path d="M102.201,37.889L76.457,38.312C76.43,38.331 75.986,32.962 75.794,31.82C75.686,31.198 75.458,30.238 75.333,30.008C75.175,29.734 74.609,29.398 74.114,29.247C73.207,28.988 69.758,28.945 69.074,28.935L68.222,19.136L77.875,19.361C78.333,18.865 79.658,17.422 80.27,17.026C81.033,16.541 82.608,16.16 83.359,16.138C84.137,16.124 85.896,16.361 86.829,16.606C87.609,16.815 89.352,17.463 90.18,17.852C91.077,18.281 92.652,19.297 93.281,19.851C94.615,21.046 96.393,23.89 96.945,24.771L99.469,24.923M101.554,47.957L106.704,58.862M12.782,111.858L11.793,111.507L10.776,115.312L12.085,117.456L9.888,121.73L44.363,143.31L58.424,141.485L59.554,139.593L57.026,123.279L56.691,120.789M60.77,117.509L57.026,123.279C81,127.067 94.117,122.326 98.175,120.302M73.789,80.849L73.789,71.701C85.868,73.112 97.882,71.774 103.326,70.283C103.326,70.283 102.914,93.278 102.901,93.392C103.055,94.303 92.408,96.819 82.678,96.715C80.653,96.694 78.668,96.559 76.829,96.272M57.863,108.334L57.863,80.335C63.335,81.238 68.667,81.516 73.789,80.849C74.811,80.716 75.824,80.545 76.829,80.335L76.829,112.272C70.541,113.659 64.627,113.919 59.225,112.633M57.026,123.279C57.026,123.279 57.026,123.279 57.026,123.279ZM28.287,67.657L36.788,99.744M56.766,107.915L56.769,107.938L39.428,109.969L42.095,119.569M44.146,122.401L44.253,122.548M45.426,66.476L39.58,64.51M39.58,64.51L40.082,58.76M40.082,58.76L39.01,58.295L44.134,47.918M44.134,47.918L44.134,47.918ZM106.704,58.862L113.253,58.91L119.921,61.697M119.921,61.697L129.706,88.369M129.147,90.205L133.081,99.104M133.081,99.104L133.868,99.079L136.814,107.006M136.814,107.006L134.14,111.314L133.578,111.392M115.723,91.691L115.021,89.752M115.021,89.752L129.706,88.369M129.147,90.205L115.723,91.691M118.981,100.697L133.081,99.104M99.596,113.917L105.503,116.818M105.503,116.818L113.586,120.789M12.782,111.858L15.119,120.219L35.983,131.201L41.928,119.703L28.287,67.657L20.83,64.562L9.793,101.161L12.782,111.858M45.426,66.476L55.135,97.549L54.968,97.739M54.968,97.739L53.92,98.935L56.691,107.922L56.766,107.915M56.766,107.915L57.702,107.826L60.585,116.926M60.585,116.926L60.612,117.01M60.612,117.01C62.973,117.275 81.255,119.084 99.319,113.995M99.319,113.995C99.411,113.969 99.503,113.943 99.596,113.917M99.596,113.917C101.225,113.451 102.852,112.929 104.464,112.345M104.464,112.345L105.118,62.378M105.118,62.378L105.12,62.189M105.12,62.189L105.147,60.108M105.147,60.108L105.149,59.97C83.198,63.157 61.536,62.383 40.082,58.76M40.082,58.76L39.58,64.51M39.58,64.51C42.274,65.329 45.516,66.348 45.426,66.476M45.426,66.476C45.273,66.694 28.287,67.657 28.287,67.657M54.968,97.739L55.187,97.714M60.77,117.509L60.053,117.588M60.053,117.588L42.095,119.569M44.363,143.31L44.253,122.548M60.612,117.01L60.77,117.509M56.691,120.789L44.221,122.282M44.221,122.282L44.146,122.401M44.146,122.401L37.865,132.449M37.865,132.449L36.047,131.284M39.58,64.51C37.684,63.933 36.059,63.454 36.059,63.454M36.059,63.454L20.823,64.55M106.704,58.862L105.147,60.108M105.147,60.108L105.146,60.11M119.921,61.697L105.12,62.189M105.12,62.189L105.05,62.192M129.706,88.369L129.147,90.205M118.981,100.697L115.723,91.691M115.021,89.752L105.118,62.378M105.118,62.378L105.05,62.192M12.782,111.858L12.828,111.874M54.968,97.739L36.788,99.744M22.209,76.354L26.797,77.519L31.313,96.899L26.924,109.437L21.723,106.329L19.069,92.73L22.209,76.354ZM118.981,100.697L121.582,108.745L136.814,107.006L121.582,108.745L113.586,120.789L117.702,120.323C117.702,120.323 122.682,111.361 122.544,111.195C122.406,111.028 133.187,110.532 133.187,110.532L133.578,111.392M99.319,113.995L99.575,119.524C99.575,119.524 99.479,119.588 99.286,119.703C99.121,119.8 98.885,119.934 98.577,120.096C98.454,120.16 98.321,120.229 98.175,120.302L98.577,120.096C103.034,122.035 121.686,130.764 121.686,130.764L134.446,129.984L135.575,115.218L133.578,111.392M102.127,34.412C102.077,34.203 101.316,29.835 100.677,27.884C100.041,25.971 98.445,22.609 97.344,20.917C96.302,19.323 93.713,16.323 92.414,15.152C91.111,13.985 87.837,11.725 86.141,10.868C84.283,9.937 80.983,8.773 78.955,8.333C77.095,7.937 73.128,7.645 71.381,7.736C69.633,7.834 65.721,8.549 63.914,9.142C61.944,9.797 58.788,11.307 57.041,12.433C55.447,13.467 52.442,16.059 51.264,17.365C50.069,18.697 47.863,21.893 46.975,23.629C46.051,25.448 45.019,28.436 44.474,30.757C44.191,31.957 44.081,33.351 44.081,33.351L44.052,39.692L44.134,47.918C44.629,48.158 47.582,48.372 49.195,48.59C53.356,49.13 62.185,49.615 65.864,49.852C69.541,50.015 80.943,49.785 80.943,49.785L101.554,47.957L102.127,34.412ZM84.525,20.175C84.792,19.961 85.581,19.613 85.937,19.551C86.685,19.431 88.125,19.772 88.829,20.132C89.409,20.439 90.518,21.38 90.921,21.869C91.32,22.361 92.016,23.638 92.201,24.269C92.409,25.033 92.453,26.511 92.184,27.219C92.049,27.555 91.55,28.258 91.289,28.477C91.02,28.688 90.233,29.036 89.877,29.101C89.129,29.218 87.689,28.877 86.985,28.52C86.405,28.213 85.293,27.272 84.89,26.782C84.494,26.288 83.796,25.011 83.613,24.38C83.402,23.619 83.361,22.138 83.628,21.43C83.762,21.094 84.264,20.393 84.525,20.175ZM83.784,21.877C84.091,21.649 84.662,21.346 85.039,21.253C85.284,21.197 86.208,21.262 86.493,21.334C86.88,21.445 87.54,21.857 87.837,22.179C88.411,22.825 89.066,24.349 89.21,25.34C89.268,25.801 89.225,26.59 89.105,26.989C88.977,27.38 88.519,27.901 88.159,28.088C87.569,28.378 86.705,28.263 86.369,28.196M45.278,27.908L61.027,27.703" style="fill:none;stroke:#ECDBC5;stroke-width:1.25px;"/>
    </g>
</svg>"""


LOADING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connecting to Droid...</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .loading-wrap { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; text-align: center; padding: 40px 20px; max-width: 400px; margin: 0 auto; position: relative; z-index: 1; }
  .loading-wrap .r2-icon { width: 80px; height: 80px; margin-bottom: 28px; }
  .status { font-size: 12px; color: var(--muted); letter-spacing: 0.12em; text-transform: uppercase; margin-bottom: 28px; min-height: 20px; }
  .progress-track { width: 240px; height: 3px; background: rgba(255,255,255,0.05); border-radius: 2px; margin: 0 auto 28px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--blue); border-radius: 2px; width: 0%; transition: width 0.8s ease; box-shadow: 0 0 8px rgba(0,168,255,0.5); }
  .hint { font-size: 11px; color: var(--muted); line-height: 1.8; margin-bottom: 28px; opacity: 0.7; }
  .error-msg { display: none; color: var(--error); font-size: 12px; margin-bottom: 16px; line-height: 1.6; }
  .error-msg.show { display: block; }
  .btn-back { background: transparent; border: 1px solid var(--edge); border-radius: 10px; color: var(--muted); font-family: var(--font-head); font-size: 13px; font-weight: 600; padding: 10px 24px; cursor: pointer; transition: border-color 0.2s, color 0.2s; }
  .btn-back:hover { border-color: var(--blue); color: var(--blue); }
</style>
</head>
<body>
<div class="loading-wrap">
  <div class="r2-icon">{r2_svg}</div>
  <h1>Loading Sound Profile</h1>
  <div class="status" id="status">Initializing sound mapper...</div>
  <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
  <div class="hint">Loading your sound map and connecting<br>to the droid brain. This may take a moment.</div>
  <div class="error-msg" id="errorMsg">Could not connect. Make sure your droid is powered on and try again.</div>
  <button class="btn-back" onclick="goBack()">&#8592; Back to Droid Settings</button>
</div>
<script>
  const SETTINGS_URL = 'http://' + location.hostname + ':5001';
  const MAPPER_URL   = 'http://' + location.hostname + ':5000';
  const MAX_WAIT = 120;
  let elapsed = 0;

  const messages = [
    [0,  'Starting sound mapper...'],
    [4,  'Loading sound profile...'],
    [8,  'Connecting to droid brain...'],
    [20, 'Almost ready...'],
  ];

  function getMessage(sec) {
    let msg = messages[0][1];
    for (const [t, m] of messages) { if (sec >= t) msg = m; }
    return msg;
  }

  function goBack() { window.location.href = SETTINGS_URL; }

  async function checkMapper() {
    elapsed++;
    document.getElementById('progressFill').style.width = Math.min(95, (elapsed / MAX_WAIT) * 100) + '%';
    document.getElementById('status').textContent = getMessage(elapsed);

    if (elapsed >= MAX_WAIT) {
      document.getElementById('status').textContent = 'Connection timed out';
      document.getElementById('errorMsg').classList.add('show');
      return;
    }

    try {
      const res = await fetch(SETTINGS_URL + '/mapper_status');
      const data = await res.json();
      if (data.ready) {
        document.getElementById('progressFill').style.width = '100%';
        document.getElementById('status').textContent = 'Connected! Loading...';
        setTimeout(() => { window.location.href = MAPPER_URL; }, 400);
        return;
      }
    } catch(e) {}

    setTimeout(checkMapper, 1000);
  }

  setTimeout(checkMapper, 1000);
</script>
</body>
</html>"""


WIZARD_WELCOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Welcome to KYBER</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Welcome to KYBER</h1>
    <p class="subtitle">Let's get your droid set up</p>
  </div>
  <p class="wizard-question">Do you have a droid ready to pair right now?</p>
  <div class="wizard-choice-row">
    <a class="btn-wizard-choice" href="/?path=yes">Yes, it's ready to go</a>
    <a class="btn-wizard-choice" href="/?path=no">Not yet</a>
  </div>
</div>
</body>
</html>"""


WIZARD_KEYS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Logic Core</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .setup-notice { background: rgba(255,200,87,0.08); border: 1px solid rgba(255,200,87,0.3); border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; color: var(--warning); font-size: 12px; line-height: 1.8; }
  .setup-notice strong { display: block; font-size: 11px; letter-spacing: 0.2em; text-transform: uppercase; margin-bottom: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Logic Core Connections</h1>
  </div>
  {breadcrumbs}
  <div class="setup-notice">
    <strong><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>API Keys Needed</strong>
    Your droid needs at least one Speech-to-Text and one AI Personality engine configured before it can think. Pick your providers below and drop in their keys — Deepgram and Gemini are solid free defaults if you're not sure where to begin.
  </div>
  {alert_html}
  <form method="POST" action="/setup/save_keys" autocomplete="off">
    <input type="hidden" name="path" value="{path}">
    <div class="panel">
      <div class="panel-header">Logic Core Connections</div>
      <div class="panel-body">
        {logic_core_panel}
      </div>
    </div>
    <div class="btn-row">
      <button type="submit" class="primary">Continue</button>
    </div>
  </form>
</div>
</body>
</html>"""


WIZARD_IDENTITY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Identity</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .ident-select { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 12px 14px; color: var(--text); font-family: var(--font-mono); font-size: 14px; outline: none; margin-bottom: 18px; }
  .ident-section-label { font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
  .trait-row { display: flex; flex-direction: column; gap: 4px; }
  .trait-head { display: flex; justify-content: space-between; align-items: baseline; }
  .trait-name { font-size: 12px; font-weight: 600; color: var(--muted); }
  .trait-value { font-size: 14px; font-weight: 700; color: var(--blue); }
  .trait-slider { -webkit-appearance: none; -moz-appearance: none; appearance: none; width: 100%; height: 4px; border-radius: 2px; outline: none; margin: 8px 0 4px; background: var(--edge); }
  .trait-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 16px; height: 16px; border-radius: 50%; background: var(--blue); cursor: pointer; border: 2px solid var(--void); margin-top: -6px; }
  .trait-slider::-moz-range-thumb { width: 16px; height: 16px; border-radius: 50%; background: var(--blue); cursor: pointer; border: 2px solid var(--void); }
  .trait-slider::-moz-range-track { background: var(--edge); height: 4px; border-radius: 2px; }
  .trait-slider::-moz-range-progress { background: var(--blue); height: 4px; border-radius: 2px; }
  .fork-box { font-size: 12px; color: var(--warning); background: rgba(255,200,87,0.08); border: 1px solid rgba(255,200,87,0.25); border-radius: 10px; padding: 12px; margin: 4px 0 16px; line-height: 1.6; display: none; }
  .fork-box.show { display: block; }
  .slot-list { display: flex; flex-direction: column; gap: 6px; margin: 10px 0 4px; }
  .slot-row { display: flex; align-items: center; gap: 10px; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 8px 12px; cursor: pointer; }
  .slot-row input[type="radio"] { accent-color: var(--blue); width: 14px; height: 14px; flex-shrink: 0; cursor: pointer; }
  .slot-row span { flex: 1; font-size: 12px; color: var(--text); }
  .slot-row span.occupied { color: var(--warning); }
  .ident-divider { border: none; border-top: 1px solid var(--edge); margin: 20px 0; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Identify your droid</h1>
  </div>
  {breadcrumbs}
  <form method="POST" action="/setup/save_identity" autocomplete="off" id="identityForm">
    <input type="hidden" name="path" value="{path}">
    <input type="hidden" name="model" id="modelField" value="{preselect_model}">
    <input type="hidden" name="selected" id="selectedField" value="{preselect}">

    <div class="ident-section-label">Choose model</div>
    <div class="model-grid">
      <div class="model-card" data-model="r2d2"><span class="model-icon">{icon_r2d2}</span>R-Series</div>
      <div class="model-card" data-model="bb8"><span class="model-icon">{icon_bb8}</span>BB-Series</div>
      <div class="model-card" data-model="chopper"><span class="model-icon">{icon_chopper}</span>C-Series</div>
      <div class="model-card" data-model="aseries"><span class="model-icon">{icon_aseries}</span>A-Series</div>
      <div class="model-card" data-model="bd1"><span class="model-icon">{icon_bd1}</span>BD-Series</div>
    </div>

    <hr class="ident-divider">

    <div class="ident-section-label">Name your droid</div>
    <input type="text" class="ident-select" name="DROID_NAME" id="droidNameInput" value="{DROID_NAME_RAW}" placeholder="##-###" maxlength="10" autocomplete="off">
    <p class="wizard-note" style="margin-top:-10px;">Optional — you can always set this later from the Mainframe.</p>

    <hr class="ident-divider">

    <div class="ident-section-label">Personality</div>
    <p class="wizard-note" style="margin-top:0;">Use the defaults as-is, or as a starting point — nothing here is locked in.</p>
    <select class="ident-select" id="personalitySelect" onchange="onPersonalityChange(this.value)">
      <optgroup label="Custom">
        {custom_options}
      </optgroup>
      <optgroup label="Defaults">
        {default_options}
      </optgroup>
    </select>
    <div class="panel">
      <div class="panel-body">
        <div class="trait-row">
          <div class="trait-head"><span class="trait-name">Brave</span><span class="trait-value" id="v-brave">3</span></div>
          <input class="trait-slider" type="range" min="1" max="5" step="1" id="s-brave" oninput="onSliderInput('brave', this)">
        </div>
        <hr class="divider">
        <div class="trait-row">
          <div class="trait-head"><span class="trait-name">Curious</span><span class="trait-value" id="v-curious">3</span></div>
          <input class="trait-slider" type="range" min="1" max="5" step="1" id="s-curious" oninput="onSliderInput('curious', this)">
        </div>
        <hr class="divider">
        <div class="trait-row">
          <div class="trait-head"><span class="trait-name">Sassy</span><span class="trait-value" id="v-sassy">3</span></div>
          <input class="trait-slider" type="range" min="1" max="5" step="1" id="s-sassy" oninput="onSliderInput('sassy', this)">
        </div>
        <hr class="divider">
        <div class="trait-row">
          <div class="trait-head"><span class="trait-name">Playful</span><span class="trait-value" id="v-playful">3</span></div>
          <input class="trait-slider" type="range" min="1" max="5" step="1" id="s-playful" oninput="onSliderInput('playful', this)">
        </div>
        <hr class="divider">
        <div class="trait-row">
          <div class="trait-head"><span class="trait-name">Sensitive</span><span class="trait-value" id="v-sensitive">3</span></div>
          <input class="trait-slider" type="range" min="1" max="5" step="1" id="s-sensitive" oninput="onSliderInput('sensitive', this)">
        </div>
      </div>
    </div>
    <div class="fork-box" id="forkBox">
      Editing a default — give it a name and pick a slot to save it to:
      <div class="field" style="margin:10px 0;">
        <label class="field-label">Name</label>
        <div class="input-wrap"><input type="text" id="forkNameInput" placeholder="e.g. My R2 Tweaks"></div>
      </div>
      <div class="slot-list" id="forkSlotList"></div>
    </div>

    <div class="btn-row">
      <button type="submit" class="primary" id="nextBtn">Continue</button>
    </div>
  </form>
</div>
<script>
const profiles = {profiles_json};
const occupiedSlots = {occupied_json};
let currentKey = "{preselect}";
let dirty = false;

document.querySelectorAll('.model-card').forEach(function(card) {
  if (card.dataset.model === document.getElementById('modelField').value) {
    card.classList.add('active-model');
  }
  card.addEventListener('click', function() {
    document.querySelectorAll('.model-card').forEach(function(c) { c.classList.remove('active-model'); });
    card.classList.add('active-model');
    document.getElementById('modelField').value = card.dataset.model;
  });
});

function fillSlider(el) {
  const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
  el.style.background = `linear-gradient(to right, var(--blue) 0%, var(--blue) ${pct}%, var(--edge) ${pct}%, var(--edge) 100%)`;
}

function applyTraits(traits) {
  for (const key of ['brave','curious','sassy','playful','sensitive']) {
    const el = document.getElementById('s-' + key);
    el.value = traits[key];
    fillSlider(el);
    document.getElementById('v-' + key).textContent = traits[key];
  }
}

function onPersonalityChange(key) {
  currentKey = key;
  document.getElementById('selectedField').value = key;
  applyTraits(profiles[key].traits);
  dirty = false;
  updateForkBox();
}

function onSliderInput(traitKey, el) {
  fillSlider(el);
  document.getElementById('v-' + traitKey).textContent = el.value;
  dirty = true;
  updateForkBox();
}

function currentTraits() {
  const t = {};
  for (const key of ['brave','curious','sassy','playful','sensitive']) {
    t[key] = parseInt(document.getElementById('s-' + key).value);
  }
  return t;
}

function updateForkBox() {
  const box = document.getElementById('forkBox');
  const locked = profiles[currentKey].locked;
  if (locked && dirty) {
    box.classList.add('show');
  } else {
    box.classList.remove('show');
  }
}

document.getElementById('forkSlotList').innerHTML = ['1','2','3','4','5'].map(function(slot) {
  const occ = occupiedSlots[slot];
  const label = occ ? '<span class="occupied">Slot ' + slot + ' — "' + occ + '" (will overwrite)</span>' : '<span>Slot ' + slot + ' — empty</span>';
  return '<label class="slot-row"><input type="radio" name="forkSlot" value="' + slot + '">' + label + '</label>';
}).join('');

document.getElementById('identityForm').addEventListener('submit', async function(e) {
  e.preventDefault();
  const locked = profiles[currentKey].locked;
  if (locked && dirty) {
    const radio = document.querySelector('input[name="forkSlot"]:checked');
    if (!radio) { return; }
    const forkName = document.getElementById('forkNameInput').value.trim();
    await fetch('/save_personality', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: forkName, traits: currentTraits(), target_slot: radio.value, source_slot: currentKey})
    });
    document.getElementById('selectedField').value = radio.value;
  } else if (!locked && dirty) {
    await fetch('/save_personality', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: '', traits: currentTraits(), source_slot: currentKey})
    });
  }
  e.target.submit();
});

applyTraits(profiles[currentKey].traits);
</script>
</body>
</html>"""


WIZARD_WIFI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Wifi</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .wizard-note { text-align: center; font-size: 12px; color: var(--muted); margin: -8px 0 20px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Connect to wifi</h1>
    <p class="wizard-step">Step 3 of 6</p>
  </div>
  <p class="wizard-question">KYBER needs internet access to think.</p>
  {soft_note}
  <div class="panel">
    <div class="panel-header">Saved networks</div>
    <div class="panel-body">
      <div id="netList"><div class="bt-empty">Loading...</div></div>
      <input id="netSsid" placeholder="Network name" autocomplete="off">
      <input id="netPsk" type="password" placeholder="Password" autocomplete="off">
      <button class="btn-net-add" onclick="addNetwork()">Connect</button>
    </div>
  </div>
  <div class="btn-row">
    <a class="btn-go" href="/?path={path}">Continue</a>
  </div>
</div>
<script>
async function loadNetworks() {
  try {
    const res = await fetch('/network_list');
    const data = await res.json();
    const el = document.getElementById('netList');
    if (!data.networks || !data.networks.length) { el.innerHTML = '<div class="bt-empty">No saved networks yet</div>'; return; }
    el.innerHTML = data.networks.map(n => `
      <div class="net-row">
        <div class="net-dot ${n.connected ? 'on' : 'off'}"></div>
        <div class="net-ssid ${n.connected ? 'connected' : ''}">${n.ssid}</div>
      </div>
    `).join('');
  } catch(e) {}
}
async function addNetwork() {
  const ssid = document.getElementById('netSsid').value.trim();
  const psk = document.getElementById('netPsk').value;
  if (!ssid) return;
  try {
    const res = await fetch('/network_add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid, psk})
    });
    const data = await res.json();
    if (data.ok) {
      document.getElementById('netSsid').value = '';
      document.getElementById('netPsk').value = '';
      setTimeout(() => { window.location.href = '/?path={path}'; }, 3000);
    }
  } catch(e) {}
}
loadNetworks();
</script>
</body>
</html>"""


WIZARD_CLAIM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Find Your Droid</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .claim-steps { display: flex; flex-direction: column; gap: 2px; background: rgba(255,255,255,0.03); border: 1px solid var(--edge); border-radius: 12px; padding: 6px 16px; margin-bottom: 18px; }
  .claim-step-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; font-size: 13px; color: var(--text); }
  .claim-step-num { width: 20px; height: 20px; border-radius: 50%; background: var(--edge); color: var(--text); font-size: 11px; font-family: var(--font-mono); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Find your droid</h1>
  </div>
  {breadcrumbs}
  <div class="claim-steps">
    <div class="claim-step-row"><span class="claim-step-num">1</span>Power on your droid.</div>
    <div class="claim-step-row"><span class="claim-step-num">2</span>Keep it nearby.</div>
    <div class="claim-step-row"><span class="claim-step-num">3</span>Scan for the droid.</div>
  </div>
  <div class="bt-section">
    <div class="bt-section-header">Nearby droids<button class="btn-scan" onclick="scanDroids()">Scan</button></div>
    <div id="droidList"><div class="bt-empty">Tap Scan to search</div></div>
  </div>
</div>
<script>
async function scanDroids() {
  document.getElementById('droidList').innerHTML = '<div class="bt-empty">Scanning...</div>';
  try {
    const res = await fetch('/droid_scan');
    const data = await res.json();
    const el = document.getElementById('droidList');
    if (!data.droids || !data.droids.length) { el.innerHTML = '<div class="bt-empty">No droid found — make sure it\\'s powered on and close by</div>'; return; }
    el.innerHTML = data.droids.map(d => `
      <div class="bt-device">
        <div class="bt-dot connected"></div>
        <div class="bt-info"><div class="bt-name">Droid</div><div class="bt-mac">${d.mac}</div></div>
        <div class="bt-actions"><button class="btn-pair" onclick="claimDroid('${d.mac}')">Activate</button></div>
      </div>
    `).join('');
  } catch(e) { document.getElementById('droidList').innerHTML = '<div class="bt-empty">Scan failed</div>'; }
}
async function claimDroid(mac) {
  try {
    const res = await fetch('/droid_claim', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac})
    });
    const data = await res.json();
    if (data.ok) { window.location.href = '/?path={path}'; }
  } catch(e) {}
}
</script>
</body>
</html>"""


WIZARD_MIC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Communication Device</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .wizard-note { text-align: center; font-size: 12px; color: var(--muted); margin: 12px 0 0; }
  .connected-status-row { display: flex; align-items: center; gap: 8px; background: rgba(255,255,255,0.03); border: 1px solid var(--edge); border-radius: 10px; padding: 10px 14px; margin-bottom: 14px; font-size: 12px; color: var(--muted); }
  .connected-status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
  .connected-status-dot.on { background: var(--success); }
  .connected-status-mac { font-family: var(--font-mono); font-size: 10px; color: var(--muted); margin-top: 2px; }
  .mic-blocked-note { background: rgba(230,57,70,0.08); border: 1px solid rgba(230,57,70,0.3); border-radius: 10px; padding: 12px 14px; margin-top: 14px; font-size: 12px; color: var(--error); line-height: 1.6; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Connect your communication device</h1>
  </div>
  {breadcrumbs}
  <p class="wizard-question">Connect your bluetooth device that will communicate with the droid.</p>
  <div class="connected-status-row">
    <div class="connected-status-dot{connected_dot_class}"></div>
    <div>
      <span style="color:var(--text);font-weight:600;">Currently connected: {connected_name}</span>
      {connected_mac_html}
    </div>
  </div>
  <div class="bt-section">
    <div class="bt-section-header">Nearby devices<button class="btn-scan" onclick="scanComlink()">Scan</button></div>
    <div id="commList"><div class="bt-empty">Search for device.</div></div>
    <div id="pinRow" style="display:none; padding: 14px 20px;">
      <input id="pinInput" placeholder="PIN" autocomplete="off">
      <button class="btn-net-add" onclick="confirmPin()">Confirm PIN</button>
    </div>
  </div>
  <form id="micAdvanceForm" method="POST" action="/setup/save_mic" style="display:none;">
    <input type="hidden" name="path" value="{path}">
    <input type="hidden" name="skipped" value="0">
  </form>
  <p class="wizard-note" id="micConnectNote"></p>
  {mic_action_html}
</div>
<script>
let pendingMac = null;
async function scanComlink() {
  document.getElementById('commList').innerHTML = '<div class="bt-empty">Scanning...</div>';
  try {
    const res = await fetch('/bluetooth_scan');
    const data = await res.json();
    const el = document.getElementById('commList');
    if (!data.devices || !data.devices.length) { el.innerHTML = '<div class="bt-empty">No devices found</div>'; return; }
    el.innerHTML = data.devices.map(d => `
      <div class="bt-device">
        <div class="bt-dot disconnected"></div>
        <div class="bt-info"><div class="bt-name">${d.name}</div><div class="bt-mac">${d.mac}</div></div>
        <div class="bt-actions"><button class="btn-pair" onclick="pairComlink('${d.mac}')">Connect</button></div>
      </div>
    `).join('');
  } catch(e) { document.getElementById('commList').innerHTML = '<div class="bt-empty">Scan failed</div>'; }
}
async function pairComlink(mac, pin) {
  try {
    const res = await fetch('/bluetooth_pair', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac, pin: pin || '', section: 'comm'})
    });
    const data = await res.json();
    if (data.ok) {
      const micRes = await fetch('/bluetooth_connect_mic', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac, section: 'comm'})
      });
      const micData = await micRes.json();
      if (!micData.ok) {
        const note = document.getElementById('micConnectNote');
        note.textContent = "Paired, but couldn't set it as your mic automatically (" +
          (micData.error || 'unknown error') + '). Continuing — you can fix this later from Bluetooth Manager if needed.';
        await new Promise(r => setTimeout(r, 2500));
      }
      document.getElementById('micAdvanceForm').submit();
    } else if (data.needs_pin) {
      pendingMac = mac;
      document.getElementById('pinRow').style.display = 'block';
    }
  } catch(e) {}
}
function confirmPin() {
  const pin = document.getElementById('pinInput').value.trim();
  if (pendingMac) pairComlink(pendingMac, pin);
}
function skipMic() {
  document.querySelector('#micAdvanceForm input[name=skipped]').value = '1';
  document.getElementById('micAdvanceForm').submit();
}
function recheckMic() {
  window.location.reload();
}
</script>
</body>
</html>"""


WIZARD_ACTIVATION_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Activating</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .activation-wrap { padding-top: 12px; }
  .activation-phrase-row { opacity: 0.3; transition: opacity 0.3s; margin-bottom: 16px; }
  .activation-phrase-row.active { opacity: 1; }
  .activation-phrase-row.done .activation-bar-fill { background: var(--success); }
  .activation-phrase-text { font-family: var(--font-mono); font-size: 12px; color: var(--text); margin-bottom: 5px; }
  .activation-bar-track { width: 100%; height: 6px; border-radius: 3px; background: var(--edge); overflow: hidden; }
  .activation-bar-fill { height: 100%; width: 0%; border-radius: 3px; background: var(--blue); }
  .activation-overall-wrap { position: relative; width: 100%; height: 40px; margin-top: 28px; }
  .activation-overall-track { position: absolute; top: 28px; left: 0; right: 0; height: 6px; border-radius: 3px; background: var(--edge); overflow: hidden; z-index: 1; }
  .activation-overall-fill { height: 100%; width: 0%; background: linear-gradient(90deg, var(--blue), var(--success)); }
  .activation-droid-icon { position: absolute; top: -3px; left: 0%; width: 10%; aspect-ratio: 1 / 1; transform: translateX(-50%); transition: left 0.05s linear; z-index: 2; }
  .activation-droid-icon svg { width: 100%; height: 100%; }
  .activation-status { text-align: center; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.04em; color: var(--muted); margin-top: 14px; }
  .activation-wait-note { text-align: center; font-size: 12px; color: var(--muted); margin-top: 18px; line-height: 1.6; display: none; }
  .activation-wait-note.show { display: block; }
  .activation-wait-note a { color: var(--blue); text-decoration: none; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Activating {DROID_NAME}</h1>
  </div>
  <div class="activation-wrap">
    <div id="phraseList"></div>
    <div class="activation-overall-wrap">
      <div class="activation-overall-track"><div class="activation-overall-fill" id="overallFill"></div></div>
      <div class="activation-droid-icon" id="droidIcon">
        <svg viewBox="0 0 600 600">
          <g stroke-linecap="round" fill="none" stroke="var(--gold-text)" stroke-width="22" stroke-linejoin="round">
            <path d="M226.45,361.36 C226.45,361.36 227.08,368.73 227.08,368.73"/>
            <path d="M225.34,387.26 C225.34,387.26 232.78,388.82 232.78,388.82"/>
            <path d="M286.68,400.98 C286.68,400.98 207.47,383.23 207.47,383.23 C207.47,383.23 194.97,437.12 194.97,437.12 C194.97,437.12 274.18,454.86 274.18,454.86 C274.18,454.86 286.68,400.98 286.68,400.98 Z"/>
            <path d="M268.34,477.26 C276.91,489.37 296.22,516.66 306.97,531.84 C308.71,534.30 311.17,537.79 311.89,538.81 C283.88,538.59 216.85,538.06 177.83,537.75 C164.43,537.65 147.24,537.51 143.46,537.49 C143.46,537.49 184.74,478.40 184.74,478.40"/>
            <path d="M196.56,535.86 C197.00,532.06 198.02,527.55 199.45,524.50 C200.85,521.59 204.50,516.64 206.78,514.54 C209.09,512.45 214.43,509.22 217.36,508.15 C220.30,507.10 226.47,506.16 229.59,506.29 C232.70,506.45 238.76,507.94 241.59,509.25 C244.41,510.59 249.43,514.28 251.55,516.58 C253.58,518.82 256.94,524.32 257.95,527.16 C259.12,530.58 259.58,533.93 259.74,536.28"/>
            <path d="M311.11,539.14 C311.11,539.14 305.09,568.66 305.09,568.66 C276.45,568.57 210.11,568.35 172.41,568.23 C165.89,568.20 154.16,568.17 148.94,568.15 C148.94,568.15 143.37,537.95 143.37,537.95 C143.37,537.95 143.37,537.95 143.37,537.95"/>
            <path d="M455.48,524.19 C455.48,524.19 450.88,551.92 450.88,551.92 C450.88,551.92 345.86,551.96 345.86,551.96 C345.86,551.96 342.16,523.75 342.16,523.75 C342.16,523.75 342.16,523.75 342.16,523.75"/>
            <path d="M427.50,486.18 C427.50,486.18 368.94,486.32 368.94,486.32 C368.94,486.32 342.87,523.69 342.87,523.69 C342.87,523.69 455.82,524.05 455.82,524.05 C455.82,524.05 427.50,486.18 427.50,486.18 Z"/>
            <path d="M423.60,485.60 C423.60,485.60 406.41,465.51 406.41,465.51 C406.41,465.51 408.20,440.86 408.20,440.86 C408.20,440.86 354.97,462.01 354.97,462.01 C354.97,462.01 379.80,486.12 379.80,486.12"/>
            <path d="M225.02,357.92 C214.51,331.85 189.15,268.93 174.30,232.08 C169.04,219.05 160.03,196.73 156.27,187.45 C155.86,186.30 150.06,171.11 148.58,162.79 C147.15,154.60 146.42,140.77 147.08,132.95 C147.72,125.63 151.03,110.74 153.30,104.40 C155.60,98.06 162.58,84.41 166.78,78.40 C171.38,71.86 180.46,61.89 186.54,56.70 C192.13,51.96 205.07,43.73 211.16,40.85 C217.27,37.99 231.86,33.28 239.07,32.00 C246.95,30.63 260.42,30.00 268.40,30.63 C275.70,31.23 290.66,34.55 297.01,36.83 C303.35,39.12 316.89,46.06 322.96,50.22 C329.46,54.73 339.22,63.81 345.17,70.78 C351.12,77.87 357.02,88.18 360.17,93.74 C360.17,93.74 364.57,104.59 364.57,104.59 C369.17,116.46 379.64,143.51 385.52,158.69 C385.80,159.42 386.08,160.14 386.36,160.87"/>
            <path d="M367.18,112.16 C376.38,135.52 399.64,194.56 413.70,230.24 C427.46,265.17 449.71,321.62 458.19,343.16 C432.80,353.33 370.18,378.41 332.94,393.32 C318.70,399.03 295.38,408.37 286.30,412.00"/>
            <path d="M235.55,389.49 C235.55,389.49 235.40,389.37 235.40,389.37"/>
            <path d="M194.78,435.77 C194.78,435.77 184.22,478.90 184.22,478.90 C184.22,478.90 267.52,479.25 267.52,479.25 C267.52,479.25 273.17,454.72 273.17,454.72 C273.17,454.72 274.34,453.98 274.34,453.98 C299.42,444.30 361.97,420.16 399.42,405.71 C425.50,395.65 452.68,385.16 453.78,384.73 C454.36,378.29 455.78,362.73 456.60,353.61 C456.92,350.35 457.45,344.39 457.97,343.42"/>
            <path d="M341.10,427.64 C341.10,427.64 355.56,462.41 355.56,462.41"/>
            <path d="M315.75,45.28 C289.28,55.37 225.40,79.71 187.98,93.97 C174.75,99.02 156.58,105.94 151.65,107.82"/>
            <path d="M407.31,441.52 C407.31,441.52 394.51,407.26 394.51,407.26"/>
            <path d="M333.39,226.44 C333.39,226.44 315.80,297.47 315.80,297.47 C302.74,294.17 270.36,286.00 251.03,281.12 C243.46,279.20 230.41,275.91 224.93,274.52 C228.45,260.57 236.39,229.11 240.81,211.61 C241.52,208.77 242.54,204.76 242.83,203.58"/>
            <path d="M243.91,201.75 C244.75,199.81 249.53,190.73 254.05,186.15 C256.09,184.11 261.03,180.21 263.65,178.67 C266.60,176.97 271.62,174.84 274.88,173.90 C277.82,173.08 284.20,172.22 286.93,172.18 C289.66,172.17 296.06,172.91 299.01,173.68 C302.29,174.56 307.35,176.61 310.32,178.25 C312.98,179.75 318.10,183.65 320.05,185.56 C321.99,187.47 325.99,192.53 327.53,195.16 C329.20,198.05 331.44,203.29 332.31,206.33 C334.05,212.59 333.99,221.08 333.58,224.38"/>
            <path d="M291.83,202.81 C295.09,203.78 300.42,208.03 302.10,211.00 C303.72,213.99 304.49,220.77 303.58,224.05 C302.61,227.32 298.36,232.65 295.39,234.32 C292.40,235.95 285.62,236.71 282.34,235.80 C279.07,234.83 273.74,230.58 272.07,227.62 C270.44,224.62 269.68,217.85 270.59,214.56 C271.56,211.30 275.81,205.97 278.77,204.29 C281.77,202.67 288.54,201.90 291.83,202.81 Z"/>
            <path d="M274.91,398.11 C274.91,398.11 294.44,309.34 294.44,309.34 C294.44,309.34 240.53,296.73 240.53,296.73 C240.53,296.73 220.79,387.23 220.79,387.23 C220.79,387.23 274.91,398.11 274.91,398.11 Z"/>
            <path d="M316.15,296.89 C316.15,296.89 225.06,274.89 225.06,274.89 C225.06,274.89 240.66,297.85 240.66,297.85 C240.66,297.85 294.03,310.01 294.03,310.01 C294.03,310.01 316.15,296.89 316.15,296.89 Z"/>
            <path d="M328.47,141.34 C328.47,141.34 295.31,154.69 295.31,154.69 C295.31,154.69 277.52,111.70 277.52,111.70 C277.52,111.70 310.68,98.35 310.68,98.35 C310.68,98.35 328.47,141.34 328.47,141.34 Z"/>
            <path d="M261.62,167.38 C261.62,167.38 200.55,192.17 200.55,192.17 C200.55,192.17 183.19,150.09 183.19,150.09 C183.19,150.09 244.27,125.30 244.27,125.30 C244.27,125.30 261.62,167.38 261.62,167.38 Z"/>
            <path d="M362.31,96.90 C362.31,96.90 319.32,113.80 319.32,113.80"/>
            <path d="M282.15,128.41 C282.15,128.41 252.55,140.04 252.55,140.04"/>
            <path d="M187.40,165.65 C187.40,165.65 152.26,179.46 152.26,179.46"/>
            <path d="M372.20,125.89 C347.80,135.27 285.84,159.08 248.28,173.51 C216.86,185.59 175.00,201.68 164.55,205.69"/>
            <path d="M294.61,308.81 C294.61,308.81 312.43,353.19 312.43,353.19 C312.43,353.19 364.18,333.01 364.18,333.01 C364.18,333.01 330.99,245.87 330.99,245.87"/>
            <path d="M359.54,320.02 C363.12,316.46 367.34,313.05 370.87,311.34 C374.31,309.70 381.06,307.90 384.65,307.65 C388.25,307.44 395.39,308.38 398.80,309.52 C402.21,310.70 408.44,314.31 411.16,316.67 C413.85,319.06 418.23,324.78 419.84,328.00 C421.40,331.22 423.24,338.07 423.52,341.79 C423.77,345.66 422.98,350.94 421.84,355.26"/>
            <path d="M369.60,376.33 C369.58,376.31 365.15,373.53 362.88,371.64 C360.34,369.50 355.76,363.41 354.20,360.31 C350.96,353.67 350.42,344.72 350.63,341.34"/>
          </g>
        </svg>
      </div>
    </div>
    <div class="activation-status" id="activationStatus">Activating {DROID_NAME}</div>
    <p class="activation-wait-note" id="waitNote">Still trying to reconnect — make sure {DROID_NAME} is powered on and nearby.<br><a href="#" onclick="proceedAnyway();return false">Continue anyway</a></p>
  </div>
</div>
<script>
const phrases = [
  "Defining identity profile",
  "Writing personality matrix",
  "Connecting logic core",
  "Establishing communication device",
  "Onboarding KYBER protocols"
];
const STEP_MS = 8500;

document.getElementById('phraseList').innerHTML = phrases.map(function(p, i) {
  return '<div class="activation-phrase-row" id="row' + i + '">' +
    '<div class="activation-phrase-text">' + p + '...</div>' +
    '<div class="activation-bar-track"><div class="activation-bar-fill" id="fill' + i + '"></div></div>' +
  '</div>';
}).join('');

let animationDone = false;
let backendReady = false;

// A randomized easing curve per phrase so the fill doesn't look like a
// mechanical, perfectly linear sweep -- a handful of random control points
// between (0,0) and (1,1), monotonic, so it still always completes exactly
// on time even though some stretches stall and others catch up in a burst.
function makeStutterCurve() {
  const n = 4 + Math.floor(Math.random() * 3);
  const points = [[0, 0]];
  for (let i = 1; i < n; i++) {
    points.push([i / n, Math.random()]);
  }
  points.push([1, 1]);
  for (let i = 1; i < points.length; i++) {
    if (points[i][1] < points[i - 1][1]) {
      points[i][1] = Math.min(1, points[i - 1][1] + Math.random() * 0.15);
    }
  }
  const maxV = points[points.length - 1][1] || 1;
  return points.map(function(p) { return [p[0], p[1] / maxV]; });
}
function curveValue(curve, t) {
  for (let i = 1; i < curve.length; i++) {
    if (t <= curve[i][0]) {
      const t0 = curve[i - 1][0], v0 = curve[i - 1][1];
      const t1 = curve[i][0], v1 = curve[i][1];
      const local = t1 > t0 ? (t - t0) / (t1 - t0) : 1;
      return v0 + (v1 - v0) * local;
    }
  }
  return 1;
}

function runPhrases() {
  phrases.forEach(function(p, i) {
    const curve = makeStutterCurve();
    setTimeout(function() {
      document.getElementById('row' + i).classList.add('active');
      const start = performance.now();
      function tick(ts) {
        const t = Math.min(1, (ts - start) / (STEP_MS - 150));
        const pct = curveValue(curve, t) * 100;
        document.getElementById('fill' + i).style.width = pct + '%';
        const overallPct = ((i + t) / phrases.length) * 100;
        document.getElementById('overallFill').style.width = overallPct + '%';
        document.getElementById('droidIcon').style.left = overallPct + '%';
        if (t < 1) { requestAnimationFrame(tick); }
        else { document.getElementById('row' + i).classList.add('done'); }
      }
      requestAnimationFrame(tick);
    }, i * STEP_MS);
  });
  setTimeout(function() {
    animationDone = true;
    checkDone();
  }, phrases.length * STEP_MS);
}

let pollCount = 0;
async function pollReady() {
  try {
    const res = await fetch('/wizard_droid_status');
    const data = await res.json();
    backendReady = !!data.fully_ready;
  } catch (e) {}
  pollCount++;
  if (pollCount === 13 && !backendReady) {
    // ~90s of holding with nothing happening -- offer a manual out rather
    // than leaving someone staring at a stuck-looking screen.
    document.getElementById('waitNote').classList.add('show');
  }
  checkDone();
  if (!backendReady) setTimeout(pollReady, 7000);
}

function checkDone() {
  if (animationDone && backendReady) {
    finish();
  } else if (animationDone) {
    document.getElementById('activationStatus').textContent = 'Initiating optimizations and completing final system checks.';
  }
}

async function finish() {
  // Confirmed once, verified -- not fire-and-forget. If this silently
  // failed and we navigated anyway, current_setup_stage() would still see
  // ACTIVATION_CONFIRMED unset and just render this exact page again,
  // which looks identical to "the whole thing restarting" from here.
  let confirmed = false;
  for (let attempt = 0; attempt < 4 && !confirmed; attempt++) {
    try {
      const res = await fetch('/setup/confirm_activation', {method: 'POST'});
      const data = await res.json();
      confirmed = !!data.ok;
    } catch (e) {
      confirmed = false;
    }
    if (!confirmed) await new Promise(function(r) { setTimeout(r, 600); });
  }
  window.location.href = '/';
}

function proceedAnyway() {
  finish();
}

runPhrases();
pollReady();
</script>
</body>
</html>"""


WIZARD_READY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KYBER Setup — Ready</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .check-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; font-size: 13px; color: var(--text); }
  .check-row i { font-size: 16px; color: var(--success); }
  .check-row.pending i { color: var(--muted); }
  .mic-level-track { width: 100%; height: 6px; border-radius: 3px; background: var(--edge); overflow: hidden; margin: -2px 0 10px 26px; width: calc(100% - 26px); }
  .mic-level-fill { height: 100%; width: 0%; border-radius: 3px; background: var(--blue); transition: width 0.1s linear; }
  .say-hi-box { text-align: center; padding: 8px 0 4px; }
  .ready-exit-row { display: flex; gap: 8px; margin-top: 18px; }
  .ready-exit-row a { flex: 1; text-align: center; font-size: 12px; color: var(--muted); text-decoration: none; }
  .ready-exit-row a:hover { color: var(--warning); }
  .confirm-overlay { display: none; position: fixed; inset: 0; background: rgba(5,18,34,0.88); z-index: 200; align-items: center; justify-content: center; }
  .confirm-overlay.show { display: flex; }
  .confirm-box { border: 1px solid transparent; border-radius: 16px; padding: 28px 24px; max-width: 340px; width: 90%; text-align: center;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .confirm-box i { font-size: 28px; color: var(--error); margin-bottom: 10px; }
  .confirm-box h2 { font-size: 15px; color: var(--text); margin-bottom: 10px; }
  .confirm-box p { font-size: 13px; color: var(--muted); line-height: 1.6; margin-bottom: 20px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Almost there</h1>
  </div>
  {breadcrumbs}
  <div class="panel">
    <div class="panel-header">Status checks</div>
    <div class="panel-body">
      <div class="check-row pending" id="checkDroid"><i class="ti ti-circle-dashed"></i>Checking droid connection...</div>
      <div class="check-row"><i class="ti ti-check"></i>{mic_status_text}</div>
      <div class="mic-level-track"><div class="mic-level-fill" id="micLevelFill"></div></div>
      <div class="say-hi-box">
        <p class="wizard-question">Say hi to your droid. When you hear it respond, confirm below.</p>
      </div>
    </div>
  </div>
  <form id="finishForm" method="POST" action="/setup/finish">
    <div class="btn-row">
      <button type="submit" class="primary" id="finishBtn">Confirm Setup &amp; Power Cycle Droid</button>
    </div>
  </form>
  <div class="ready-exit-row">
    <a href="/?edit=welcome&path=yes">Start over</a>
    <a href="#" onclick="document.getElementById('fullResetOverlay').classList.add('show');return false">Full reset</a>
  </div>
</div>

<div class="confirm-overlay" id="fullResetOverlay">
  <div class="confirm-box">
    <i class="ti ti-alert-triangle"></i>
    <h2>Are you sure? This will remove all progress.</h2>
    <p>Clears model, name, personality, communication device, keys, and droid pairing — sends you back to the very beginning. Your API keys are kept.</p>
    <div class="btn-row">
      <button type="button" class="secondary" onclick="document.getElementById('fullResetOverlay').classList.remove('show')">Cancel</button>
      <button type="button" class="primary" onclick="document.getElementById('fullResetForm').submit()">Full reset</button>
    </div>
  </div>
</div>
<form id="fullResetForm" method="POST" action="/setup/start_over" style="display:none"></form>

<div class="overlay" id="restartOverlay">
  <div class="overlay-box">
    <h2 id="overlayTitle">Restart Your Droid</h2>
    <p id="overlayMsg">Please restart your droid.</p>
    <div class="countdown-ring" id="countdownRing">
      <svg viewBox="0 0 80 80" width="90" height="90">
        <circle class="track" cx="40" cy="40" r="36"/>
        <circle class="fill" id="ring" cx="40" cy="40" r="36"/>
      </svg>
      <div class="countdown-num" id="countNum">30</div>
    </div>
    <div class="overlay-status" id="overlayStatus">Attempting to establish reconnection...</div>
    <button class="btn-overlay-ok" id="overlayBtn" onclick="overlayAction()">OK — I've Power Cycled the Droid</button>
  </div>
</div>

<script>
function checkFinish() {
  const el = document.getElementById('checkDroid');
  fetch('/wizard_droid_status').then(r => r.json()).then(data => {
    if (data.connected) {
      el.classList.remove('pending');
      el.innerHTML = '<i class="ti ti-check"></i>Droid connected';
    } else {
      el.innerHTML = '<i class="ti ti-circle-dashed"></i>Waiting for droid to connect...';
      setTimeout(checkFinish, 3000);
    }
  }).catch(() => setTimeout(checkFinish, 3000));
}
checkFinish();

// Live mic-level bar -- polls faster than the connection check above since
// it's meant to visibly react to someone talking, not just report a static
// connected/not-connected state. Baseline/ceiling are a starting estimate
// (matching kyber_core.py's own VAD_RMS_FLOOR_MIN as the "silence" point) --
// may need real-hardware tuning once this is actually watched live.
const MIC_LEVEL_BASELINE = 800;
const MIC_LEVEL_CEILING  = 3000;
function pollMicLevel() {
  fetch('/wizard_droid_status').then(r => r.json()).then(data => {
    const rms = data.mic_rms || 0;
    const pct = Math.max(0, Math.min(100, ((rms - MIC_LEVEL_BASELINE) / (MIC_LEVEL_CEILING - MIC_LEVEL_BASELINE)) * 100));
    document.getElementById('micLevelFill').style.width = pct + '%';
  }).catch(() => {});
  setTimeout(pollMicLevel, 250);
}
pollMicLevel();

// ── Restart overlay -- same reconnect-poll pattern as the Mainframe's,
// with one addition: once reconnect is confirmed, mark onboarding complete
// before reloading, so the reload actually lands on the Mainframe instead
// of looping back to this same Ready page.
const SETTINGS_URL = 'http://' + location.hostname + ':5001';
const CIRCUMFERENCE = 251;
const TOTAL = 30;
let elapsed = 0;
let polling = false;
let tickTimer = null;
let brainWentDown = false;

function overlayAction() {
  if (!polling) {
    document.getElementById('overlayMsg').style.display = 'none';
    document.getElementById('overlayTitle').textContent = 'Reconnecting...';
    document.getElementById('countdownRing').classList.add('show');
    document.getElementById('overlayStatus').classList.add('show');
    document.getElementById('overlayBtn').disabled = true;
    polling = true;
    brainWentDown = false;
    elapsed = 0;
    tickTimer = setTimeout(tick, 1000);
  } else {
    window.location.reload();
  }
}

function tick() {
  elapsed++;
  const remaining = Math.max(0, TOTAL - elapsed);
  const offset = CIRCUMFERENCE * (remaining / TOTAL);
  document.getElementById('ring').style.strokeDashoffset = offset;
  document.getElementById('countNum').textContent = remaining;
  checkBrain();
}

async function checkBrain() {
  try {
    const res = await fetch(SETTINGS_URL + '/brain_ready');
    const data = await res.json();
    if (!data.ready) brainWentDown = true;
    if (data.ready && brainWentDown) {
      if (tickTimer) { clearTimeout(tickTimer); tickTimer = null; }
      document.getElementById('ring').style.strokeDashoffset = 0;
      document.getElementById('ring').style.stroke = 'var(--success)';
      document.getElementById('countNum').textContent = '✓';
      document.getElementById('overlayTitle').textContent = 'Droid Is Back Online!';
      document.getElementById('overlayStatus').textContent = 'Connected successfully. Refreshing...';
      polling = false;
      try { await fetch('/setup/confirm_complete', {method: 'POST'}); } catch(e) {}
      location.reload();
      return;
    }
  } catch(e) {}
  if (elapsed < TOTAL) {
    tickTimer = setTimeout(tick, 1000);
  } else {
    document.getElementById('overlayStatus').textContent = 'Still searching — make sure droid is powered on.';
    document.getElementById('overlayBtn').textContent = 'Keep Waiting';
    document.getElementById('overlayBtn').disabled = false;
    document.getElementById('overlayBtn').onclick = () => {
      document.getElementById('overlayBtn').disabled = true;
      elapsed = 0;
      document.getElementById('ring').style.strokeDashoffset = CIRCUMFERENCE;
      tickTimer = setTimeout(tick, 1000);
    };
  }
}

{restart_overlay_js}
</script>
</body>
</html>"""





MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Mainframe">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#060a0f">
<title>Mainframe</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  /* Status bar */
  .status-bar { display: flex; align-items: center; gap: 10px; border-radius: 999px; padding: 12px 16px; margin-bottom: 16px; font-size: 13px;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .status-dot.active { background: var(--success); box-shadow: 0 0 6px var(--success); animation: blink 2s infinite; }
  .status-dot.inactive { background: var(--muted); }
  .status-dot.unknown { background: var(--warning); }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  /* Tab nav */
  /* 2x2 grid */
  .main-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  .main-grid > .full-width { grid-column: 1 / -1; }
  @media (max-width: 480px) {
    .main-grid { grid-template-columns: 1fr; }
  }
  /* Droid Identity dropdowns */
  .identity-row { display: flex; gap: 8px; align-items: center; }
  .identity-select-wrap { position: relative; flex: 1; }
  .identity-select { -webkit-appearance: none; -moz-appearance: none; appearance: none; width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 12px 32px 12px 14px; color: var(--text); font-family: var(--font-body); font-size: 14px; line-height: 1.4; outline: none; cursor: pointer; transition: border-color 0.2s; }
  .identity-select:focus { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(0,168,255,0.15); }
  .identity-chevron { position: absolute; right: 12px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 10px; pointer-events: none; }
  .btn-identity-edit {
    border: 1px solid transparent; border-radius: 10px; padding: 12px 16px;
    font-family: var(--font-head); font-size: 13px; font-weight: 700; cursor: pointer; white-space: nowrap; color: #fff;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-identity-edit:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  /* Droid chassis selector */
  .droid-type-row { display: flex; gap: 7px; flex-wrap: wrap; }
  .droid-type-option { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; flex: 1; min-width: 56px; padding: 10px 6px; border-radius: 14px; cursor: pointer;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .droid-type-option.active-droid-type {
    background-image: linear-gradient(135deg, var(--gold-light) 0%, var(--gold-dark) 100%), linear-gradient(135deg, var(--gold-light), var(--gold-dark));
    box-shadow: 0 2px 4px var(--shadow);
  }
  .droid-type-option input[type="radio"] { display: none; }
  .droid-type-icon { display: block; width: 32px; height: 32px; }
  .droid-type-icon svg { width: 100%; height: 100%; }
  /* Network panel */
  .net-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid rgba(143,174,193,0.18); }
  .net-row:last-child { border-bottom: none; }
  .net-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .net-dot.on { background: var(--success); box-shadow: 0 0 4px var(--success); }
  .net-dot.off { background: var(--muted); }
  .net-ssid { flex: 1; font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .net-ssid.connected { color: var(--success); }
  .btn-net-connect {
    font-size: 12px; font-family: var(--font-head); font-weight: 600; color: var(--muted); border-radius: 8px; padding: 6px 12px; cursor: pointer; flex-shrink: 0;
    border: 1px solid var(--edge); background: transparent;
  }
  .btn-net-connect:hover { background: rgba(255,255,255,0.05); color: var(--text); }
  .btn-net-forget {
    font-size: 12px; font-family: var(--font-head); font-weight: 600; color: #fff; border-radius: 8px; padding: 6px 12px; cursor: pointer; flex-shrink: 0;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-net-forget:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .btn-net-add {
    width: 100%; margin-top: 10px; border-radius: 10px; padding: 11px;
    font-family: var(--font-head); font-size: 13px; font-weight: 600; cursor: pointer; color: #fff;
    border: 1px solid transparent;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-net-add:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  /* Add network modal */
  .net-modal { display: none; position: fixed; inset: 0; background: rgba(5,18,34,0.9); z-index: 100; align-items: center; justify-content: center; }
  .net-modal.show { display: flex; }
  .net-modal-box { border: 1px solid transparent; border-radius: 16px; padding: 24px; width: 90%; max-width: 320px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .net-modal-title { font-family: var(--font-head); font-size: 15px; font-weight: 700; color: var(--gold-text); margin-bottom: 16px; }
  .net-modal-note { font-size: 12px; color: var(--warning); background: rgba(255,200,87,0.08); border: 1px solid rgba(255,200,87,0.25); border-radius: 8px; padding: 8px 10px; margin-bottom: 14px; line-height: 1.6; }
  .net-modal input { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 12px 14px; color: var(--text); font-family: var(--font-mono); font-size: 14px; outline: none; margin-bottom: 10px; box-sizing: border-box; }
  .net-modal input:focus { border-color: var(--blue); }
  .net-modal-btns { display: flex; gap: 8px; margin-top: 4px; }
  .net-modal-btns button { flex: 1; padding: 11px; border-radius: 10px; font-family: var(--font-head); font-size: 13px; font-weight: 700; cursor: pointer; border: 1px solid transparent; }
  .net-modal-save { color: #fff;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .net-modal-save:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text); }
  .net-modal-cancel { background: transparent; color: var(--muted); border-color: var(--edge); }
  .net-modal-cancel:hover { border-color: var(--gold-light); color: var(--gold-text); }
  .net-scan-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .net-scan-header span { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .net-scan-list { background: rgba(255,255,255,0.03); border: 1px solid var(--edge); border-radius: 10px; overflow: hidden; margin-bottom: 14px; max-height: 160px; overflow-y: auto; }
  .net-scan-row { display: flex; align-items: center; gap: 8px; padding: 10px 12px; cursor: pointer; border-bottom: 1px solid rgba(143,174,193,0.15); font-size: 13px; }
  .net-scan-row:last-child { border-bottom: none; }
  .net-scan-row:hover { background: rgba(255,255,255,0.05); }
  .net-scan-row.selected { background: rgba(29,66,104,0.4); }
  .net-scan-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
  .net-scan-ssid { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .net-scan-security { font-size: 10px; color: var(--muted); flex-shrink: 0; }
  .net-scan-empty { padding: 14px 12px; font-size: 12px; color: var(--muted); text-align: center; }
  .net-rescan-btn { display: block; width: 100%; box-sizing: border-box; text-align: center; font-family: var(--font-head); font-size: 12px; font-weight: 700; color: var(--muted); background: transparent; border: 1px solid var(--edge); border-radius: 10px; padding: 9px; cursor: pointer; margin: 0 0 14px; }
  .net-rescan-btn:hover { border-color: var(--gold-light); color: var(--gold-text); }
  .confirm-overlay { display: none; position: fixed; inset: 0; background: rgba(5,18,34,0.9); z-index: 200; align-items: center; justify-content: center; }
  .confirm-overlay.show { display: flex; }
  .confirm-box { border: 1px solid transparent; border-radius: 16px; padding: 24px; width: 90%; max-width: 320px; text-align: center;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .confirm-box p { font-size: 13px; color: var(--text); line-height: 1.6; margin-bottom: 20px; }
  .kb-warning { display: flex; align-items: flex-start; gap: 6px; font-size: 14px; color: var(--error); margin-bottom: 14px; line-height: 1.5; }
  .kb-warning svg { flex-shrink: 0; margin-top: 2px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>K.Y.B.E.R.</h1>
    <p class="subtitle">Kinetic Yammering and Behavioral Engine Routines</p>
    <p class="secondary-header">Droid Settings</p>
  </div>

  <!-- Status bars -->
  <div class="status-bar">
    <div class="status-dot {status_class}"></div>
    <span>Droid service: {status_text}</span>
  </div>
  <div class="status-bar" id="hotelStatusBar" style="display:none">
    <div class="status-dot active" style="background:var(--warning);box-shadow:0 0 6px var(--warning)"></div>
    <span>Hotel Sentry: Active — <span id="hotelCountdown"></span> remaining</span>
  </div>
  <div class="status-bar" id="networkStatusBar">
    <div class="status-dot" id="networkStatusDot"></div>
    <span id="networkStatusText">Network: checking...</span>
    <span id="networkStatusIp" style="display:none;margin-left:6px;font-size:11px;color:var(--muted);"></span>
    <button type="button" id="networkReconnectBtn" onclick="reconnectNetwork()"
      style="display:none;margin-left:auto;background:transparent;border:1px solid var(--blue);color:var(--blue);
      border-radius:8px;padding:4px 12px;font-family:var(--font-head);font-size:11px;font-weight:600;cursor:pointer;">
      Reconnect
    </button>
  </div>

  <!-- Tab nav -->
  {nav}

  <!-- Hotel deactivate (only during sentry) -->
  <div id="hotelDeactivateBtn" style="display:none;margin-bottom:16px">
    <button type="button" onclick="deactivateSentry()" style="width:100%;background:transparent;color:var(--warning);border:1px solid var(--warning);border-radius:10px;padding:12px 20px;font-family:var(--font-head);font-size:13px;font-weight:600;cursor:pointer;">&#9632; Deactivate Hotel Sentry</button>
  </div>

  {alert_html}

  <!-- 2x2 grid -->
  <form method="POST" action="/save" autocomplete="off">

  <div class="main-grid">

  <!-- Droid Model -->
  <div class="panel full-width">
    <div class="panel-header">Droid Model</div>
    <div class="panel-body">
      <div class="field">
        <p class="field-note" style="margin:0 0 4px">Select your droid model.</p>
        <div class="droid-type-row">
          {droid_type_options}
        </div>
      </div>
    </div>
  </div>

  <!-- Droid Identity (Designation + Personality + Sound Profile) -->
  <div class="panel">
    <div class="panel-header">Droid Identity</div>
    <div class="panel-body">
      <div class="field">
        <label class="field-label">Designation</label>
        <div class="input-wrap">
          <input type="text" name="DROID_NAME" id="droidNameInput" value="{DROID_NAME_RAW}" placeholder="##-###" maxlength="10" autocomplete="off">
        </div>
        <p class="field-note" id="droidNameNote"></p>
      </div>
      <div class="field">
        <label class="field-label">Personality Profile</label>
        <div class="identity-row">
          <div class="identity-select-wrap">
            <select class="identity-select" id="personalitySelect" name="ACTIVE_PERSONALITY">
              {personality_options}
            </select>
            <span class="identity-chevron">&#9662;</span>
          </div>
          <button type="button" class="btn-identity-edit" onclick="openPersonalityEditor(document.getElementById('personalitySelect').value)">Edit</button>
        </div>
      </div>
      <div class="field">
        <label class="field-label">Sound Profile</label>
        <div class="identity-row">
          <div class="identity-select-wrap">
            <select class="identity-select" id="soundProfileSelect" name="ACTIVE_SOUND_PROFILE">
              {sound_profile_options}
            </select>
            <span class="identity-chevron">&#9662;</span>
          </div>
          <button type="button" class="btn-identity-edit" onclick="openMapper(document.getElementById('soundProfileSelect').value)">Edit</button>
        </div>
      </div>
      <p class="field-note">Personality and Sound Profile are independent — mix and match freely.</p>
    </div>
  </div>

  <!-- Network -->
  <div class="panel">
    <div class="panel-header">Network</div>
    <div class="panel-body">
      <div id="netList"><div style="color:var(--muted);font-size:12px">Loading...</div></div>
      <button type="button" class="btn-net-add" onclick="openAddNetwork()">+ Add Network</button>
    </div>
  </div>

  <!-- Logic Core Connections -->
  <div class="panel full-width">
    <div class="panel-header">Logic Core Connections</div>
    <div class="panel-body">
      {logic_core_panel}
    </div>
  </div>

  </div>

  <div class="kb-warning">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
    <span>Changes apply only after Save &amp; Restart — then a full power cycle of your droid.</span>
  </div>

  <div class="btn-row" style="margin-top:8px">
    <button type="submit" name="action" value="save_restart" class="primary">Save &amp; Restart</button>
  </div>
  </form>

  <div class="footer"><p>K.Y.B.E.R. — Kinetic Yammering and Behavioral Engine Routines — Open Source Project</p></div>
</div>

<!-- Add Network Modal -->
<div class="net-modal" id="netModal">
  <div class="net-modal-box">
    <div class="net-modal-title">Add Network</div>
    <div class="net-modal-note"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>Switching networks may change {DROID_NAME}'s IP. Try <span id="netModalHost"></span> to reconnect.</div>
    <div class="net-scan-header"><span>Nearby networks</span></div>
    <div class="net-scan-list" id="netScanList"><div class="net-scan-empty">Loading...</div></div>
    <button type="button" class="net-rescan-btn" onclick="document.getElementById('rescanConfirmOverlay').classList.add('show')">Rescan for networks</button>
    <input type="text" id="netSsid" placeholder="Network name (SSID)" autocomplete="off">
    <input type="password" id="netPass" placeholder="Password" autocomplete="off">
    <div class="net-modal-btns">
      <button type="button" class="net-modal-save" onclick="saveNetwork()">Save &amp; Connect</button>
      <button type="button" class="net-modal-cancel" onclick="closeAddNetwork()">Cancel</button>
    </div>
  </div>
</div>

<div class="confirm-overlay" id="rescanConfirmOverlay">
  <div class="confirm-box">
    <p>Rescan for available networks? You will need to use KYBER Connect in order to select a network — Connection to the droid will be disrupted until a network is chosen.</p>
    <div class="net-modal-btns">
      <button type="button" class="net-modal-save" onclick="confirmRescan()">Continue</button>
      <button type="button" class="net-modal-cancel" onclick="document.getElementById('rescanConfirmOverlay').classList.remove('show')">Cancel</button>
    </div>
  </div>
</div>

<!-- Restart overlay -->
<div class="overlay" id="restartOverlay">
  <div class="overlay-box">
    <h2 id="overlayTitle">Restart Your Droid</h2>
    <p id="overlayMsg">Please restart your droid.</p>
    <div class="countdown-ring" id="countdownRing">
      <svg viewBox="0 0 80 80" width="90" height="90">
        <circle class="track" cx="40" cy="40" r="36"/>
        <circle class="fill" id="ring" cx="40" cy="40" r="36"/>
      </svg>
      <div class="countdown-num" id="countNum">30</div>
    </div>
    <div class="overlay-status" id="overlayStatus">Attempting to establish reconnection...</div>
    <button class="btn-overlay-ok" id="overlayBtn" onclick="overlayAction()">OK — I've Power Cycled the Droid</button>
  </div>
</div>

<script>
function openPersonalityEditor(slot) {
  window.location.href = '/edit_personality?slot=' + encodeURIComponent(slot);
}

function openMapper(slot) {
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = '/open_mapper';
  const input = document.createElement('input');
  input.type = 'hidden';
  input.name = 'mapper_slot';
  input.value = slot;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
}

function formatCountdown(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

async function pollHotelStatus() {
  try {
    const res = await fetch('/hotel_status');
    const data = await res.json();
    const bar = document.getElementById('hotelStatusBar');
    const cd = document.getElementById('hotelCountdown');
    const deactBtn = document.getElementById('hotelDeactivateBtn');
    if (data.active) {
      bar.style.display = 'flex';
      cd.textContent = formatCountdown(data.remaining_seconds);
      deactBtn.style.display = 'block';
    } else {
      bar.style.display = 'none';
      deactBtn.style.display = 'none';
    }
  } catch(e) {}
}

async function deactivateSentry() {
  await fetch('/hotel_toggle', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({active: false})
  });
  await new Promise(r => setTimeout(r, 1000));
  pollHotelStatus();
}

// "ap" / "associating" mean KYBER doesn't have a working network connection
// yet — warning color, link to the provisioning page. "client_checking" /
// "bridging" mean it's actively working on it. "client_online" is the only
// fully-good state.
async function pollNetworkStatus() {
  try {
    const res = await fetch('/network_status');
    const data = await res.json();
    const dot  = document.getElementById('networkStatusDot');
    const text = document.getElementById('networkStatusText');
    const ipEl = document.getElementById('networkStatusIp');
    const btn  = document.getElementById('networkReconnectBtn');
    const labels = {
      ap: 'Not connected — join "' + (data.ap_ssid || 'KYBER Connect') + '" from a phone to set up wifi',
      associating: 'Joining network...',
      client_checking: 'Verifying connection...',
      bridging: 'Network needs sign-in — try your phone\u2019s hotspot instead',
      client_online: 'Network: connected',
      unreachable: 'Network status unavailable (kyber_netgw.service not running)',
    };
    text.textContent = labels[data.state] || ('Network: ' + data.state);
    dot.className = 'status-dot' + (data.state === 'client_online' ? ' active' : '');
    dot.style.background = data.state === 'client_online' ? '' : 'var(--warning)';
    dot.style.boxShadow = data.state === 'client_online' ? '' : '0 0 6px var(--warning)';
    btn.style.display = (data.state === 'client_online') ? 'none' : 'inline-block';
    if (data.state === 'client_online' && data.current_ip) {
      ipEl.textContent = '(' + data.current_ip + ')';
      ipEl.style.display = 'inline';
    } else {
      ipEl.style.display = 'none';
    }
  } catch(e) {}
}

async function reconnectNetwork() {
  const btn = document.getElementById('networkReconnectBtn');
  btn.disabled = true;
  await fetch('/network_reconnect', { method: 'POST' });
  await new Promise(r => setTimeout(r, 1000));
  btn.disabled = false;
  pollNetworkStatus();
}

setTimeout(pollNetworkStatus, 500);
setInterval(pollNetworkStatus, 10000);

// ── Network management ──────────────────────────────────────────────────────

async function loadNetworks() {
  try {
    const res = await fetch('/network_list');
    const data = await res.json();
    const el = document.getElementById('netList');
    if (!data.networks || !data.networks.length) {
      el.innerHTML = '<div style="color:var(--muted);font-size:12px">No saved networks</div>';
      return;
    }
    el.innerHTML = data.networks.map(n => `
      <div class="net-row">
        <div class="net-dot ${n.connected ? 'on' : 'off'}"></div>
        <div class="net-ssid ${n.connected ? 'connected' : ''}">${n.ssid}</div>
        ${n.connected ? '' : `<button type="button" class="btn-net-connect" onclick="connectNetwork('${n.ssid.replace(/'/g, "\\'")}')">Connect</button>`}
        <button type="button" class="btn-net-forget" onclick="forgetNetwork('${n.ssid.replace(/'/g, "\\'")}')">Forget</button>
      </div>`).join('');
  } catch(e) {
    document.getElementById('netList').innerHTML = '<div style="color:var(--muted);font-size:12px">Error loading</div>';
  }
}

async function connectNetwork(ssid) {
  try {
    await fetch('/network_add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid, assume_known: true})
    });
    // Fire-and-forget on the backend (the actual join happens in the
    // background and can take a few seconds) -- give it a moment before
    // refreshing so the list has a real chance of reflecting the change
    // rather than reloading mid-transition.
    setTimeout(loadNetworks, 3000);
  } catch(e) {}
}

function openAddNetwork() {
  document.getElementById('netSsid').value = '';
  document.getElementById('netPass').value = '';
  document.getElementById('netModal').classList.add('show');
  loadScanResults();
}

async function loadScanResults() {
  const el = document.getElementById('netScanList');
  el.innerHTML = '<div class="net-scan-empty">Loading...</div>';
  try {
    const res = await fetch('/network_scan_cache');
    const data = await res.json();
    if (!data.networks || !data.networks.length) {
      el.innerHTML = '<div class="net-scan-empty">No networks found — try Rescan below</div>';
      return;
    }
    el.innerHTML = data.networks.map(n => `
      <div class="net-scan-row" onclick="selectScanNetwork('${n.ssid.replace(/'/g, "\\'")}', this)">
        <div class="net-scan-dot"></div>
        <div class="net-scan-ssid">${n.ssid}</div>
        <div class="net-scan-security">${n.open ? 'open' : 'secured'}</div>
      </div>`).join('');
  } catch(e) {
    el.innerHTML = '<div class="net-scan-empty">Could not load nearby networks</div>';
  }
}

function selectScanNetwork(ssid, rowEl) {
  document.getElementById('netSsid').value = ssid;
  document.querySelectorAll('.net-scan-row').forEach(r => r.classList.remove('selected'));
  rowEl.classList.add('selected');
  document.getElementById('netPass').focus();
}

async function confirmRescan() {
  document.getElementById('rescanConfirmOverlay').classList.remove('show');
  closeAddNetwork();
  try {
    await fetch('/network_rescan', { method: 'POST' });
  } catch(e) {}
}

function closeAddNetwork() {
  document.getElementById('netModal').classList.remove('show');
}

async function saveNetwork() {
  const ssid = document.getElementById('netSsid').value.trim();
  const psk = document.getElementById('netPass').value;
  if (!ssid) return;
  closeAddNetwork();
  try {
    const res = await fetch('/network_add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid, psk})
    });
    await loadNetworks();
  } catch(e) {}
}

async function forgetNetwork(ssid) {
  if (!confirm(`Remove "${ssid}" from saved networks?`)) return;
  try {
    const res = await fetch('/network_forget', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid})
    });
    const data = await res.json();
    if (!data.ok) {
      alert('Could not remove "' + ssid + '": ' + (data.error || 'unknown error'));
    }
    await loadNetworks();
  } catch(e) {
    alert('Could not remove "' + ssid + '" — request failed.');
  }
}

document.getElementById('netModal').addEventListener('click', function(e) {
  if (e.target === this) closeAddNetwork();
});
document.getElementById('netPass').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') saveNetwork();
});

// ── Overlay / reconnect ─────────────────────────────────────────────────────

const SETTINGS_URL = 'http://' + location.hostname + ':5001';
const CIRCUMFERENCE = 251;
const TOTAL = 30;
let elapsed = 0;
let polling = false;
let tickTimer = null;
let brainWentDown = false;

function overlayAction() {
  if (!polling) {
    document.getElementById('overlayMsg').style.display = 'none';
    document.getElementById('overlayTitle').textContent = 'Reconnecting...';
    document.getElementById('countdownRing').classList.add('show');
    document.getElementById('overlayStatus').classList.add('show');
    document.getElementById('overlayBtn').disabled = true;
    polling = true;
    brainWentDown = false;
    elapsed = 0;
    tickTimer = setTimeout(tick, 1000);
  } else {
    window.location.reload();
  }
}

function tick() {
  elapsed++;
  const remaining = Math.max(0, TOTAL - elapsed);
  const offset = CIRCUMFERENCE * (remaining / TOTAL);
  document.getElementById('ring').style.strokeDashoffset = offset;
  document.getElementById('countNum').textContent = remaining;
  checkBrain();
}

async function checkBrain() {
  try {
    const res = await fetch(SETTINGS_URL + '/brain_ready');
    const data = await res.json();
    if (!data.ready) brainWentDown = true;
    if (data.ready && brainWentDown) {
      if (tickTimer) { clearTimeout(tickTimer); tickTimer = null; }
      document.getElementById('ring').style.strokeDashoffset = 0;
      document.getElementById('ring').style.stroke = 'var(--success)';
      document.getElementById('countNum').textContent = '✓';
      document.getElementById('overlayTitle').textContent = 'Droid Is Back Online!';
      document.getElementById('overlayStatus').textContent = 'Connected successfully. Refreshing...';
      polling = false;
      location.reload();
      return;
    }
  } catch(e) {}
  if (elapsed < TOTAL) {
    tickTimer = setTimeout(tick, 1000);
  } else {
    document.getElementById('overlayStatus').textContent = 'Still searching — make sure droid is powered on.';
    document.getElementById('overlayBtn').textContent = 'Keep Waiting';
    document.getElementById('overlayBtn').disabled = false;
    document.getElementById('overlayBtn').onclick = () => {
      document.getElementById('overlayBtn').disabled = true;
      elapsed = 0;
      document.getElementById('ring').style.strokeDashoffset = CIRCUMFERENCE;
      tickTimer = setTimeout(tick, 1000);
    };
  }
}

setTimeout(pollHotelStatus, 2000);
setInterval(pollHotelStatus, 60000);
loadNetworks();

const netModalHostEl = document.getElementById('netModalHost');
if (netModalHostEl) netModalHostEl.textContent = location.hostname + ':5001';

// ── Droid Designation ───────────────────────────────────────────────────────

function sanitizeDroidNameClient(raw) {
  if (!raw) return '';
  raw = raw.trim().slice(0, 10);
  const stripped = raw.replace(/[^a-zA-Z0-9 .-]/g, '');
  const hasAlphanumeric = /[a-zA-Z0-9]/.test(stripped);
  return hasAlphanumeric ? stripped : '';
}

document.getElementById('droidNameInput').addEventListener('input', function(e) {
  const original = e.target.value;
  const cleaned = sanitizeDroidNameClient(original);
  const strippedOnly = original.trim().slice(0, 10).replace(/[^a-zA-Z0-9 .-]/g, '');
  if (original.trim() && !cleaned && strippedOnly) {
    // Characters survived the character-whitelist but none were letters/numbers
    // (e.g. typing the placeholder "##-###" leaves just a dash) — reject silently
    // rather than saving something that would render as a broken fragment elsewhere.
    document.getElementById('droidNameNote').textContent = 'Needs at least one letter or number — symbols alone won\\'t work.';
    document.getElementById('droidNameNote').style.color = 'var(--warning)';
  } else if (cleaned !== original) {
    e.target.value = cleaned;
    document.getElementById('droidNameNote').textContent = 'Some characters were removed — letters, numbers, spaces, dashes, and periods only.';
    document.getElementById('droidNameNote').style.color = 'var(--warning)';
  } else {
    document.getElementById('droidNameNote').textContent = 'Up to 10 characters. Letters, numbers, spaces, dashes, and periods only.';
    document.getElementById('droidNameNote').style.color = '';
  }
});

{restart_overlay_js}
</script>
</body>
</html>"""


ROAM_CARD_HTML = """  <div class="protocol-card">
    <div class="protocol-header">
      <span class="protocol-name"><i class="ti ti-robot" aria-hidden="true"></i> Autonomous Roam</span>
      <span class="protocol-status" id="roamStatus">Inactive</span>
    </div>
    <div class="protocol-body">
      <p class="protocol-desc">
        {DROID_NAME} roams autonomously — random forward bursts, turns, and occasional 180s every 2-5 minutes.
        Sounds play during movement. Full voice interaction remains active.
        Activating this deactivates any other active mode.
      </p>
      <button class="btn-protocol-on" id="roamBtn" onclick="toggleRoam()">Activate Autonomous Roam</button>
    </div>
  </div>
"""

PROTOCOLS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Subroutines">
<meta name="theme-color" content="#060a0f">
<title>Subroutines</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .subroutine-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
  @media (max-width: 480px) {
    .subroutine-grid { grid-template-columns: 1fr; }
  }
  .protocol-card { border: 1px solid transparent; border-radius: 16px; overflow: hidden;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .protocol-header { border-bottom: 1px solid rgba(143,174,193,0.3); padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px; }
  .protocol-name { font-family: var(--font-head); font-size: 14px; font-weight: 700; color: var(--gold-text); }
  .protocol-body { padding: 20px; }
  .protocol-desc { font-size: 12px; color: var(--muted); line-height: 1.8; margin-bottom: 20px; }
  .protocol-status { font-size: 12px; font-weight: 600; }
  .protocol-status.active { color: var(--warning); }
  .protocol-status.inactive { color: var(--muted); }
  .countdown-display { font-family: var(--font-head); font-size: 28px; font-weight: 700; color: var(--warning); text-align: center; padding: 16px 0; text-shadow: 0 0 20px rgba(255,200,87,0.5); display: none; margin-bottom: 16px; }
  .countdown-display.show { display: block; }
  .btn-protocol-on {
    width: 100%; border: 1px solid transparent; border-radius: 12px; padding: 14px 20px;
    font-family: var(--font-head); font-size: 14px; font-weight: 700; cursor: pointer; color: #fff;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-protocol-on:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .btn-protocol-off { width: 100%; background: transparent; color: var(--muted); border: 1px solid var(--edge); border-radius: 12px; padding: 14px 20px; font-family: var(--font-head); font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .btn-protocol-off:hover { border-color: var(--error); color: var(--error); }
</style>
</head>
<body>
<div class="wrap">
  {nav}
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Subroutines</h1>
    <p class="subtitle">Active System Configuration</p>
  </div>

  <div class="subroutine-grid">

  <div class="protocol-card">
    <div class="protocol-header">
      <span class="protocol-name"><i class="ti ti-building" aria-hidden="true"></i> Hotel Sentry</span>
      <span class="protocol-status" id="sentryStatus">Inactive</span>
    </div>
    <div class="protocol-body">
      <p class="protocol-desc">
        Activates an 8-hour patrol mode that periodically moves {DROID_NAME} to trigger hotel room motion sensors,
        keeping the air conditioning running overnight. {DROID_NAME} will only respond to stop commands while sentry is active.
        Automatically deactivates after 8 hours.
      </p>
      <div class="countdown-display" id="sentryCountdown"></div>
      <button class="btn-protocol-on" id="sentryBtn" onclick="toggleSentry()">Activate Hotel Sentry</button>
    </div>
  </div>

  <div class="protocol-card">
    <div class="protocol-header">
      <span class="protocol-name"><i class="ti ti-sparkles" aria-hidden="true"></i> Expressive Mode</span>
      <span class="protocol-status" id="expressiveStatus">Inactive</span>
    </div>
    <div class="protocol-body">
      <p class="protocol-desc">
        Enables physical motor reactions to emotional responses. {DROID_NAME} will occasionally move in response
        to what it's feeling — charging forward when angry, retreating when scared, happy dancing when
        excited. Specific voice commands also trigger animations: "hell yeah", "look out", "come here",
        "back up". Runs alongside Conversational mode.
      </p>
      <button class="btn-protocol-on" id="expressiveBtn" onclick="toggleExpressive()">Activate Expressive Mode</button>
    </div>
  </div>

  {roam_card_html}
  <div class="protocol-card">
    <div class="protocol-header">
      <span class="protocol-name"><i class="ti ti-paw" aria-hidden="true"></i> Pet Entertainer</span>
      <span class="protocol-status" id="petStatus">Inactive</span>
    </div>
    <div class="protocol-body">
      <p class="protocol-desc">
        {DROID_NAME} moves erratically to entertain pets — faster, more unpredictable movement every 30 seconds to 3 minutes.
        Sounds play during movement. Activating this deactivates any other active mode.
      </p>
      <button class="btn-protocol-on" id="petBtn" onclick="togglePet()">Activate Pet Entertainer</button>
    </div>
  </div>

  </div>

  <div class="footer"><p>K.Y.B.E.R. — Kinetic Yammering and Behavioral Engine Routines — Open Source Project</p></div>
</div>
<script>
const SETTINGS_URL = 'http://' + location.hostname + ':5001';
let sentryActive = false;

function formatCountdown(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m remaining` : `${m}m remaining`;
}

async function pollSentry() {
  try {
    const res = await fetch('/hotel_status');
    const data = await res.json();
    sentryActive = data.active;
    const status = document.getElementById('sentryStatus');
    const btn = document.getElementById('sentryBtn');
    const cd = document.getElementById('sentryCountdown');

    if (data.active) {
      status.textContent = 'Active';
      status.className = 'protocol-status active';
      cd.textContent = formatCountdown(data.remaining_seconds);
      cd.classList.add('show');
      btn.textContent = 'Deactivate Hotel Sentry';
      btn.className = 'btn-protocol-off';
    } else {
      status.textContent = 'Inactive';
      status.className = 'protocol-status inactive';
      cd.classList.remove('show');
      btn.textContent = 'Activate Hotel Sentry';
      btn.className = 'btn-protocol-on';
    }
  } catch(e) {}
}

async function toggleSentry() {
  try {
    const res = await fetch('/hotel_toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: !sentryActive})
    });
    // Give brain a moment to update state before polling
    await new Promise(r => setTimeout(r, 1000));
    await pollSentry();
  } catch(e) {}
}

// Was missing the same initial-load + recurring poll the other three
// protocols below already have — without it, this badge only ever
// refreshed when you personally clicked its own toggle button, so a page
// reload (or activating Sentry by voice instead of the button) left it
// stuck showing stale state.
setTimeout(pollSentry, 2000);
setInterval(pollSentry, 30000);

async function toggleExpressive() {
  try {
    await fetch('/expressive_toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: !expressiveActive})
    });
    await new Promise(r => setTimeout(r, 1000));
    await pollExpressive();
  } catch(e) {}
}

let expressiveActive = false;

async function pollExpressive() {
  try {
    const res = await fetch('/expressive_status');
    const data = await res.json();
    expressiveActive = data.active;
    const status = document.getElementById('expressiveStatus');
    const btn = document.getElementById('expressiveBtn');
    if (data.active) {
      status.textContent = 'Active';
      status.className = 'protocol-status active';
      btn.textContent = 'Deactivate Expressive Mode';
      btn.className = 'btn-protocol-off';
    } else {
      status.textContent = 'Inactive';
      status.className = 'protocol-status inactive';
      btn.textContent = 'Activate Expressive Mode';
      btn.className = 'btn-protocol-on';
    }
  } catch(e) {}
}

setTimeout(pollExpressive, 2000);
setInterval(pollExpressive, 30000);

let roamActive = false;
let petActive = false;

async function pollRoam() {
  try {
    const res = await fetch('/roam_status');
    const data = await res.json();
    roamActive = data.active;
    const status = document.getElementById('roamStatus');
    const btn = document.getElementById('roamBtn');
    if (data.active) {
      status.textContent = 'Active'; status.className = 'protocol-status active';
      btn.textContent = 'Deactivate Autonomous Roam'; btn.className = 'btn-protocol-off';
    } else {
      status.textContent = 'Inactive'; status.className = 'protocol-status inactive';
      btn.textContent = 'Activate Autonomous Roam'; btn.className = 'btn-protocol-on';
    }
  } catch(e) {}
}

async function toggleRoam() {
  try {
    await fetch('/roam_toggle', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: !roamActive}) });
    await new Promise(r => setTimeout(r, 1000));
    await pollRoam();
  } catch(e) {}
}

async function pollPet() {
  try {
    const res = await fetch('/pet_status');
    const data = await res.json();
    petActive = data.active;
    const status = document.getElementById('petStatus');
    const btn = document.getElementById('petBtn');
    if (data.active) {
      status.textContent = 'Active'; status.className = 'protocol-status active';
      btn.textContent = 'Deactivate Pet Entertainer'; btn.className = 'btn-protocol-off';
    } else {
      status.textContent = 'Inactive'; status.className = 'protocol-status inactive';
      btn.textContent = 'Activate Pet Entertainer'; btn.className = 'btn-protocol-on';
    }
  } catch(e) {}
}

async function togglePet() {
  try {
    await fetch('/pet_toggle', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({active: !petActive}) });
    await new Promise(r => setTimeout(r, 1000));
    await pollPet();
  } catch(e) {}
}

setTimeout(pollRoam, 2500);
setInterval(pollRoam, 30000);
setTimeout(pollPet, 3000);
setInterval(pollPet, 30000);
</script>
</body>
</html>"""


CONTROLS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Gestures">
<meta name="theme-color" content="#060a0f">
<title>Gestures</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .control-section { border: 1px solid transparent; border-radius: 16px; overflow: hidden; margin-bottom: 16px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .control-section-header { padding: 14px 20px; font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); border-bottom: 1px solid rgba(143,174,193,0.3); }
  .control-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 16px; }
  @media (max-width: 480px) {
    .control-grid { grid-template-columns: 1fr; }
  }
  .btn-control {
    border: 1px solid transparent; border-radius: 12px; padding: 16px 12px;
    font-family: var(--font-head); font-size: 13px; font-weight: 600; color: #fff; cursor: pointer; text-align: center;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-control:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .btn-control.full-width { grid-column: 1 / -1; }
  .btn-control .icon { font-size: 20px; display: block; margin-bottom: 6px; }
  .feedback { font-size: 12px; color: var(--success); text-align: center; padding: 8px; min-height: 24px; }
</style>
</head>
<body>
<div class="wrap">
  {nav}
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Gestures</h1>
    <p class="subtitle">Manual Command Interface</p>
  </div>

  <div class="control-section">
    <div class="control-section-header"><i class="ti ti-arrows-move" aria-hidden="true"></i> Movement</div>
    <div class="control-grid">
      <button class="btn-control full-width" onclick="sendMotor('happy_dance')">
        <span class="icon"><i class="ti ti-confetti" aria-hidden="true"></i></span>Happy Dance
      </button>
      <button class="btn-control full-width" onclick="sendMotor('happy_spin')">
        <span class="icon"><i class="ti ti-rotate-clockwise" aria-hidden="true"></i></span>Happy Spin
      </button>
      <button class="btn-control full-width" onclick="sendMotor('moonwalk')">
        <span class="icon"><i class="ti ti-moon" aria-hidden="true"></i></span>Moonwalk
      </button>
      <button class="btn-control" onclick="sendMotor('angry_charge')">
        <span class="icon"><i class="ti ti-flame" aria-hidden="true"></i></span>Angry Charge
      </button>
      <button class="btn-control" onclick="sendMotor('retreat')">
        <span class="icon"><i class="ti ti-ghost-2" aria-hidden="true"></i></span>Scared Retreat
      </button>
      <button class="btn-control" onclick="sendMotor('sad_drift')">
        <span class="icon"><i class="ti ti-mood-sad" aria-hidden="true"></i></span>Sad Drift
      </button>
      <button class="btn-control" onclick="sendMotor('curious_nudge')">
        <span class="icon"><i class="ti ti-help" aria-hidden="true"></i></span>Curious Nudge
      </button>
      <button class="btn-control" onclick="sendMotor('defensive_back')">
        <span class="icon"><i class="ti ti-shield" aria-hidden="true"></i></span>Defensive Back
      </button>
      <button class="btn-control full-width" onclick="sendMotor('about_face')">
        <span class="icon"><i class="ti ti-rotate-2" aria-hidden="true"></i></span>About Face
      </button>
      <button class="btn-control full-width" onclick="sendMotor('hotel')">
        <span class="icon"><i class="ti ti-bed" aria-hidden="true"></i></span>Hotel Patrol
      </button>
      <button class="btn-control full-width" onclick="sendMotor('hotel_v2')">
        <span class="icon"><i class="ti ti-flask" aria-hidden="true"></i></span>Hotel V2 (Test)
      </button>
    </div>
    <div class="feedback" id="motorFeedback"></div>
  </div>

  <div class="footer"><p>K.Y.B.E.R. — Kinetic Yammering and Behavioral Engine Routines — Open Source Project</p></div>
</div>
<script>
async function sendMotor(command) {
  const fb = document.getElementById('motorFeedback');
  fb.textContent = 'Sending...';
  try {
    await fetch('/motor_command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command})
    });
    fb.textContent = command.replace('_', ' ').toUpperCase() + ' ✓';
    setTimeout(() => { fb.textContent = ''; }, 2000);
  } catch(e) {
    fb.textContent = 'Error — is {DROID_NAME} connected?';
  }
}
</script>
</body>
</html>"""


MOTIVATOR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Motivator Configuration Panel">
<meta name="theme-color" content="#060a0f">
<title>Motivator Configuration Panel</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .control-section { border: 1px solid transparent; border-radius: 16px; overflow: hidden; margin-bottom: 16px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .control-section-header { padding: 14px 20px; font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); border-bottom: 1px solid rgba(143,174,193,0.3); }
  .btn-control { border: 1px solid transparent; border-radius: 12px; padding: 16px 12px;
    font-family: var(--font-head); font-size: 13px; font-weight: 600; color: #fff; cursor: pointer; text-align: center; width: 100%;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .btn-control:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text); }
  .motion-card { border: 1px solid transparent; border-radius: 12px; padding: 14px 16px; margin: 0 16px 12px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; }
  .motion-card:first-child { margin-top: 16px; }
  .motion-card:last-child { margin-bottom: 16px; }
  .motion-card-head { display: flex; align-items: center; gap: 8px; font-family: var(--font-head); font-size: 14px; font-weight: 700; color: var(--gold-text); margin-bottom: 10px; }
  .motion-card-head i { font-size: 17px; color: var(--gold-light); }
  .motion-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-family: var(--font-mono); font-size: 12px; color: var(--muted); }
  .motion-row label { width: 60px; flex-shrink: 0; }
  .motion-num { width: 58px; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 8px; color: var(--text); font-family: var(--font-mono); font-size: 12px; padding: 5px 7px; outline: none; }
  .motion-unit { color: var(--muted); }
  .motion-slider { -webkit-appearance: none; -moz-appearance: none; appearance: none; flex: 1; height: 4px; border-radius: 2px; outline: none; background: var(--edge); }
  .motion-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 16px; height: 16px; border-radius: 50%; background: var(--gold-light); cursor: pointer; border: 2px solid var(--void); margin-top: -6px; }
  .motion-slider::-moz-range-thumb { width: 16px; height: 16px; border-radius: 50%; background: var(--gold-light); cursor: pointer; border: 2px solid var(--void); }
  .motion-slider::-moz-range-track { background: var(--edge); height: 4px; border-radius: 2px; }
  .motion-readout { width: 52px; text-align: right; color: var(--gold-text); }
  .motion-feedback { font-size: 11px; color: var(--success); text-align: center; padding: 6px 0 0; min-height: 16px; }
</style>
</head>
<body>
<div class="wrap">
  {nav}
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Motivator Configuration Panel</h1>
    <p class="subtitle">Direct motor tuning — not linked from any menu</p>
  </div>

  <div class="control-section">
    <div class="motion-card">
      <div class="motion-card-head"><i class="ti ti-arrow-up" aria-hidden="true"></i>Forward</div>
      <div class="motion-row"><label>Time</label><input class="motion-num" type="number" id="ml-forward-dur" value="0.5" step="0.1" min="0.1" max="3"><span class="motion-unit">sec</span></div>
      <div class="motion-row"><label>Motor 0</label><input class="motion-slider" type="range" id="ml-forward-m0" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-forward-m0-out">100/255</span></div>
      <div class="motion-row"><label>Motor 1</label><input class="motion-slider" type="range" id="ml-forward-m1" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-forward-m1-out">100/255</span></div>
      <button class="btn-control" onclick="sendMotionTest('forward')">Test Forward</button>
      <div class="motion-feedback" id="ml-forward-feedback"></div>
    </div>

    <div class="motion-card">
      <div class="motion-card-head"><i class="ti ti-arrow-down" aria-hidden="true"></i>Backward</div>
      <div class="motion-row"><label>Time</label><input class="motion-num" type="number" id="ml-backward-dur" value="0.5" step="0.1" min="0.1" max="3"><span class="motion-unit">sec</span></div>
      <div class="motion-row"><label>Motor 0</label><input class="motion-slider" type="range" id="ml-backward-m0" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-backward-m0-out">100/255</span></div>
      <div class="motion-row"><label>Motor 1</label><input class="motion-slider" type="range" id="ml-backward-m1" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-backward-m1-out">100/255</span></div>
      <button class="btn-control" onclick="sendMotionTest('backward')">Test Backward</button>
      <div class="motion-feedback" id="ml-backward-feedback"></div>
    </div>

    <div class="motion-card">
      <div class="motion-card-head"><i class="ti ti-corner-up-left" aria-hidden="true"></i>Left Turn</div>
      <div class="motion-row"><label>Time</label><input class="motion-num" type="number" id="ml-left-dur" value="0.4" step="0.1" min="0.1" max="3"><span class="motion-unit">sec</span></div>
      <div class="motion-row"><label>Motor 0</label><input class="motion-slider" type="range" id="ml-left-m0" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-left-m0-out">100/255</span></div>
      <div class="motion-row"><label>Motor 1</label><input class="motion-slider" type="range" id="ml-left-m1" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-left-m1-out">100/255</span></div>
      <button class="btn-control" onclick="sendMotionTest('left')">Test Left Turn</button>
      <div class="motion-feedback" id="ml-left-feedback"></div>
    </div>

    <div class="motion-card">
      <div class="motion-card-head"><i class="ti ti-corner-up-right" aria-hidden="true"></i>Right Turn</div>
      <div class="motion-row"><label>Time</label><input class="motion-num" type="number" id="ml-right-dur" value="0.4" step="0.1" min="0.1" max="3"><span class="motion-unit">sec</span></div>
      <div class="motion-row"><label>Motor 0</label><input class="motion-slider" type="range" id="ml-right-m0" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-right-m0-out">100/255</span></div>
      <div class="motion-row"><label>Motor 1</label><input class="motion-slider" type="range" id="ml-right-m1" min="0" max="255" step="1" value="100"><span class="motion-readout" id="ml-right-m1-out">100/255</span></div>
      <button class="btn-control" onclick="sendMotionTest('right')">Test Right Turn</button>
      <div class="motion-feedback" id="ml-right-feedback"></div>
    </div>
  </div>

  <div class="footer"><p>K.Y.B.E.R. — Kinetic Yammering and Behavioral Engine Routines — Open Source Project</p></div>
</div>
<script>
['forward','backward','left','right'].forEach(function(cat) {
  ['m0','m1'].forEach(function(m) {
    const sl = document.getElementById('ml-' + cat + '-' + m);
    const out = document.getElementById('ml-' + cat + '-' + m + '-out');
    sl.addEventListener('input', function() { out.textContent = sl.value + '/255'; });
  });
});

async function sendMotionTest(category) {
  const dur    = parseFloat(document.getElementById('ml-' + category + '-dur').value);
  const speed0 = parseInt(document.getElementById('ml-' + category + '-m0').value, 10);
  const speed1 = parseInt(document.getElementById('ml-' + category + '-m1').value, 10);
  const fb = document.getElementById('ml-' + category + '-feedback');
  fb.textContent = 'Sending...';
  try {
    await fetch('/motor_test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({category, duration: dur, speed0, speed1})
    });
    fb.textContent = dur.toFixed(1) + 's · M0 ' + speed0 + '/255 · M1 ' + speed1 + '/255 ✓';
    setTimeout(() => { fb.textContent = ''; }, 2500);
  } catch(e) {
    fb.textContent = 'Error — is {DROID_NAME} connected?';
  }
}
</script>
</body>
</html>"""


BLUETOOTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bluetooth Manager">
<meta name="theme-color" content="#060a0f">
<title>Bluetooth Manager</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .bt-section { border: 1px solid transparent; border-radius: 16px; overflow: hidden; margin-bottom: 16px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .bt-section-header { padding: 14px 20px; font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); border-bottom: 1px solid rgba(143,174,193,0.3); display: flex; align-items: center; justify-content: space-between; }
  .bt-header-icon { display: inline-block; width: 20px; height: 20px; vertical-align: -4px; }
  .bt-header-icon svg { width: 100%; height: 100%; }
  .bt-warning { display: flex; align-items: flex-start; gap: 10px; padding: 12px 20px; background: rgba(255,200,87,0.08); border-bottom: 1px solid rgba(255,200,87,0.25); font-size: 12px; color: var(--warning); line-height: 1.6; }
  .bt-warning-icon { flex-shrink: 0; font-size: 13px; margin-top: 1px; }
  .bt-device { padding: 14px 20px; border-bottom: 1px solid rgba(143,174,193,0.18); display: flex; align-items: center; gap: 12px; }
  .bt-device:last-child { border-bottom: none; }
  .bt-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .bt-dot.connected { background: var(--success); box-shadow: 0 0 6px var(--success); }
  .bt-dot.disconnected { background: var(--muted); }
  .bt-info { flex: 1; min-width: 0; }
  .bt-name { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bt-mac { font-size: 11px; color: var(--muted); margin-top: 2px; font-family: var(--font-mono); }
  .bt-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .btn-bt { padding: 6px 12px; border-radius: 8px; font-family: var(--font-head); font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.2s; border: 1px solid; }
  .btn-bt.connect { background: transparent; color: var(--blue); border-color: var(--blue); }
  .btn-bt.connect:hover { background: rgba(0,168,255,0.1); }
  .btn-bt.disconnect { background: transparent; color: var(--muted); border-color: var(--edge); }
  .btn-bt.disconnect:hover { border-color: var(--warning); color: var(--warning); }
  .btn-bt.forget { background: transparent; color: var(--muted); border-color: var(--edge); }
  .btn-bt.forget:hover { border-color: var(--error); color: var(--error); }
  .btn-scan { background: transparent; color: var(--blue); border: 1px solid var(--blue); border-radius: 8px; padding: 5px 14px; font-family: var(--font-head); font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .btn-scan:hover { background: rgba(0,168,255,0.1); }
  .btn-scan:disabled { border-color: var(--edge); color: var(--muted); cursor: default; background: transparent; }
  .bt-empty { padding: 20px; text-align: center; color: var(--muted); font-size: 12px; }
  .scan-result { padding: 14px 20px; border-bottom: 1px solid rgba(143,174,193,0.18); display: flex; align-items: center; gap: 12px; }
  .scan-result:last-child { border-bottom: none; }
  .btn-pair { padding: 6px 12px; border-radius: 8px; font-family: var(--font-head); font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.2s; border: 1px solid var(--blue); color: var(--blue); background: transparent; }
  .btn-pair:hover { background: rgba(0,168,255,0.1); }
  .scan-status { font-size: 12px; color: var(--muted); padding: 8px 20px; min-height: 16px; }
  .scan-cap { padding: 10px 20px; font-size: 12px; color: var(--muted); text-align: center; border-top: 1px solid rgba(143,174,193,0.18); font-style: italic; }
  .btn-show-more { background: transparent; border: none; color: var(--blue); font-family: var(--font-head); font-size: 12px; font-weight: 600; cursor: pointer; padding: 0; text-decoration: underline; }
  @keyframes scanning-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  .btn-scanning { animation: scanning-pulse 1s ease-in-out infinite; }
  @keyframes chirp-spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  .chirp-spinner { display: inline-block; width: 10px; height: 10px; border: 2px solid var(--muted); border-top-color: var(--blue); border-radius: 50%; animation: chirp-spin 0.7s linear infinite; vertical-align: middle; margin-right: 4px; }
  .comlink-note { display: flex; align-items: flex-start; gap: 8px; padding: 10px 20px; font-size: 12px; color: var(--success); line-height: 1.5; border-top: 1px solid rgba(143,174,193,0.18); }
  .pin-modal { display: none; position: fixed; inset: 0; background: rgba(5,18,34,0.9); z-index: 100; align-items: center; justify-content: center; }
  .pin-modal.show { display: flex; }
  .pin-box { border: 1px solid transparent; border-radius: 16px; padding: 28px; width: 90%; max-width: 320px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .pin-title { font-family: var(--font-head); font-size: 15px; font-weight: 700; color: var(--gold-text); margin-bottom: 6px; }
  .pin-sub { font-size: 12px; color: var(--muted); margin-bottom: 20px; line-height: 1.6; }
  .pin-input { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 12px; color: var(--text); font-family: var(--font-mono); font-size: 16px; letter-spacing: 0.2em; text-align: center; box-sizing: border-box; margin-bottom: 16px; }
  .pin-input:focus { outline: none; border-color: var(--blue); }
  .pin-btns { display: flex; gap: 10px; }
  .pin-btns button { flex: 1; padding: 12px; border-radius: 10px; font-family: var(--font-head); font-size: 13px; font-weight: 700; cursor: pointer; border: 1px solid transparent; }
  .pin-confirm { color: #fff;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .pin-confirm:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text); }
  .pin-cancel { background: transparent; color: var(--muted); border-color: var(--edge); }
  .pin-cancel:hover { border-color: var(--blue); color: var(--blue); }
  .feedback-bar { padding: 10px 20px; font-size: 12px; min-height: 36px; color: var(--success); }
  .feedback-bar.error { color: var(--error); }
</style>
</head>
<body>
<div class="wrap">
  {nav}
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Bluetooth Manager</h1>
    <p class="subtitle">Device Pairing &amp; Configuration</p>
  </div>

  <!-- Droid -->
  <div class="bt-section">
    <div class="bt-section-header">
      <span><span class="bt-header-icon">{droid_header_icon}</span> Droid</span>
      <button class="btn-scan" id="scanBtnDroid" onclick="startDroidScan()">Scan</button>
    </div>
    <div class="bt-warning">
      <span class="bt-warning-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>
      <span><strong>Before scanning:</strong> Ensure your droid is not connected to anything. Scan to pair your droid. Only 1 droid can be paired at a time.</span>
    </div>
    <div id="claimedDroid"><div class="bt-empty">Loading...</div></div>
    <div class="scan-status" id="scanStatusDroid"></div>
    <div id="scanListDroid"></div>
  </div>

  <!-- Comlink -->
  <div class="bt-section">
    <div class="bt-section-header">
      <span><i class="ti ti-microphone" aria-hidden="true"></i> Comlink</span>
      <button class="btn-scan" id="scanBtnComm" onclick="startScan('comm')">Scan</button>
    </div>
    <div class="bt-warning">
      <span class="bt-warning-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>
      <span><strong>Warning:</strong> It is not recommended to connect more than 1 comlink at a time.</span>
    </div>
    <div id="pairedComm"><div class="bt-empty">Loading...</div></div>
    <div class="scan-status" id="scanStatusComm"></div>
    <div id="scanListComm"></div>
  </div>

  <!-- Sensor Arrays -->
  <div class="bt-section">
    <div class="bt-section-header">
      <span><i class="ti ti-antenna" aria-hidden="true"></i> Sensor Arrays</span>
      <button class="btn-scan" id="scanBtnSensor" onclick="startScan('sensor')">Scan</button>
    </div>
    <div id="pairedSensor"><div class="bt-empty">Loading...</div></div>
    <div class="scan-status" id="scanStatusSensor"></div>
    <div id="scanListSensor"></div>
  </div>

  <div class="feedback-bar" id="feedback"></div>
  <div class="footer"><p>K.Y.B.E.R. — Kinetic Yammering and Behavioral Engine Routines — Open Source Project</p></div>
</div>

<!-- PIN Modal -->
<div class="pin-modal" id="pinModal">
  <div class="pin-box">
    <div class="pin-title">Enter PIN</div>
    <div class="pin-sub" id="pinSub">Enter the PIN for this device, or leave blank if none is required.</div>
    <input class="pin-input" id="pinInput" type="text" inputmode="numeric" placeholder="0000" maxlength="8">
    <div class="pin-btns">
      <button class="pin-confirm" onclick="confirmPair()">Pair</button>
      <button class="pin-cancel" onclick="closePinModal()">Cancel</button>
    </div>
  </div>
</div>

<script>
const SCAN_LIMIT = 15;

const SECTIONS = {
  comm:   { pairedEl: 'pairedComm',   statusEl: 'scanStatusComm',   listEl: 'scanListComm',   scanBtn: 'scanBtnComm',   isComm: true  },
  sensor: { pairedEl: 'pairedSensor', statusEl: 'scanStatusSensor', listEl: 'scanListSensor', scanBtn: 'scanBtnSensor', isComm: false },
};

let macToSection = {};
let pendingPairMac = null;
let pendingPairSection = null;

function showFeedback(msg, isError) {
  const el = document.getElementById('feedback');
  el.textContent = msg;
  el.className = 'feedback-bar' + (isError ? ' error' : '');
  setTimeout(() => { el.textContent = ''; el.className = 'feedback-bar'; }, 4000);
}

function renderPairedDevice(sectionKey, device) {
  const sec = SECTIONS[sectionKey];
  const el = document.getElementById(sec.pairedEl);
  if (!device) {
    el.innerHTML = '<div class="bt-empty">No device paired</div>';
    return;
  }
  const connectBtn = device.connected
    ? `<button class="btn-bt disconnect" onclick="disconnectDevice('${device.mac}')">Disconnect</button>`
    : `<button class="btn-bt connect" onclick="connectDevice('${device.mac}', '${sectionKey}')">Connect</button>`;
  const micNote = sec.isComm
    ? ''
    : '';
  el.innerHTML = `
    <div class="bt-device">
      <div class="bt-dot ${device.connected ? 'connected' : 'disconnected'}"></div>
      <div class="bt-info">
        <div class="bt-name">${device.name}</div>
        <div class="bt-mac">${device.mac}</div>
      </div>
      <div class="bt-actions">
        ${connectBtn}
        <button class="btn-bt forget" onclick="forgetDevice('${device.mac}', '${device.name}', '${sectionKey}')">Forget</button>
      </div>
    </div>${micNote}`;
}

function renderScanResults(sectionKey, devices, showAll) {
  const sec = SECTIONS[sectionKey];
  const el = document.getElementById(sec.listEl);
  if (!devices.length) {
    el.innerHTML = '<div class="bt-empty">No new devices found</div>';
    return;
  }
  const total = devices.length;
  const shown = showAll ? devices : devices.slice(0, SCAN_LIMIT);
  let html = shown.map(d => `
    <div class="scan-result">
      <div class="bt-info">
        <div class="bt-name">${d.name}</div>
        <div class="bt-mac">${d.mac}</div>
      </div>
      <div class="bt-actions">
        <button class="btn-pair" onclick="initPair('${d.mac}', '${d.name}', '${sectionKey}')">Pair</button>
      </div>
    </div>`).join('');
  if (!showAll && total > SCAN_LIMIT) {
    html += `<div class="scan-cap">Showing ${SCAN_LIMIT} of ${total} nearby devices &mdash; <button class="btn-show-more" onclick="showAllResults('${sectionKey}')">Show all ${total}</button></div>`;
  }
  el.innerHTML = html;
  // Store full device list on element for show-all
  el._allDevices = devices;
  el._sectionKey = sectionKey;
}

function showAllResults(sectionKey) {
  const sec = SECTIONS[sectionKey];
  const el = document.getElementById(sec.listEl);
  if (el._allDevices) renderScanResults(sectionKey, el._allDevices, true);
}

// ── Droid claiming ──────────────────────────────────────────────────────────

async function loadClaimedDroid() {
  try {
    const res = await fetch('/droid_claimed');
    const data = await res.json();
    const el = document.getElementById('claimedDroid');
    if (data.mac) {
      el.innerHTML = `
        <div class="bt-device">
          <div class="bt-dot connected"></div>
          <div class="bt-info">
            <div class="bt-name">Paired Droid</div>
            <div class="bt-mac">${data.mac}</div>
          </div>
          <div class="bt-actions">
            <button class="btn-bt forget" onclick="releaseDroid()">Unpair</button>
          </div>
        </div>
        <div class="comlink-note">&#10003; Paired — this droid will be connected on next brain restart</div>`;
    } else {
      el.innerHTML = '<div class="bt-empty">No droid paired — scan to pair your droid</div>';
    }
  } catch(e) {
    document.getElementById('claimedDroid').innerHTML = '<div class="bt-empty">Error loading</div>';
  }
}

async function startDroidScan() {
  const btn = document.getElementById('scanBtnDroid');
  const status = document.getElementById('scanStatusDroid');
  const list = document.getElementById('scanListDroid');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  btn.classList.add('btn-scanning');
  status.textContent = 'Searching for nearby droids (15 seconds)...';
  list.innerHTML = '';
  try {
    const res = await fetch('/droid_scan');
    const data = await res.json();
    if (!data.ok) {
      status.textContent = 'Scan error: ' + (data.error || 'Unknown error');
    } else if (!data.droids.length) {
      status.textContent = 'No droids found — make sure your droid is powered on and not connected to anything else';
    } else {
      status.textContent = data.droids.length + ' droid' + (data.droids.length !== 1 ? 's' : '') + ' found' + (data.droids.length > 1 ? ' — chirp to identify yours' : '');
      list.innerHTML = data.droids.map(d => `
        <div class="scan-result">
          <div class="bt-info">
            <div class="bt-name">DROID</div>
            <div class="bt-mac">${d.mac}</div>
          </div>
          <div class="bt-actions">
            <button class="btn-bt disconnect" id="chirp-${d.mac.replace(/:/g,'-')}" onclick="chirpDroid('${d.mac}')">Chirp</button>
            <button class="btn-pair" onclick="claimDroid('${d.mac}')">Pair</button>
          </div>
        </div>`).join('');
    }
  } catch(e) {
    status.textContent = 'Scan failed';
  }
  btn.disabled = false;
  btn.textContent = 'Scan Again';
  btn.classList.remove('btn-scanning');
}

async function chirpDroid(mac) {
  const btnId = 'chirp-' + mac.replace(/:/g, '-');
  const btn = document.getElementById(btnId);
  if (btn) {
    btn.innerHTML = '<span class="chirp-spinner"></span>Chirping...';
    btn.disabled = true;
  }
  showFeedback('Chirping — listen for your droid!');
  try {
    const res = await fetch('/droid_chirp', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac})
    });
    const data = await res.json();
    if (data.ok) {
      showFeedback('Chirped! Did your droid respond? If so, pair it.');
    } else {
      showFeedback('Chirp failed: ' + (data.reason || 'Unknown error'), true);
    }
  } catch(e) {
    showFeedback('Chirp error — is the droid brain running?', true);
  }
  if (btn) { btn.innerHTML = 'Chirp'; btn.disabled = false; }
}

async function claimDroid(mac) {
  showFeedback('Pairing droid...');
  try {
    const res = await fetch('/droid_claim', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac})
    });
    const data = await res.json();
    if (data.ok) {
      showFeedback('Droid paired! Restart the brain service to connect.');
      document.getElementById('scanListDroid').innerHTML = '';
      document.getElementById('scanStatusDroid').textContent = '';
      await loadClaimedDroid();
    } else {
      showFeedback('Failed: ' + (data.error || 'Unknown error'), true);
    }
  } catch(e) { showFeedback('Claim error', true); }
}

async function releaseDroid() {
  if (!confirm('Unpair this droid? The brain will connect to the nearest droid on next restart.')) return;
  try {
    await fetch('/droid_release', { method: 'POST' });
    showFeedback('Droid unpaired');
    await loadClaimedDroid();
  } catch(e) { showFeedback('Error releasing droid', true); }
}

async function loadPaired() {
  try {
    const res = await fetch('/bluetooth_paired');
    const data = await res.json();
    const devices = data.devices || [];

    const assigned = { comm: null, sensor: null };
    for (const d of devices) {
      if (d.section && assigned[d.section] === null) {
        assigned[d.section] = d;
        macToSection[d.mac] = d.section;
      }
    }
    for (const [sec, device] of Object.entries(assigned)) {
      renderPairedDevice(sec, device);
    }
  } catch(e) {
    for (const sec of Object.keys(SECTIONS)) {
      document.getElementById(SECTIONS[sec].pairedEl).innerHTML = '<div class="bt-empty">Error loading</div>';
    }
  }
}

async function startScan(sectionKey) {
  const sec = SECTIONS[sectionKey];
  const btn = document.getElementById(sec.scanBtn);
  const status = document.getElementById(sec.statusEl);
  const list = document.getElementById(sec.listEl);
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  status.textContent = 'Searching for nearby devices (10 seconds)...';
  list.innerHTML = '';
  try {
    const res = await fetch('/bluetooth_scan');
    const data = await res.json();
    if (data.error) {
      status.textContent = 'Scan error: ' + data.error;
    } else {
      const total = data.devices.length;
      status.textContent = total + ' device' + (total !== 1 ? 's' : '') + ' found';
      renderScanResults(sectionKey, data.devices);
    }
  } catch(e) {
    status.textContent = 'Scan failed';
  }
  btn.disabled = false;
  btn.textContent = 'Scan Again';
  await loadPaired();
}

function initPair(mac, name, sectionKey) {
  pendingPairMac = mac;
  pendingPairSection = sectionKey;
  // Attempt pairing without PIN first — modal only appears if device requests one
  doPair(mac, sectionKey, '');
}

function closePinModal() {
  pendingPairMac = null;
  pendingPairSection = null;
  document.getElementById('pinModal').classList.remove('show');
}

async function confirmPair() {
  if (!pendingPairMac) return;
  const pin = document.getElementById('pinInput').value.trim();
  const mac = pendingPairMac;
  const sectionKey = pendingPairSection;
  closePinModal();
  await doPair(mac, sectionKey, pin);
}

async function doPair(mac, sectionKey, pin) {
  showFeedback('Pairing... this may take a moment');
  try {
    const res = await fetch('/bluetooth_pair', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac, pin: pin || '', section: sectionKey})
    });
    const data = await res.json();
    if (data.ok) {
      macToSection[mac] = sectionKey;
      if (SECTIONS[sectionKey].isComm) {
        showFeedback('Paired! Connecting and setting mic...');
        await connectDevice(mac, sectionKey);
      } else {
        showFeedback('Paired successfully!');
        const sec = SECTIONS[sectionKey];
        document.getElementById(sec.listEl).innerHTML = '';
        document.getElementById(sec.statusEl).textContent = '';
        await loadPaired();
      }
    } else if (data.needs_pin) {
      // Device is requesting a PIN — show the modal now
      pendingPairMac = mac;
      pendingPairSection = sectionKey;
      document.getElementById('pinSub').textContent = 'This device requires a PIN. Enter it below.';
      document.getElementById('pinInput').value = '';
      document.getElementById('pinModal').classList.add('show');
      document.getElementById('pinInput').focus();
      showFeedback('PIN required — enter it in the dialog', false);
    } else {
      showFeedback('Pairing failed: ' + (data.error || 'Unknown error'), true);
    }
  } catch(e) {
    showFeedback('Pairing error', true);
  }
}

async function connectDevice(mac, sectionKey) {
  const isComm = sectionKey && SECTIONS[sectionKey] && SECTIONS[sectionKey].isComm;
  showFeedback(isComm ? 'Connecting and setting mic...' : 'Connecting...');
  try {
    const endpoint = isComm ? '/bluetooth_connect_mic' : '/bluetooth_connect';
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac, section: sectionKey})
    });
    const data = await res.json();
    if (data.ok) {
      showFeedback(isComm ? 'Comlink connected — mic active. Restart droid to apply.' : 'Connected!');
      if (sectionKey) {
        document.getElementById(SECTIONS[sectionKey].listEl).innerHTML = '';
        document.getElementById(SECTIONS[sectionKey].statusEl).textContent = '';
      }
    } else {
      showFeedback('Failed: ' + (data.error || 'Unknown error'), true);
    }
    await loadPaired();
  } catch(e) { showFeedback('Connection error', true); }
}

async function disconnectDevice(mac) {
  showFeedback('Disconnecting...');
  try {
    await fetch('/bluetooth_disconnect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac})
    });
    showFeedback('Disconnected');
    await loadPaired();
  } catch(e) { showFeedback('Disconnect error', true); }
}

async function forgetDevice(mac, name, sectionKey) {
  if (!confirm(`Remove "${name}" from paired devices?`)) return;
  try {
    const res = await fetch('/bluetooth_forget', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac})
    });
    const data = await res.json();
    if (data.ok) {
      delete macToSection[mac];
      showFeedback('Device removed');
    } else {
      showFeedback('Failed: ' + data.error, true);
    }
    await loadPaired();
  } catch(e) { showFeedback('Error removing device', true); }
}

document.getElementById('pinModal').addEventListener('click', function(e) {
  if (e.target === this) closePinModal();
});

document.getElementById('pinInput').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') confirmPair();
});

loadClaimedDroid();
loadPaired();
setInterval(loadPaired, 10000);
setInterval(loadClaimedDroid, 15000);
</script>
</body>
</html>"""


CALIBRATION_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Calibration">
<meta name="theme-color" content="#060a0f">
<title>Calibration</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .cal-section { border: 1px solid transparent; border-radius: 16px; overflow: hidden; margin-bottom: 16px;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .cal-section-header { padding: 14px 20px; font-family: var(--font-head); font-size: 17px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--gold-text); border-bottom: 1px solid rgba(143,174,193,0.3); }
  .bt-warning { display: flex; align-items: flex-start; gap: 10px; padding: 12px 20px; background: rgba(255,200,87,0.08); border: 1px solid rgba(255,200,87,0.25); border-radius: 12px; font-size: 12px; color: var(--warning); line-height: 1.6; margin-bottom: 16px; }
  .bt-warning-icon { flex-shrink: 0; font-size: 13px; margin-top: 1px; }
  .cal-body { padding: 20px; }
  .cal-status-msg { text-align: center; font-size: 12px; color: var(--muted); padding: 8px 0 4px; }
  .cal-spinner { width: 36px; height: 36px; border: 3px solid var(--edge); border-top-color: var(--blue); border-radius: 50%; margin: 0 auto 16px; animation: cal-spin 0.8s linear infinite; }
  @keyframes cal-spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  .cal-question { text-align: center; font-size: 13px; color: var(--text); margin-bottom: 18px; line-height: 1.6; }
  .cal-option-stack .btn-cal-option { display: block; width: 100%; margin-bottom: 8px; }
  .cal-option-stack .btn-cal-option:last-child { margin-bottom: 0; }
  .btn-cal-option {
    border: 1px solid transparent; border-radius: 12px; padding: 13px 10px;
    font-family: var(--font-head); font-size: 13px; font-weight: 600; color: #fff; cursor: pointer;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-cal-option:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-cal-option:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .cal-btn-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .cal-success { text-align: center; padding: 8px 0; }
  .cal-success .big-check { font-size: 36px; color: var(--success); margin-bottom: 8px; }
  .cal-warning-box { background: rgba(255,200,87,0.08); border: 1px solid rgba(255,200,87,0.25); border-radius: 10px; padding: 16px; font-size: 12px; color: var(--warning); line-height: 1.7; margin-top: 14px; text-align: left; }
  .btn-control {
    border: 1px solid transparent; border-radius: 12px; padding: 16px 12px;
    font-family: var(--font-head); font-size: 13px; font-weight: 600; color: #fff; cursor: pointer; text-align: center;
    background-image: linear-gradient(135deg, #1D4268, #0D2C4D), linear-gradient(135deg, var(--edge), var(--dim));
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow);
  }
  .btn-control:hover {
    background-image: linear-gradient(135deg, var(--gold-light), var(--gold-dark)), linear-gradient(135deg, var(--gold-border-light), var(--gold-border-dark));
    color: var(--gold-text);
  }
  .btn-control.full-width { width: 100%; }
</style>
</head>
<body>
<div class="wrap">
  {nav}
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Calibration</h1>
    <p class="subtitle">Motor Run-Time Calibration</p>
  </div>

  <div class="bt-warning">
    <span class="bt-warning-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>
    <span>Place your droid on a hard, flat surface.</span>
  </div>

  <div class="cal-section">
    <div class="cal-section-header">Calibration Wizard</div>
    <div class="cal-body" id="calBody">
      <button class="btn-control full-width" onclick="startCalibration()">&#9654; Run Calibration</button>
    </div>
  </div>

  <div class="footer"><p>K.Y.B.E.R. &mdash; Kinetic Yammering and Behavioral Engine Routines &mdash; Open Source Project</p></div>
</div>

<script>
// Friendly-worded magnitude buckets, asked separately after each spin. "Full
// 360" is the only one that counts as a pass — everything else feeds a fraction
// used to size the next attempt's correction. Provisional, easy to retune.
const BUCKET_FRACTION = {
  under_quarter:      0.15,
  quarter:            0.25,
  half:               0.5,
  three_quarter:      0.75,
  over_three_quarter: 0.9,
  full:               1.0
};
const MAX_ATTEMPTS = 3;
const droidName = "{DROID_NAME}";

let attemptNum    = 0;
let scale         = 1.0;
let rightFraction = 0;
let leftFraction  = 0;

function calBody() { return document.getElementById('calBody'); }

function renderIdle() {
  calBody().innerHTML = '<button class="btn-control full-width" onclick="startCalibration()">&#9654; Run Calibration</button>';
}

function renderError(msg) {
  calBody().innerHTML = `<div class="cal-status-msg">${msg}</div>
    <button class="btn-control full-width" style="margin-top:12px" onclick="startCalibration()">Try Again</button>`;
}

function renderSpinning(label) {
  calBody().innerHTML = `
    <div class="cal-spinner"></div>
    <div class="cal-status-msg">${label}</div>`;
}

function renderBucketQuestion(directionLabel, onAnswer) {
  calBody().innerHTML = `
    <div class="cal-question">How far did ${droidName} get spinning 360&deg; in the ${directionLabel} direction?</div>
    <div class="cal-option-stack" id="bucketBtns"></div>`;
  const opts = [
    ['full',               'Made a full 360&deg; spin'],
    ['over_three_quarter', 'Over 3/4 of the way'],
    ['three_quarter',      '3/4 of the way'],
    ['half',               '1/2 of the way'],
    ['quarter',            '1/4 of the way'],
    ['under_quarter',      'Less than 1/4 of the way'],
  ];
  const container = document.getElementById('bucketBtns');
  opts.forEach(([key, label]) => {
    const btn = document.createElement('button');
    btn.className = 'btn-cal-option';
    btn.innerHTML = label;
    btn.onclick = () => onAnswer(key);
    container.appendChild(btn);
  });
}

function renderVictory() {
  calBody().innerHTML = `
    <div class="cal-success">
      <div class="big-check">&#10003;</div>
      <div class="cal-question">Victory spin&hellip;</div>
    </div>`;
}

function renderSuccess(scaleValue, liveApplied) {
  const statusMsg = liveApplied
    ? 'Applied immediately &mdash; no restart needed.'
    : 'Saved, but couldn&rsquo;t reach the brain to apply it live. Go to Mainframe, click Save &amp; Restart, then power-cycle your droid.';
  calBody().innerHTML = `
    <div class="cal-success">
      <div class="big-check">&#10003;</div>
      <div class="cal-question">Calibration locked in.</div>
      <div class="cal-status-msg">${statusMsg}</div>
    </div>`;
}

function renderGiveUp(scaleValue, liveApplied) {
  const statusMsg = liveApplied
    ? 'Best-effort value applied immediately &mdash; no restart needed.'
    : 'Best-effort value saved, but couldn&rsquo;t reach the brain to apply it live. Go to Mainframe, click Save &amp; Restart, then power-cycle your droid.';
  calBody().innerHTML = `
    <div class="cal-status-msg" style="text-align:center">Best-effort value stored.</div>
    <div class="cal-warning-box"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>If your droid has failed to execute a 360 spin after 3 attempts, please be sure your droid has a fresh set of batteries and rerun the calibration. If the problem persists, your droid's motivators may need servicing.</div>
    <div class="cal-status-msg" style="margin-top:10px">${statusMsg}</div>
    <button class="btn-control full-width" style="margin-top:14px" onclick="startCalibration()">Run Calibration Again</button>`;
}

async function probe(direction, scaleValue) {
  const res = await fetch('/calibration_probe', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({direction, scale: scaleValue})
  });
  const result = await res.json();
  if (!result.ok) throw new Error(result.reason || 'probe failed');
  return result;
}

async function victorySpin() {
  const res = await fetch('/calibration_victory', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'}
  });
  const result = await res.json();
  if (!result.ok) throw new Error(result.reason || 'victory spin failed');
  return result;
}

async function lockIn(scaleValue) {
  const res = await fetch('/calibration_lock', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({left_scale: scaleValue, right_scale: scaleValue})
  });
  return res.json();
}

async function startCalibration() {
  attemptNum = 0;
  scale = 1.0;
  runBlock();
}

async function runBlock() {
  attemptNum += 1;
  try {
    renderSpinning('Spinning right&hellip; watch your droid');
    await probe('right', scale);
    renderBucketQuestion('right', (key) => {
      rightFraction = BUCKET_FRACTION[key];
      continueToLeft();
    });
  } catch (e) {
    renderError(`Error: ${e.message}`);
  }
}

async function continueToLeft() {
  try {
    renderSpinning('Spinning left&hellip; watch your droid');
    await probe('left', scale);
    renderBucketQuestion('left', (key) => {
      leftFraction = BUCKET_FRACTION[key];
      afterBlock();
    });
  } catch (e) {
    renderError(`Error: ${e.message}`);
  }
}

async function afterBlock() {
  if (rightFraction >= 1.0 && leftFraction >= 1.0) {
    try {
      renderVictory();
      await victorySpin();
    } catch (e) {
      // Victory spin is cosmetic — don't let it block actually locking in.
    }
    const lockResult = await lockIn(scale);
    renderSuccess(scale, lockResult.live_applied);
    return;
  }

  if (attemptNum >= MAX_ATTEMPTS) {
    const lockResult = await lockIn(scale);
    renderGiveUp(scale, lockResult.live_applied);
    return;
  }

  // Size the correction off whichever side fell shorter, so the fix is big
  // enough to cover the worse of the two instead of just splitting the
  // difference and possibly under-correcting.
  const worstFraction = Math.min(rightFraction, leftFraction);
  scale = scale / worstFraction;
  runBlock();
}
</script>
</body>
</html>"""


EDIT_PERSONALITY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edit Personality</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .back-link { display: inline-block; font-size: 12px; color: var(--blue); text-decoration: none; margin-bottom: 16px; font-weight: 600; }
  .back-link:hover { text-decoration: underline; }
  .trait-row { display: flex; flex-direction: column; gap: 4px; }
  .trait-head { display: flex; justify-content: space-between; align-items: baseline; }
  .trait-name { font-size: 12px; font-weight: 600; color: var(--muted); }
  .trait-value { font-size: 14px; font-weight: 700; color: var(--blue); }
  .trait-slider { -webkit-appearance: none; -moz-appearance: none; appearance: none; width: 100%; height: 4px; border-radius: 2px; outline: none; margin: 8px 0 4px; background: var(--edge); }
  .trait-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 16px; height: 16px; border-radius: 50%; background: var(--blue); cursor: pointer; border: 2px solid var(--void); margin-top: -6px; }
  .trait-slider::-moz-range-thumb { width: 16px; height: 16px; border-radius: 50%; background: var(--blue); cursor: pointer; border: 2px solid var(--void); }
  .trait-slider::-moz-range-track { background: var(--edge); height: 4px; border-radius: 2px; }
  .trait-slider::-moz-range-progress { background: var(--blue); height: 4px; border-radius: 2px; }
  .trait-desc { font-size: 12px; color: var(--muted); }
  .locked-note { font-size: 12px; color: var(--warning); background: rgba(255,200,87,0.08); border: 1px solid rgba(255,200,87,0.25); border-radius: 10px; padding: 10px 12px; margin-bottom: 16px; line-height: 1.6; }
  .save-overlay { display: none; position: fixed; inset: 0; background: rgba(5,18,34,0.88); z-index: 200; align-items: center; justify-content: center; }
  .save-overlay.show { display: flex; }
  .save-box { border: 1px solid transparent; border-radius: 16px; padding: 28px 24px; max-width: 360px; width: 90%;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .save-box h2 { font-family: var(--font-head); font-size: 16px; font-weight: 700; letter-spacing: 0.04em; color: var(--gold-text); margin-bottom: 16px; text-align: center; text-transform: uppercase; }
  .slot-list { display: flex; flex-direction: column; gap: 6px; margin: 12px 0 20px; max-height: 220px; overflow-y: auto; }
  .slot-row { display: flex; align-items: center; gap: 10px; background: rgba(255,255,255,0.04); border: 1px solid var(--edge); border-radius: 10px; padding: 10px 12px; cursor: pointer; }
  .slot-row input[type="radio"] { accent-color: var(--blue); width: 14px; height: 14px; flex-shrink: 0; cursor: pointer; }
  .slot-row span { flex: 1; font-size: 12px; }
  .slot-row span.occupied { color: var(--warning); }
</style>
</head>
<body>
<div class="wrap">
  <a class="back-link" href="/">&#8249; Mainframe</a>
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Editing &mdash; {profile_name}</h1>
  </div>

  {locked_note}

  <div class="panel">
    <div class="panel-header">Personality Dial</div>
    <div class="panel-body">

      <div class="trait-row">
        <div class="trait-head"><span class="trait-name">Brave</span><span class="trait-value" id="v-brave">{brave}</span></div>
        <input class="trait-slider" type="range" min="1" max="5" step="1" value="{brave}" id="s-brave" oninput="fillSlider(this); document.getElementById('v-brave').textContent=this.value">
        <p class="trait-desc">Low: spooks easily — high: holds its ground.</p>
      </div>
      <hr class="divider">
      <div class="trait-row">
        <div class="trait-head"><span class="trait-name">Curious</span><span class="trait-value" id="v-curious">{curious}</span></div>
        <input class="trait-slider" type="range" min="1" max="5" step="1" value="{curious}" id="s-curious" oninput="fillSlider(this); document.getElementById('v-curious').textContent=this.value">
        <p class="trait-desc">Low: shrugs off the unknown — high: can't resist a mystery.</p>
      </div>
      <hr class="divider">
      <div class="trait-row">
        <div class="trait-head"><span class="trait-name">Sassy</span><span class="trait-value" id="v-sassy">{sassy}</span></div>
        <input class="trait-slider" type="range" min="1" max="5" step="1" value="{sassy}" id="s-sassy" oninput="fillSlider(this); document.getElementById('v-sassy').textContent=this.value">
        <p class="trait-desc">Low: lets it slide — high: claps back.</p>
      </div>
      <hr class="divider">
      <div class="trait-row">
        <div class="trait-head"><span class="trait-name">Playful</span><span class="trait-value" id="v-playful">{playful}</span></div>
        <input class="trait-slider" type="range" min="1" max="5" step="1" value="{playful}" id="s-playful" oninput="fillSlider(this); document.getElementById('v-playful').textContent=this.value">
        <p class="trait-desc">Low: keeps composed — high: hypes hard.</p>
      </div>
      <hr class="divider">
      <div class="trait-row">
        <div class="trait-head"><span class="trait-name">Sensitive</span><span class="trait-value" id="v-sensitive">{sensitive}</span></div>
        <input class="trait-slider" type="range" min="1" max="5" step="1" value="{sensitive}" id="s-sensitive" oninput="fillSlider(this); document.getElementById('v-sensitive').textContent=this.value">
        <p class="trait-desc">Low: brushes off bad news — high: takes it to heart.</p>
      </div>

    </div>
  </div>

  <p class="field-note" style="margin-bottom:16px">Read once at launch — applies after Save &amp; Restart.</p>

  <div class="btn-row">
    <button type="button" class="primary" onclick="openSaveDialog()">Save &amp; Restart</button>
    <a class="secondary" href="/" style="display:flex;align-items:center;justify-content:center;text-decoration:none;">Cancel</a>
  </div>

  <div class="footer"><p>K.Y.B.E.R. — Kinetic Yammering and Behavioral Engine Routines — Open Source Project</p></div>
</div>

<!-- Save dialog -->
<div class="save-overlay" id="saveOverlay">
  <div class="save-box">
    <h2>{save_dialog_title}</h2>
    {locked_warning}
    <div class="field" style="margin-bottom:16px">
      <label class="field-label">Name</label>
      <div class="input-wrap"><input type="text" id="saveNameInput" value="{name_value}" placeholder="{name_placeholder}"></div>
    </div>
    {slot_picker}
    <div class="btn-row">
      <button class="secondary" type="button" onclick="closeSaveDialog()">Cancel</button>
      <button class="primary" type="button" onclick="confirmSave()">Save &amp; Restart</button>
    </div>
  </div>
</div>

<script>
function fillSlider(el) {
  const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
  el.style.background = `linear-gradient(to right, var(--blue) 0%, var(--blue) ${pct}%, var(--edge) ${pct}%, var(--edge) 100%)`;
}
document.querySelectorAll('.trait-slider').forEach(fillSlider);

function openSaveDialog() {
  document.getElementById('saveOverlay').classList.add('show');
}
function closeSaveDialog() {
  document.getElementById('saveOverlay').classList.remove('show');
}

async function confirmSave() {
  const name = document.getElementById('saveNameInput').value.trim();
  const slotRadio = document.querySelector('input[name="targetSlot"]:checked');
  const targetSlot = slotRadio ? slotRadio.value : null;
  const traits = {
    brave: document.getElementById('s-brave').value,
    curious: document.getElementById('s-curious').value,
    sassy: document.getElementById('s-sassy').value,
    playful: document.getElementById('s-playful').value,
    sensitive: document.getElementById('s-sensitive').value
  };
  const body = { name: name, traits: traits, source_slot: '{slot}' };
  if (targetSlot) body.target_slot = targetSlot;
  await fetch('/save_personality', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  window.location.href = '/?restarting=1';
}
</script>
</body>
</html>"""


RESET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Reset">
<meta name="theme-color" content="#060a0f">
<title>Reset Droid Profile</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.35.0/tabler-icons-outline.min.css">
<style>
""" + SHARED_CSS + """
  .reset-card { border: 1px solid transparent; border-radius: 16px; overflow: hidden;
    background-image: linear-gradient(135deg, var(--panel) 0%, var(--void) 100%), linear-gradient(135deg, var(--edge) 0%, var(--dim) 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); padding: 24px 20px; }
  .reset-card p { font-size: 13px; color: var(--text); line-height: 1.7; margin-bottom: 20px; }
  .btn-reset-trigger { width: 100%; border-radius: 12px; padding: 16px 20px; font-family: var(--font-head); font-size: 14px; font-weight: 700; letter-spacing: 0.02em; cursor: pointer; background: transparent; color: var(--error); border: 1px solid var(--error); }
  .btn-reset-trigger:hover { background: rgba(230,57,70,0.1); }
  .reset-overlay { display: none; position: fixed; inset: 0; background: rgba(20,4,4,0.85); z-index: 100; align-items: center; justify-content: center; padding: 20px; }
  .reset-overlay.show { display: flex; }
  .reset-overlay-box { border-radius: 16px; padding: 30px 26px; max-width: 320px; width: 100%; text-align: center; border: 1px solid transparent;
    background-image: linear-gradient(135deg, #2b1010 0%, #160606 100%), linear-gradient(135deg, var(--error) 0%, #7a1e26 100%);
    background-origin: border-box; background-clip: padding-box, border-box; box-shadow: 0 2px 4px var(--shadow); }
  .reset-overlay-box i { font-size: 32px; color: var(--error); margin-bottom: 14px; }
  .reset-overlay-box .headline { font-size: 15px; font-weight: 700; color: #ffeceb; margin-bottom: 10px; line-height: 1.5; }
  .reset-overlay-box .subline { font-size: 13px; color: #f0b8bb; margin-bottom: 24px; }
  .btn-reset-cancel { flex: 1; background: transparent; color: var(--muted); border: 1px solid var(--edge); border-radius: 12px; padding: 13px 16px; font-family: var(--font-head); font-size: 13px; font-weight: 600; cursor: pointer; }
  .btn-reset-confirm { flex: 1; border: 1px solid transparent; border-radius: 12px; padding: 13px 16px; font-family: var(--font-head); font-size: 13px; font-weight: 700; cursor: pointer; background: var(--error); color: #fff; }
</style>
</head>
<body>
<div class="wrap">
  {nav}
  <div class="header">
    <div class="r2-icon">{r2_svg}</div>
    <h1>Reset Droid Profile</h1>
    <p class="subtitle">For moving KYBER to a different droid, or starting fresh</p>
  </div>

  <div class="reset-card">
    <p>Use this if you're transplanting KYBER into a different droid, or want to start this one over from scratch. Pairing, model, personality assignment, sound profile, and calibration all reset &mdash; you'll go back through setup for just those steps. Your API keys, mic setup, and saved presets stay exactly as they are.</p>
    <button type="button" class="btn-reset-trigger" onclick="document.getElementById('resetOverlay').classList.add('show')">
      <i class="ti ti-refresh" aria-hidden="true"></i> Reset droid profile
    </button>
  </div>

  <div class="footer"><p>K.Y.B.E.R. &mdash; Kinetic Yammering and Behavioral Engine Routines &mdash; Open Source Project</p></div>
</div>

<div class="reset-overlay" id="resetOverlay">
  <div class="reset-overlay-box">
    <i class="ti ti-alert-triangle" aria-hidden="true"></i>
    <div class="headline">This is not reversible and will erase your droid's profile in KYBER.</div>
    <div class="subline">Are you sure?</div>
    <div class="btn-row">
      <button type="button" class="btn-reset-cancel" onclick="document.getElementById('resetOverlay').classList.remove('show')">Cancel</button>
      <button type="button" class="btn-reset-confirm" onclick="document.getElementById('resetConfirmForm').submit()">Continue</button>
    </div>
  </div>
</div>
<form id="resetConfirmForm" method="POST" action="/reset/confirm" style="display:none"></form>
</body>
</html>"""


def safe_render(template: str, **kwargs) -> str:
    """Replace {key} placeholders without touching CSS curly braces."""
    result = template
    for k, v in kwargs.items():
        result = result.replace('{' + k + '}', v)
    return result



# ── Droid discovery backend functions ────────────────────────────────────────

def droid_scan() -> dict:
    """Scan for nearby Galaxy's Edge droids using BleakScanner in a subprocess."""
    venv_python = os.path.join(PROJECT_DIR, 'venv', 'bin', 'python3')
    scan_script = """
import asyncio, json
from bleak import BleakScanner

DISNEY_MFR_ID = 0x0183

async def scan():
    found = {}
    async with BleakScanner() as scanner:
        for _ in range(15):
            for addr, (dev, adv) in scanner.discovered_devices_and_advertisement_data.items():
                mfr = adv.manufacturer_data or {}
                if dev.name == "DROID" and DISNEY_MFR_ID in mfr:
                    found[dev.address.upper()] = dev.address.upper()
            await asyncio.sleep(1)
    print(json.dumps(list(found.keys())))

asyncio.run(scan())
"""
    try:
        env = os.environ.copy()
        env['XDG_RUNTIME_DIR'] = '/run/user/1000'
        result = subprocess.run(
            [venv_python, '-c', scan_script],
            capture_output=True, text=True, timeout=25, env=env
        )
        import json as _json
        macs = _json.loads(result.stdout.strip()) if result.stdout.strip() else []
        return {"ok": True, "droids": [{"mac": m} for m in macs]}
    except Exception as e:
        return {"ok": False, "error": str(e), "droids": []}


def droid_claim(mac: str) -> dict:
    """Save a droid MAC to .env as DROID_MAC, then restart the brain so it
    actually goes and connects to it. Every other action that changes what
    the brain should be doing (saving mic setup, finishing the wizard)
    already restarts -- claiming a droid is the one path that didn't, which
    is harmless on a first-time setup (a later step restarts anyway) but
    leaves the brain sitting stopped with nothing to reconnect to after a
    Reset or Start Over, since those intentionally skip straight to "claim"
    without passing through a step that would restart it."""
    if not mac:
        return {"ok": False, "error": "No MAC provided"}
    existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    existing["DROID_MAC"] = mac.upper()
    # One-shot: tells kyber_core.py to fire the Disney DroidBayActivationSequence
    # over BLE the moment it connects on this fresh restart, and clears its own
    # flag right after so it never replays on a later, unrelated restart.
    existing["PLAY_ACTIVATION_ON_NEXT_BOOT"] = "1"
    # KYBER's own confirmation chime is silenced on this restart too --
    # hearing it partway through Disney's own sequence threw off the
    # activation page's timing/pacing. Same one-shot flag /setup/finish
    # uses for the power-cycle restart; every other restart (routine
    # reconnects, Mainframe "Save & Restart") is untouched.
    existing["SKIP_STARTUP_SOUND_ONCE"] = "1"
    # A fresh claim always means the wizard's activation page should show
    # again before Ready, even if a previous claim already confirmed it.
    existing["ACTIVATION_CONFIRMED"] = ""
    with open(ENV_PATH, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
    restart_kyber_service()
    return {"ok": True, "mac": mac.upper()}


def droid_release() -> dict:
    """Clear DROID_MAC from .env."""
    existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    existing["DROID_MAC"] = ""
    with open(ENV_PATH, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
    return {"ok": True}


def droid_get_claimed() -> dict:
    """Return the currently claimed droid MAC from .env."""
    vals = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    mac = vals.get("DROID_MAC", "").upper().strip()
    return {"mac": mac if mac else None}


def droid_chirp(mac: str) -> dict:
    """Connect to a specific droid by MAC, play a chirp sound, and disconnect."""
    if not mac:
        return {"ok": False, "error": "No MAC provided"}
    venv_python = os.path.join(PROJECT_DIR, 'venv', 'bin', 'python3')
    chirp_script = f"""
import asyncio
from droiddepot.connection import DroidConnection
from droiddepot.protocol import DisneyBLEManufacturerId
from bleak import BleakScanner

TARGET_MAC = "{mac.upper()}"

async def chirp():
    # Find and connect to the specific droid
    async with BleakScanner() as scanner:
        for _ in range(10):
            devices = scanner.discovered_devices_and_advertisement_data
            for addr, (dev, adv) in devices.items():
                if dev.address.upper() == TARGET_MAC:
                    mfr = adv.manufacturer_data or {{}}
                    if DisneyBLEManufacturerId.DroidManufacturerId in mfr:
                        d = DroidConnection(dev.address, mfr)
                        await d.connect(silent=True)
                        await asyncio.sleep(0.5)
                        await d.audio_controller.play_audio(sound_id=1, bank_id=1)
                        await asyncio.sleep(2)
                        await d.disconnect(silent=True)
                        return
            await asyncio.sleep(1)
    raise RuntimeError("Droid not found")

asyncio.run(chirp())
"""
    try:
        env = os.environ.copy()
        env['XDG_RUNTIME_DIR'] = '/run/user/1000'
        result = subprocess.run(
            [venv_python, '-c', chirp_script],
            capture_output=True, text=True, timeout=20, env=env
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Chirp failed"}
        return {"ok": True}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Chirp timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Bluetooth backend functions ───────────────────────────────────────────────

import subprocess as _sp
import re as _re


def _btctl(*args, stdin_data=None, timeout=15):
    """Run bluetoothctl command(s) and return stdout+stderr."""
    try:
        if stdin_data is not None:
            # Interactive mode — pipe commands via stdin
            result = _sp.run(
                ["bluetoothctl"],
                input=stdin_data,
                capture_output=True, text=True, timeout=timeout
            )
        else:
            result = _sp.run(
                ["bluetoothctl"] + list(args),
                capture_output=True, text=True, timeout=timeout
            )
        return result.stdout + result.stderr
    except _sp.TimeoutExpired:
        return "timeout"
    except Exception as e:
        return str(e)


def bt_read_section_macs() -> dict:
    """Read BT_DROID_MAC, BT_COMLINK_MAC, BT_SENSOR_MAC from .env."""
    vals = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    return {
        "droid":  vals.get("BT_DROID_MAC",   "").upper().strip(),
        "comm":   vals.get("BT_COMLINK_MAC",  "").upper().strip(),
        "sensor": vals.get("BT_SENSOR_MAC",   "").upper().strip(),
    }


def bt_write_section_mac(section: str, mac: str):
    """Write a MAC address for a section into .env."""
    key_map = {"droid": "BT_DROID_MAC", "comm": "BT_COMLINK_MAC", "sensor": "BT_SENSOR_MAC"}
    key = key_map.get(section)
    if not key:
        return
    existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    existing[key] = mac.upper()
    with open(ENV_PATH, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")


def bt_clear_section_mac(mac: str):
    """Remove a MAC from all section keys in .env."""
    mac = mac.upper()
    existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    changed = False
    for key in ("BT_DROID_MAC", "BT_COMLINK_MAC", "BT_SENSOR_MAC"):
        if existing.get(key, "").upper() == mac:
            existing[key] = ""
            changed = True
    if changed:
        with open(ENV_PATH, "w") as f:
            for k, v in existing.items():
                f.write(f"{k}={v}\n")


def wired_mic_detected() -> bool:
    """Lightweight existence check for a capture device -- just enough to
    know a fallback mic is actually plugged in, not a full open-and-verify
    like kyber_core.py's own mic warmup does. Only meaningful to call when
    BT_COMLINK_MAC isn't set, since a paired Bluetooth mic can also show up
    here once connected -- callers check bluetooth first."""
    try:
        out = subprocess.run(['arecord', '-l'], capture_output=True, text=True, timeout=3).stdout
        return 'card' in out.lower()
    except Exception:
        return False


def bt_list_paired():
    """Return list of paired devices with name, MAC, connected status, and section."""
    section_macs = bt_read_section_macs()
    mac_to_section = {v: k for k, v in section_macs.items() if v}
    out = _btctl("devices")
    devices = []
    for line in out.splitlines():
        m = _re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip())
        if m:
            mac, name = m.group(1), m.group(2).strip()
            info = _btctl("info", mac)
            connected = "Connected: yes" in info
            audio = any(x in info for x in ["Audio", "Headset", "HandsFree", "A2DP", "HFP"])
            devices.append({
                "mac": mac,
                "name": name or mac,
                "connected": connected,
                "audio": audio,
                "section": mac_to_section.get(mac.upper(), None),
            })
    return {"devices": devices}


def bt_scan():
    """Scan for nearby BT/BLE devices, return unpaired ones."""
    import time as _time
    try:
        # One persistent bluetoothctl session fed via stdin, not separate
        # "bluetoothctl scan on" command-line invocations. BlueZ has a
        # confirmed issue (bluez/bluez#826) where "scan on" passed as a
        # standalone command-line argument just sets a discovery filter
        # and exits immediately, without ever actually starting discovery
        # -- which would explain a previously-cached device (the droid)
        # still showing up while a genuinely new device never does, since
        # the device list below comes from BlueZ's persistent cache either
        # way and doesn't by itself prove a scan actually ran.
        proc = _sp.Popen(
            ["bluetoothctl"],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True
        )
        proc.stdin.write("scan on\n")
        proc.stdin.flush()
        _time.sleep(15)
        proc.stdin.write("scan off\nquit\n")
        proc.stdin.flush()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()
    except Exception as e:
        return {"devices": [], "error": str(e)}

    # Get all known devices minus already-paired ones
    all_out = _btctl("devices")
    paired_out = _btctl("devices", "Paired")
    paired_macs = set()
    for line in paired_out.splitlines():
        m = _re.match(r"Device\s+([0-9A-F:]{17})", line.strip())
        if m:
            paired_macs.add(m.group(1))

    devices = []
    seen = set()
    for line in all_out.splitlines():
        m = _re.match(r"Device\s+([0-9A-F:]{17})\s+(.*)", line.strip())
        if m:
            mac, name = m.group(1), m.group(2).strip()
            # Droids advertise as exactly "DROID" -- they show up on the same
            # scan since it's the same radio, but they don't belong in a list
            # meant for pairing a comlink/mic. Exact match on purpose, not a
            # substring check: a substring match on "droid" would also catch
            # any device with "android" in its name.
            if name.strip().upper() == "DROID":
                continue
            if mac not in paired_macs and mac not in seen:
                seen.add(mac)
                devices.append({"mac": mac, "name": name or mac})
    return {"devices": devices}


def bt_pair(mac, pin=""):
    """Pair a device. If no PIN provided, attempt without one first.
    Returns needs_pin=True if the device requests a PIN."""
    if not mac:
        return {"ok": False, "error": "No MAC address provided"}
    try:
        commands = f"pair {mac}\n"
        if pin:
            commands += f"{pin}\n"
        commands += "yes\n"
        commands += f"trust {mac}\n"
        commands += "quit\n"
        result = _sp.run(
            ["bluetoothctl"],
            input=commands,
            capture_output=True, text=True, timeout=20
        )
        out = result.stdout + result.stderr
        # Device is requesting a PIN
        if "Enter PIN code" in out or "Request passkey" in out or "Confirm passkey" in out:
            if not pin:
                return {"ok": False, "needs_pin": True}
        if "Failed" in out or "not available" in out.lower():
            lines = [l for l in out.strip().splitlines() if l.strip()]
            return {"ok": False, "error": lines[-1] if lines else "Pairing failed"}
        return {"ok": True}
    except _sp.TimeoutExpired:
        return {"ok": False, "error": "Pairing timed out after 20 seconds"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def bt_connect(mac):
    """Connect to a paired device."""
    if not mac:
        return {"ok": False, "error": "No MAC address provided"}
    out = _btctl("connect", mac, timeout=10)
    if "Failed" in out or "not available" in out.lower():
        lines = [l for l in out.strip().splitlines() if l.strip()]
        return {"ok": False, "error": lines[-1] if lines else "Connection failed"}
    return {"ok": True}


def bt_disconnect(mac):
    """Disconnect a device."""
    if not mac:
        return {"ok": False, "error": "No MAC address provided"}
    _btctl("disconnect", mac, timeout=10)
    return {"ok": True}


def bt_forget(mac):
    """Remove/unpair a device and clear its section assignment from .env."""
    if not mac:
        return {"ok": False, "error": "No MAC address provided"}
    out = _btctl("remove", mac, timeout=10)
    if "not available" in out.lower() and "Device has been removed" not in out:
        return {"ok": False, "error": out.strip()}
    bt_clear_section_mac(mac)
    return {"ok": True}



def bt_set_mic(mac):
    """Set a BT audio device as the default mic source via WirePlumber, and
    make sure .env's MIC_DEVICE points at the 'pipewire' ALSA bridge device
    rather than the device-specific node id -- node ids aren't stable across
    reconnects/reboots, but 'pipewire' always resolves to whatever the
    current default capture device is, which this sets explicitly below
    rather than relying on WirePlumber's own auto-promotion happening to
    pick it (it often does, but isn't guaranteed on every setup)."""
    try:
        out = _sp.run(["wpctl", "status"], capture_output=True, text=True, timeout=5).stdout
        mac_match = mac.upper()
        node_id = None
        for line in out.splitlines():
            if f"BLUEZ_INPUT.{mac_match}" in line.upper():
                m = _re.search(r"(\d+)\.\s+bluez_input", line, _re.IGNORECASE)
                if m:
                    node_id = m.group(1)
                    break
        if not node_id:
            return {"ok": False, "error": "Audio source not found — is the device connected?"}
        _sp.run(["wpctl", "set-default", node_id], timeout=5)
        # Update .env -- always 'pipewire', never a node id (those don't persist)
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        lines = []
        found = False
        with open(env_path, 'r') as f:
            for line in f:
                if line.startswith("MIC_DEVICE="):
                    lines.append("MIC_DEVICE=pipewire\n")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append("MIC_DEVICE=pipewire\n")
        with open(env_path, 'w') as f:
            f.writelines(lines)
        return {"ok": True, "source": "pipewire", "node_id": node_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ConfigHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/mapper_status':
            ready = is_port_open(MAPPER_PORT)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ready":true}' if ready else b'{"ready":false}')
            return

        if self.path == '/brain_ready':
            ready = False
            if is_port_open(5002):
                try:
                    import urllib.request as _ur
                    import json as _json
                    with _ur.urlopen('http://127.0.0.1:5002/droid_status', timeout=2) as r:
                        status = _json.loads(r.read())
                    ready = status.get("connected", False)
                except Exception:
                    ready = False
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ready":true}' if ready else b'{"ready":false}')
            return

        if self.path == '/service_status':
            status = get_service_status()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            import json
            self.wfile.write(json.dumps({"status": status}).encode())
            return

        if self.path == '/hotel_status':
            import json as _json
            try:
                import urllib.request
                req = urllib.request.Request('http://127.0.0.1:5002/hotel')
                with urllib.request.urlopen(req, timeout=1) as r:
                    data = r.read()
            except Exception:
                data = b'{"active":false,"remaining_seconds":0}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == '/network_status':
            try:
                import urllib.request
                with urllib.request.urlopen('http://127.0.0.1:5003/netgw/status', timeout=2) as r:
                    data = r.read()
            except Exception:
                # kyber_netgw.service not running/reachable — distinct from any
                # of its real states, so the dashboard can say so plainly.
                data = b'{"state":"unreachable"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == '/roam_status':
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5002/roam', timeout=1) as r:
                    data = r.read()
            except Exception:
                data = b'{"active":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == '/pet_status':
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5002/pet', timeout=1) as r:
                    data = r.read()
            except Exception:
                data = b'{"active":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == '/expressive_status':
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5002/expressive', timeout=1) as r:
                    data = r.read()
            except Exception:
                data = b'{"active":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == '/controls':
            self._serve_controls()
            return

        if self.path == '/motivator':
            self._serve_motivator()
            return

        if self.path == '/bluetooth':
            self._serve_bluetooth()
            return

        if self.path == '/calibration':
            self._serve_calibration()
            return

        if self.path == '/reset':
            self._serve_reset()
            return

        if self.path == '/calibration_current':
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5002/calibration_status', timeout=3) as r:
                    data = r.read()
            except Exception:
                data = b'{"left_scale":1.0,"right_scale":1.0,"base_spin_duration":0.9}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == '/droid_claimed':
            self._serve_json(droid_get_claimed())
            return

        if self.path == '/wizard_droid_status':
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5002/droid_status', timeout=3) as r:
                    data = r.read()
            except Exception:
                data = b'{"connected": false, "waiting": false, "droid_mac": null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path.startswith('/droid_chirp'):
            # Extract MAC from query string: /droid_chirp?mac=XX:XX:XX:XX:XX:XX
            from urllib.parse import urlparse, parse_qs as _pqs
            qs = _pqs(urlparse(self.path).query)
            mac = qs.get('mac', [''])[0]
            self._serve_json(droid_chirp(mac))
            return

        if self.path.startswith('/droid_scan'):
            self._serve_json(droid_scan())
            return

        if self.path == '/network_list':
            # Sourced from kyber_netgw.py's real NetworkManager-backed state
            # now, not wpa_supplicant.conf -- kyber_netgw.py has owned the
            # actual active connection via nmcli for a while, and that file
            # was never being updated to match, so this was quietly showing
            # stale/empty data regardless of what was really connected.
            # /netgw/known_networks is read fresh every call (no scan
            # involved), unlike /netgw/networks (the wifi-scan cache, which
            # only refreshes on an AP-mode transition) -- Forget/Connect
            # need to show up immediately, not whenever that next happens.
            import json as _json
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5003/netgw/known_networks', timeout=3) as r:
                    known_ssids = _json.loads(r.read()).get("networks", [])
                with _ur.urlopen('http://127.0.0.1:5003/netgw/status', timeout=3) as r:
                    status = _json.loads(r.read())
                target = status.get("target_ssid")
                online = status.get("state") == "client_online"
                networks = [
                    {"ssid": ssid, "connected": online and ssid == target}
                    for ssid in known_ssids
                ]
            except Exception:
                networks = []
            self._serve_json({"networks": networks})
            return

        if self.path == '/network_scan_cache':
            # For the Add Network picker specifically -- deliberately the
            # last-known scan cache (/netgw/networks), not a fresh live scan.
            # Every scan in kyber_netgw.py happens only during an AP-mode
            # transition, since scanning has the same single-radio conflict
            # as running AP+STA together -- there's no proven-safe way to
            # scan on demand while already connected and online, so this
            # shows what's already known rather than risk the connection.
            # "Rescan for networks" (a deliberate, confirmed, disruptive
            # action) is what actually forces a fresh one.
            import json as _json
            import urllib.request as _ur
            try:
                with _ur.urlopen('http://127.0.0.1:5003/netgw/networks', timeout=3) as r:
                    networks = _json.loads(r.read()).get("networks", [])
            except Exception:
                networks = []
            self._serve_json({"networks": networks})
            return

        if self.path == '/bluetooth_paired':
            self._serve_json(bt_list_paired())
            return

        if self.path.startswith('/bluetooth_scan'):
            self._serve_json(bt_scan())
            return

        if self.path == '/protocols':
            self._serve_protocols()
            return

        if self.path.startswith('/edit_personality'):
            from urllib.parse import urlparse, parse_qs as _pqs
            qs = _pqs(urlparse(self.path).query)
            slot = qs.get('slot', ['1'])[0]
            self._serve_edit_personality(slot)
            return

        from urllib.parse import urlparse, parse_qs as _pqs
        qs = _pqs(urlparse(self.path).query)
        restarting = qs.get('restarting', ['0'])[0] == '1'
        wizard_path = qs.get('path', [''])[0]
        edit_stage = qs.get('edit', [''])[0]
        self._serve_page(restart_overlay=restarting, wizard_path=wizard_path, edit_stage=edit_stage)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length).decode()
        params = parse_qs(body)

        if self.path == '/droid_chirp':
            import json as _json
            import urllib.request as _ur
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            try:
                req = _ur.Request(
                    'http://127.0.0.1:5002/chirp',
                    data=_json.dumps({'mac': mac}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=15) as r:
                    result = _json.loads(r.read())
                self._serve_json(result)
            except Exception as e:
                self._serve_json({'ok': False, 'reason': str(e)})
            return

        if self.path == '/droid_claim':
            import json as _json
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            self._serve_json(droid_claim(mac))
            return

        if self.path == '/droid_release':
            self._serve_json(droid_release())
            return

        if self.path == '/network_add':
            import json as _json
            import urllib.request as _ur
            data = _json.loads(body) if body else {}
            ssid = data.get('ssid', '')
            psk = data.get('psk', '')
            assume_known = bool(data.get('assume_known'))
            try:
                req = _ur.Request(
                    'http://127.0.0.1:5003/netgw/credentials',
                    data=_json.dumps({'ssid': ssid, 'password': psk, 'assume_known': assume_known}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=10) as r:
                    result = _json.loads(r.read())
                self._serve_json(result)
            except Exception as e:
                self._serve_json({'ok': False, 'error': str(e)})
            return

        if self.path == '/network_forget':
            import json as _json
            import urllib.request as _ur
            data = _json.loads(body) if body else {}
            ssid = data.get('ssid', '')
            try:
                req = _ur.Request(
                    'http://127.0.0.1:5003/netgw/forget',
                    data=_json.dumps({'ssid': ssid}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=10) as r:
                    result = _json.loads(r.read())
                self._serve_json(result)
            except Exception as e:
                self._serve_json({'ok': False, 'error': str(e)})
            return

        if self.path == '/bluetooth_pair':
            import json as _json
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            pin = data.get('pin', '')
            section = data.get('section', '')
            result = bt_pair(mac, pin)
            if result.get('ok') and section:
                bt_write_section_mac(section, mac)
            self._serve_json(result)
            return

        if self.path == '/bluetooth_connect':
            import json as _json
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            section = data.get('section', '')
            result = bt_connect(mac)
            if result.get('ok') and section:
                bt_write_section_mac(section, mac)
            self._serve_json(result)
            return

        if self.path == '/bluetooth_connect_mic':
            import json as _json
            import time as _time
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            result = bt_connect(mac)
            if result.get('ok'):
                _time.sleep(2)  # Give PipeWire time to register the audio source
                mic_result = bt_set_mic(mac)
                if not mic_result.get('ok'):
                    result = {'ok': True, 'warn': mic_result.get('error', 'Mic set failed')}
            self._serve_json(result)
            return

        if self.path == '/bluetooth_disconnect':
            import json as _json
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            result = bt_disconnect(mac)
            self._serve_json(result)
            return

        if self.path == '/bluetooth_forget':
            import json as _json
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            result = bt_forget(mac)
            self._serve_json(result)
            return

        if self.path == '/bluetooth_set_mic':
            import json as _json
            data = _json.loads(body) if body else {}
            mac = data.get('mac', '')
            result = bt_set_mic(mac)
            self._serve_json(result)
            return

        if self.path == '/calibration_probe':
            # Unlike /motor_command, this is NOT fire-and-forget — the wizard needs
            # to know the physical spin actually finished before asking the next
            # question, so this blocks on the proxied call all the way through.
            import json as _json
            import urllib.request as _ur
            import urllib.error as _ue
            data = _json.loads(body) if body else {}
            try:
                req = _ur.Request(
                    'http://127.0.0.1:5002/calibration_probe',
                    data=_json.dumps(data).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=15) as r:
                    result = _json.loads(r.read())
                self._serve_json(result)
            except _ue.HTTPError as e:
                # urllib raises on any non-2xx status instead of returning it, so
                # the actual {"ok": false, "reason": "..."} body kyber_core sent
                # back is sitting unread inside this exception. str(e) alone only
                # gives the generic "HTTP Error 500: Internal Server Error" —
                # read the real body and surface kyber_core's actual reason.
                try:
                    detail = _json.loads(e.read())
                    reason = detail.get('reason', str(e))
                except Exception:
                    reason = str(e)
                self._serve_json({'ok': False, 'reason': reason})
            except Exception as e:
                self._serve_json({'ok': False, 'reason': str(e)})
            return

        if self.path == '/calibration_victory':
            import json as _json
            import urllib.request as _ur
            import urllib.error as _ue
            try:
                req = _ur.Request(
                    'http://127.0.0.1:5002/calibration_victory',
                    data=b'{}',
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=15) as r:
                    result = _json.loads(r.read())
                self._serve_json(result)
            except _ue.HTTPError as e:
                try:
                    detail = _json.loads(e.read())
                    reason = detail.get('reason', str(e))
                except Exception:
                    reason = str(e)
                self._serve_json({'ok': False, 'reason': reason})
            except Exception as e:
                self._serve_json({'ok': False, 'reason': str(e)})
            return

        if self.path == '/calibration_lock':
            import json as _json
            data = _json.loads(body) if body else {}
            try:
                left  = float(data.get('left_scale', 1.0))
                right = float(data.get('right_scale', 1.0))
            except (TypeError, ValueError):
                self._serve_json({'ok': False, 'reason': 'invalid scale values'})
                return
            existing_env = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
            existing_env["CALIBRATION_LEFT_SCALE"]  = f"{left:.4f}"
            existing_env["CALIBRATION_RIGHT_SCALE"] = f"{right:.4f}"
            with open(ENV_PATH, 'w') as f:
                for k, v in existing_env.items():
                    f.write(f"{k}={v}\n")
            # .env above is for persistence across a real restart/reboot later.
            # This separately pushes the same values straight into the already-
            # running brain process's memory, so they take effect immediately —
            # no restart needed for the common case. If the brain can't be
            # reached, the values are still safely stored in .env and will be
            # picked up normally the next time it does restart for any reason.
            import urllib.request as _ur
            live_applied = False
            try:
                req = _ur.Request(
                    'http://127.0.0.1:5002/calibration_set',
                    data=_json.dumps({'left_scale': left, 'right_scale': right}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                with _ur.urlopen(req, timeout=5) as r:
                    result = _json.loads(r.read())
                live_applied = bool(result.get('ok'))
            except Exception as e:
                print(f"[CALIBRATION] WARNING: could not push live update to brain: {e}", flush=True)
            self._serve_json({'ok': True, 'left_scale': left, 'right_scale': right, 'live_applied': live_applied})
            return

        if self.path == '/motor_command':
            import urllib.request as _ur
            import threading as _threading
            data = body.encode() if body else b'{}'
            def _do_cmd():
                try:
                    req = _ur.Request(
                        'http://127.0.0.1:5002/motor',
                        data=data,
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    _ur.urlopen(req, timeout=5)
                except Exception:
                    pass
            _threading.Thread(target=_do_cmd, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/motor_test':
            import urllib.request as _ur
            import threading as _threading
            data = body.encode() if body else b'{}'
            def _do_test():
                try:
                    req = _ur.Request(
                        'http://127.0.0.1:5002/motor_test',
                        data=data,
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    _ur.urlopen(req, timeout=5)
                except Exception:
                    pass
            _threading.Thread(target=_do_test, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/roam_toggle':
            import urllib.request as _ur
            import threading as _threading
            data = body.encode() if body else b'{}'
            def _do_roam():
                try:
                    req = _ur.Request('http://127.0.0.1:5002/roam', data=data,
                        headers={'Content-Type': 'application/json'}, method='POST')
                    _ur.urlopen(req, timeout=3)
                except Exception:
                    pass
            _threading.Thread(target=_do_roam, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/pet_toggle':
            import urllib.request as _ur
            import threading as _threading
            data = body.encode() if body else b'{}'
            def _do_pet():
                try:
                    req = _ur.Request('http://127.0.0.1:5002/pet', data=data,
                        headers={'Content-Type': 'application/json'}, method='POST')
                    _ur.urlopen(req, timeout=3)
                except Exception:
                    pass
            _threading.Thread(target=_do_pet, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/expressive_toggle':
            import urllib.request as _ur
            import threading as _threading
            data = body.encode() if body else b'{}'
            def _do_toggle():
                try:
                    req = _ur.Request(
                        'http://127.0.0.1:5002/expressive',
                        data=data,
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    _ur.urlopen(req, timeout=3)
                except Exception:
                    pass
            _threading.Thread(target=_do_toggle, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/network_reconnect':
            import urllib.request
            import threading as _threading
            def _do_reconnect():
                try:
                    req = urllib.request.Request(
                        'http://127.0.0.1:5003/netgw/revert',
                        data=b'{}',
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    urllib.request.urlopen(req, timeout=3)
                except Exception:
                    pass
            _threading.Thread(target=_do_reconnect, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/network_rescan':
            # Same underlying revert_to_ap() as /network_reconnect above,
            # but with delete_profile=False -- this is a deliberate "let me
            # look around" action, not failure recovery, so the current
            # network's saved password shouldn't be wiped as a side effect.
            # Rejoining the same network afterward should just reconnect,
            # not demand the password again.
            import urllib.request
            import threading as _threading
            def _do_rescan():
                try:
                    req = urllib.request.Request(
                        'http://127.0.0.1:5003/netgw/revert',
                        data=b'{"delete_profile": false}',
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    urllib.request.urlopen(req, timeout=3)
                except Exception:
                    pass
            _threading.Thread(target=_do_rescan, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/hotel_toggle':
            import urllib.request
            import json as _json
            import threading as _threading
            data = body.encode() if body else b'{}'
            # Fire the brain API call in background — don't block the response
            def _do_toggle():
                try:
                    req = urllib.request.Request(
                        'http://127.0.0.1:5002/hotel',
                        data=data,
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    urllib.request.urlopen(req, timeout=3)
                except Exception:
                    pass
            _threading.Thread(target=_do_toggle, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == '/save_personality':
            import json as _json
            data = _json.loads(body) if body else {}
            name = (data.get('name') or '').strip()
            traits_in = data.get('traits', {})
            target_slot = data.get('target_slot')
            source_slot = str(data.get('source_slot', '1'))

            # target_slot is only present when forking off a locked default —
            # otherwise save in place to whichever slot was being edited.
            save_slot = str(target_slot) if target_slot else source_slot
            if not save_slot.isdigit():
                # Safety net — the UI never sends a non-digit target, but this
                # guarantees a write can never land on a locked default file.
                self._serve_json({"ok": False, "reason": "invalid target slot"})
                return

            map_path = _personality_map_path(save_slot)
            os.makedirs(MAP_DIR, exist_ok=True)
            existing = {}
            if os.path.exists(map_path):
                try:
                    with open(map_path) as f:
                        existing = _json.load(f)
                except Exception:
                    pass
            existing["name"] = name or f"Personality Profile {save_slot}"
            existing["traits"] = {
                "brave":     int(traits_in.get("brave", 3)),
                "curious":   int(traits_in.get("curious", 3)),
                "sassy":     int(traits_in.get("sassy", 3)),
                "playful":   int(traits_in.get("playful", 3)),
                "sensitive": int(traits_in.get("sensitive", 3)),
            }
            with open(map_path, 'w') as f:
                _json.dump(existing, f, indent=2)

            # Editing a personality establishes core droid behavior — restart
            # immediately rather than deferring, same reasoning as Calibration's
            # lock-in being the one place that does NOT defer.
            existing_env = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
            existing_env["ACTIVE_PERSONALITY"] = save_slot
            with open(ENV_PATH, 'w') as f:
                for k, v in existing_env.items():
                    f.write(f'{k}={v}\n')
            restart_kyber_service()

            self._serve_json({"ok": True, "slot": save_slot})
            return

        if self.path == '/open_mapper':
            target_slot = params.get("mapper_slot", ["1"])[0].strip()
            # Write mapper slot as a separate var — never touch ACTIVE_SOUND_PROFILE
            existing_env = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
            existing_env["MAPPER_SOUND_PROFILE"] = target_slot
            with open(ENV_PATH, 'w') as f:
                for k, v in existing_env.items():
                    f.write(f'{k}={v}\n')
            if is_port_open(MAPPER_PORT):
                # A mapper instance is already running — most likely a stale one
                # that never cleanly exited (e.g. a closed browser tab without
                # hitting its own shutdown route). Spawning a second one on top
                # is what let mapper mode's on/off flag get confused: two
                # independent processes both flipping the same shared flag on
                # the brain, with no coordination between them, and whichever
                # one's shutdown happened to land last won — even if that was
                # the stale one, well after the new session had already started.
                # Shutting the old one down cleanly first removes that overlap
                # window entirely.
                import urllib.request as _ur
                try:
                    req = _ur.Request(f'http://127.0.0.1:{MAPPER_PORT}/api/shutdown', data=b'{}', method='POST')
                    _ur.urlopen(req, timeout=3)
                except Exception:
                    pass
                for _ in range(20):
                    if not is_port_open(MAPPER_PORT):
                        break
                    time.sleep(0.25)
            start_sound_mapper()
            html = safe_render(LOADING_HTML, r2_svg=R2_SVG)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            return

        if self.path == '/setup/save_keys':
            # Deliberately its own handler rather than reusing /save below —
            # that one always writes ACTIVE_PERSONALITY/DROID_TYPE/etc. with
            # fallback defaults even when the submitted form doesn't include
            # them, which is correct for the full Settings page but would
            # silently stomp the model choice on a returning user mid-wizard.
            # This one only ever touches the key/provider fields.
            key_fields = {
                "DEEPGRAM_API_KEY":     params.get("DEEPGRAM_API_KEY",     [""])[0].strip(),
                "GROQ_API_KEY":         params.get("GROQ_API_KEY",         [""])[0].strip(),
                "OPENAI_API_KEY":       params.get("OPENAI_API_KEY",       [""])[0].strip(),
                "GOOGLE_STT_API_KEY":   params.get("GOOGLE_STT_API_KEY",   [""])[0].strip(),
                "ASSEMBLYAI_API_KEY":   params.get("ASSEMBLYAI_API_KEY",   [""])[0].strip(),
                "GEMINI_API_KEY":       params.get("GEMINI_API_KEY",       [""])[0].strip(),
                "ANTHROPIC_API_KEY":    params.get("ANTHROPIC_API_KEY",    [""])[0].strip(),
                "STT_PROVIDER":         params.get("STT_PROVIDER",         ["deepgram"])[0].strip(),
                "LLM_PROVIDER":         params.get("LLM_PROVIDER",         ["gemini"])[0].strip(),
            }
            write_env(key_fields)
            wizard_path = params.get("path", [""])[0]
            if not is_setup_complete():
                # Whatever was submitted still doesn't satisfy at least one
                # STT key and one LLM key — show the keys step again with
                # an alert instead of silently advancing.
                vals = read_env()
                html = safe_render(
                    WIZARD_KEYS_HTML,
                    r2_svg=R2_SVG,
                    alert_html='<div class="alert error">Need at least one Speech-to-Text key and one AI Personality key before continuing.</div>',
                    logic_core_panel=render_logic_core_panel(vals),
                    path=wizard_path,
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())
                return
            # Keys is satisfied -- let the normal dispatch figure out the
            # actual next stage rather than hardcoding one. Whatever stage
            # comes after Keys in the chain is wherever this should land,
            # not a fixed assumption that could go stale on a future reorder
            # the same way this exact handler just did.
            self._serve_page(wizard_path=wizard_path)
            return

        if self.path == '/setup/start_over':
            # A true full reset except API keys -- those are a pain to lose
            # (Deepgram in particular won't reissue a deleted key), everything
            # else about the setup is cheap to redo. Backed up first, same
            # safety net as every other destructive action this session.
            import shutil as _shutil
            import time as _time
            if os.path.exists(ENV_PATH):
                backup_path = ENV_PATH + f".bak.{int(_time.time())}"
                _shutil.copy(ENV_PATH, backup_path)
            vals = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
            # Ask bluetoothctl directly for everything currently paired rather
            # than reading MACs from .env -- .env may already have had them
            # cleared by a previous reset, leaving orphaned OS-level pairings
            # behind that nothing will ever clean up. This is the only approach
            # that's actually idempotent regardless of what .env says.
            _paired_out = _btctl("devices", "Paired")
            import re as _re_bt
            for _line in _paired_out.splitlines():
                _m = _re_bt.match(r"Device\s+([0-9A-Fa-f:]{17})", _line.strip())
                if _m:
                    _btctl("remove", _m.group(1), timeout=10)
            preserved = {
                k: vals.get(k, "") for k in (
                    "DEEPGRAM_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY",
                    "GOOGLE_STT_API_KEY", "ASSEMBLYAI_API_KEY",
                    "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
                    "STT_PROVIDER", "LLM_PROVIDER",
                ) if vals.get(k)
            }
            with open(ENV_PATH, 'w') as f:
                for k, v in preserved.items():
                    f.write(f"{k}={v}\n")
            stop_kyber_service()
            self._serve_page()
            return

        if self.path == '/reset/confirm':
            # Resets just the droid-specific identity -- pairing, model,
            # personality/calibration assignment -- while keeping everything
            # that isn't tied to which physical droid is connected (API
            # keys, STT/LLM provider, mic setup, Bluetooth accessories).
            # Sound profile isn't wiped either, just pointed back at
            # profile 1 -- bank/sound counts differ per chassis (see
            # pyDroidDepot's ChipAudioCount), so carrying over an arbitrary
            # previous profile could reference sounds that don't exist on
            # the new droid. Saved personality/sound-profile JSON files on
            # disk are untouched either way -- only the *assignment*
            # resets, so old presets stay pickable later ("transplant the
            # brain into another body"). Backed up first, same safety net
            # as /setup/start_over above.
            import shutil as _shutil
            import time as _time
            if os.path.exists(ENV_PATH):
                backup_path = ENV_PATH + f".bak.{int(_time.time())}"
                _shutil.copy(ENV_PATH, backup_path)
            vals = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
            for key in (
                "DROID_MAC", "DROID_NAME", "DROID_TYPE", "BT_DROID_MAC",
                "MODEL_CONFIRMED", "ACTIVE_PERSONALITY", "PERSONALITY_CONFIRMED",
                "CALIBRATION_LEFT_SCALE", "CALIBRATION_RIGHT_SCALE",
                "ONBOARDING_COMPLETE",
            ):
                vals.pop(key, None)
            vals["ACTIVE_SOUND_PROFILE"] = "1"
            vals["MAPPER_SOUND_PROFILE"] = "1"
            with open(ENV_PATH, 'w') as f:
                for k, v in vals.items():
                    f.write(f"{k}={v}\n")
            stop_kyber_service()
            self._serve_page()
            return

        if self.path == '/setup/save_identity':
            # Combined Model + Name + Personality page -- one submit writes
            # all three. The "selected" value already reflects the right
            # thing in every case: untouched default/slot -> the dropdown's
            # own value; just forked or edited in place -> /save_personality
            # already wrote ACTIVE_PERSONALITY moments ago and the page
            # updated this same hidden field to match.
            model = params.get("model", [""])[0].strip()
            selected_personality = params.get("selected", [""])[0].strip()
            wizard_path = params.get("path", [""])[0]

            updates = {}
            if model in WIZARD_MODEL_CHASSIS:
                updates["DROID_TYPE"] = WIZARD_MODEL_CHASSIS[model]
                updates["MODEL_CONFIRMED"] = "1"
            if selected_personality:
                updates["ACTIVE_PERSONALITY"] = selected_personality
                updates["PERSONALITY_CONFIRMED"] = "1"
            write_env(updates)

            # DROID_NAME is optional -- handled separately from write_env's
            # generic update since write_env skips falsy values, but clearing
            # the name (or simply never entering one) is valid and must
            # still take effect, same reasoning as the Mainframe's own
            # Droid Designation field.
            droid_name_clean = sanitize_droid_name(params.get("DROID_NAME", [""])[0])
            existing_env = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
            existing_env["DROID_NAME"] = droid_name_clean
            with open(ENV_PATH, "w") as f:
                for k, v in existing_env.items():
                    f.write(f"{k}={v}\n")

            self._serve_page(wizard_path=wizard_path)
            return

        if self.path == '/setup/save_mic':
            skipped = params.get("skipped", ["0"])[0] == "1"
            wizard_path = params.get("path", [""])[0]
            if skipped:
                write_env({"MIC_SETUP_SKIPPED": "1"})
            restart_kyber_service()
            self._serve_page(restart_overlay=True, wizard_path=wizard_path)
            return

        if self.path == '/setup/finish':
            # ONBOARDING_COMPLETE is deliberately NOT set here -- _serve_page()
            # picks its stage fresh on every render, and if this flag flipped
            # immediately, the very next render (showing this same overlay)
            # would already resolve to "complete" and draw the Mainframe
            # underneath instead of the Ready page. It gets set by
            # /setup/confirm_complete, once the overlay's own reconnect poll
            # actually confirms the droid is back, right before it reloads.
            #
            # SKIP_STARTUP_SOUND_ONCE: by this point the Ready page's own
            # status checks already confirmed the droid connects -- there's
            # nothing left to confirm audibly, so this one restart's
            # confirmation chime is silenced. Every other restart (routine
            # reconnects, the claim moment, Mainframe "Save & Restart") is
            # untouched by this and keeps the chime as its only "I'm back"
            # signal.
            write_env({"SKIP_STARTUP_SOUND_ONCE": "1"})
            restart_kyber_service()
            self._serve_page(restart_overlay=True)
            return

        if self.path == '/setup/confirm_activation':
            write_env({"ACTIVATION_CONFIRMED": "1"})
            self._serve_json({"ok": True})
            return

        if self.path == '/setup/confirm_complete':
            write_env({"ONBOARDING_COMPLETE": "1"})
            self._serve_json({"ok": True})
            return

        keys = {
            "DEEPGRAM_API_KEY":     params.get("DEEPGRAM_API_KEY",     [""])[0].strip(),
            "GROQ_API_KEY":         params.get("GROQ_API_KEY",         [""])[0].strip(),
            "OPENAI_API_KEY":       params.get("OPENAI_API_KEY",       [""])[0].strip(),
            "GOOGLE_STT_API_KEY":   params.get("GOOGLE_STT_API_KEY",   [""])[0].strip(),
            "ASSEMBLYAI_API_KEY":   params.get("ASSEMBLYAI_API_KEY",   [""])[0].strip(),
            "GEMINI_API_KEY":       params.get("GEMINI_API_KEY",       [""])[0].strip(),
            "ANTHROPIC_API_KEY":    params.get("ANTHROPIC_API_KEY",    [""])[0].strip(),
            "STT_PROVIDER":         params.get("STT_PROVIDER",         ["deepgram"])[0].strip(),
            "LLM_PROVIDER":         params.get("LLM_PROVIDER",         ["gemini"])[0].strip(),
            "ACTIVE_PERSONALITY":   params.get("ACTIVE_PERSONALITY",   ["1"])[0].strip(),
            "ACTIVE_SOUND_PROFILE": params.get("ACTIVE_SOUND_PROFILE", ["1"])[0].strip(),
            "DROID_TYPE":           params.get("DROID_TYPE",           ["R"])[0].strip(),
        }
        action = params.get("action", ["save"])[0]

        write_env(keys)

        # DROID_NAME is handled separately from write_env's generic update,
        # since write_env skips falsy values — but clearing the name to blank
        # is a valid, intentional action that must actually take effect.
        droid_name_clean = sanitize_droid_name(params.get("DROID_NAME", [""])[0])
        existing_env = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
        existing_env["DROID_NAME"] = droid_name_clean
        with open(ENV_PATH, "w") as f:
            for k, v in existing_env.items():
                f.write(f"{k}={v}\n")

        # Personality and Sound Profile names are no longer set from this form —
        # naming happens via the save dialog on each one's own editor page.

        if action == "save_restart":
            restart_kyber_service()
            self._serve_page(restart_overlay=True)
            return
        else:
            self._serve_page(alert=("success", "Settings saved successfully."))

    def _serve_page(self, alert=None, restart_overlay=False, wizard_path="", edit_stage=""):
        alert_html = ""
        if alert:
            kind, msg = alert
            alert_html = f'<div class="alert {kind}">{msg}</div>'

        natural_stage = "complete" if onboarding_complete() else current_setup_stage()

        # Back-navigation: an ?edit=<stage> link only takes effect while
        # still mid-onboarding, and only for a stage actually safe to
        # revisit right now -- never lets you jump ahead of real progress,
        # and never past Identity once a droid's been claimed (see
        # wizard_reached_idx).
        stage = natural_stage
        if (
            edit_stage
            and natural_stage in WIZARD_BREADCRUMB_STAGES + ["activation"]
            and edit_stage in WIZARD_BREADCRUMB_STAGES
        ):
            reached_idx = wizard_reached_idx(natural_stage)
            if WIZARD_BREADCRUMB_STAGES.index(edit_stage) <= reached_idx:
                stage = edit_stage

        if stage == "welcome" and not wizard_path:
            stop_kyber_service()
            html = safe_render(WIZARD_WELCOME_HTML, r2_svg=R2_SVG)

        elif stage == "welcome":
            # Path chosen on the previous screen (or revisiting via
            # breadcrumb/Start Over) -- combined Model + Name + Personality.
            stop_kyber_service()
            import json as _json
            vals = read_env()
            chassis = vals.get("DROID_TYPE", "R")
            if vals.get("PERSONALITY_CONFIRMED") and vals.get("ACTIVE_PERSONALITY"):
                preselect = vals.get("ACTIVE_PERSONALITY")
            else:
                preselect = CHASSIS_DEFAULT_PERSONALITY.get(chassis, "neutral")
            reverse_chassis = {v: k for k, v in WIZARD_MODEL_CHASSIS.items()}
            preselect_model = reverse_chassis.get(chassis, "r2d2") if vals.get("MODEL_CONFIRMED") else "r2d2"
            custom_ids = ["1", "2", "3", "4", "5"]
            default_ids = ["r2d2", "bb8", "chopper", "bd1", "neutral"]
            profiles = {}
            occupied = {}
            for pid in custom_ids:
                profiles[pid] = {"name": read_personality_name(pid), "traits": read_personality_traits(pid), "locked": False}
                if os.path.exists(_personality_map_path(pid)):
                    occupied[pid] = read_personality_name(pid)
            for pid in default_ids:
                profiles[pid] = {"name": read_personality_name(pid), "traits": read_personality_traits(pid), "locked": True}
            custom_options = "".join(
                f'<option value="{pid}" {"selected" if pid == preselect else ""}>{profiles[pid]["name"]}</option>'
                for pid in custom_ids
            )
            default_options = "".join(
                f'<option value="{pid}" {"selected" if pid == preselect else ""}>{profiles[pid]["name"]}</option>'
                for pid in default_ids
            )
            html = safe_render(
                WIZARD_IDENTITY_HTML,
                r2_svg=R2_SVG, path=wizard_path or "yes",
                breadcrumbs=render_wizard_breadcrumbs("welcome", natural_stage, wizard_path),
                preselect_model=preselect_model,
                preselect=preselect,
                custom_options=custom_options,
                default_options=default_options,
                profiles_json=_json.dumps(profiles),
                occupied_json=_json.dumps(occupied),
                DROID_NAME_RAW=vals.get("DROID_NAME", ""),
                icon_r2d2=MODEL_ICON_R2D2, icon_bb8=MODEL_ICON_BB8,
                icon_chopper=MODEL_ICON_CHOPPER, icon_bd1=MODEL_ICON_BD1,
                icon_aseries=MODEL_ICON_ASERIES,
            )

        elif stage == "keys":
            stop_kyber_service()
            vals = read_env()
            html = safe_render(
                WIZARD_KEYS_HTML,
                r2_svg=R2_SVG,
                alert_html=alert_html,
                logic_core_panel=render_logic_core_panel(vals),
                path=wizard_path or "yes",
                breadcrumbs=render_wizard_breadcrumbs("keys", natural_stage, wizard_path),
            )

        elif stage == "wifi":
            stop_kyber_service()
            soft_note = (
                '<p class="wizard-note">No rush — this\'ll be right here whenever you\'re ready.</p>'
                if wizard_path == "no" else ""
            )
            html = safe_render(WIZARD_WIFI_HTML, r2_svg=R2_SVG, path=wizard_path or "yes", soft_note=soft_note)

        elif stage == "claim":
            stop_kyber_service()
            html = safe_render(
                WIZARD_CLAIM_HTML, r2_svg=R2_SVG, path=wizard_path or "yes",
                breadcrumbs=render_wizard_breadcrumbs("claim", natural_stage, wizard_path),
            )

        elif stage == "mic":
            stop_kyber_service()
            vals = read_env()
            connected_mac = vals.get("BT_COMLINK_MAC", "").strip()
            connected_name = "None connected yet"
            connected_mac_html = ""
            if connected_mac:
                paired = bt_list_paired().get("devices", [])
                match = next((d for d in paired if d["mac"].upper() == connected_mac.upper()), None)
                connected_name = match["name"] if match else connected_mac
                connected_mac_html = f'<div class="connected-status-mac">{connected_mac}</div>'

            if connected_mac:
                mic_action_html = (
                    '<div class="btn-row">'
                    '<button type="button" class="btn-go" onclick="skipMic()">Use current connected device.</button>'
                    '</div>'
                )
            elif wired_mic_detected():
                mic_action_html = (
                    '<div class="btn-row">'
                    '<button type="button" class="secondary" onclick="skipMic()">Use Wired Mic</button>'
                    '<button type="button" class="btn-go" onclick="skipMic()">Continue</button>'
                    '</div>'
                    '<p class="wizard-note">A wired microphone was detected on this Pi.</p>'
                )
            else:
                mic_action_html = (
                    '<div class="mic-blocked-note">No microphone detected. KYBER needs a microphone '
                    'to work \u2014 connect a Bluetooth device above, or plug in a USB/wired mic and recheck.</div>'
                    '<div class="btn-row"><button type="button" class="secondary" onclick="recheckMic()">Recheck</button></div>'
                )

            html = safe_render(
                WIZARD_MIC_HTML, r2_svg=R2_SVG, path=wizard_path or "yes",
                breadcrumbs=render_wizard_breadcrumbs("mic", natural_stage, wizard_path),
                connected_name=connected_name,
                connected_mac_html=connected_mac_html,
                connected_dot_class=(" on" if connected_mac else ""),
                mic_action_html=mic_action_html,
            )

        elif stage == "activation":
            # Presentational, unlabeled -- no breadcrumbs. Real BLE trigger
            # and reconnect already happened/are happening on the Pi side;
            # this page just paces itself against real readiness signals.
            vals = read_env()
            html = safe_render(
                WIZARD_ACTIVATION_HTML,
                r2_svg=R2_SVG,
                DROID_NAME=display_droid_name(vals.get("DROID_NAME", "")),
            )

        elif stage == "ready":
            vals = read_env()
            mic_status_text = (
                "Microphone ready (communication device)" if vals.get("BT_COMLINK_MAC")
                else "Microphone ready (using existing mic)"
            )
            overlay_js = (
                "document.getElementById('restartOverlay').classList.add('show');"
                "history.replaceState(null, '', location.pathname);"
            ) if restart_overlay else ""
            html = safe_render(
                WIZARD_READY_HTML, r2_svg=R2_SVG, mic_status_text=mic_status_text,
                breadcrumbs=render_wizard_breadcrumbs("ready", natural_stage, wizard_path),
                restart_overlay_js=overlay_js,
            )

        else:
            vals   = read_env()
            status = get_service_status()
            status_class = {"active": "active", "inactive": "inactive", "failed": "inactive"}.get(status, "unknown")
            status_text  = {"active": "Online", "inactive": "Offline", "failed": "Offline"}.get(status, status)
            overlay_js = (
                "document.getElementById('restartOverlay').classList.add('show');"
                "history.replaceState(null, '', location.pathname);"
            ) if restart_overlay else ""
            html = safe_render(
                MAIN_HTML,
                r2_svg=R2_SVG,
                nav=render_nav("mainframe"),
                status_class=status_class,
                status_text=status_text,
                alert_html=alert_html,
                logic_core_panel=render_logic_core_panel(vals),
                DROID_NAME=display_droid_name(vals["DROID_NAME"]),
                DROID_NAME_RAW=vals["DROID_NAME"],
                personality_options=personality_options(vals["ACTIVE_PERSONALITY"]),
                sound_profile_options=sound_profile_options(vals["ACTIVE_SOUND_PROFILE"]),
                droid_type_options=droid_type_options(vals["DROID_TYPE"]),
                restart_overlay_js=overlay_js,
            )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_protocols(self):
        vals = read_env()
        droid_name = display_droid_name(vals["DROID_NAME"])
        # Read directly rather than through read_env()'s allowlist -- that
        # allowlist has already silently dropped keys before (the DROID_MAC
        # bug), and this flag doesn't need to go through it at all.
        roam_enabled = dotenv_values(ENV_PATH).get("ROAM_MODE_ENABLED", "false").strip().lower() == "true"
        roam_card_html = ROAM_CARD_HTML.replace("{DROID_NAME}", droid_name) if roam_enabled else ""
        html = safe_render(PROTOCOLS_HTML, r2_svg=R2_SVG, nav=render_nav("protocols"), DROID_NAME=droid_name, roam_card_html=roam_card_html)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_controls(self):
        vals = read_env()
        html = safe_render(CONTROLS_HTML, r2_svg=R2_SVG, nav=render_nav("controls"), DROID_NAME=display_droid_name(vals["DROID_NAME"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_motivator(self):
        # Deliberately not in NAV_TABS -- direct-URL only, per Kalvin: if the
        # community dials in real C/A/BD chassis multipliers and finds his
        # provisional numbers off, they can come here to test and tell him
        # what to fix, without this being a page every user stumbles into.
        vals = read_env()
        html = safe_render(MOTIVATOR_HTML, r2_svg=R2_SVG, nav=render_nav("motivator"), DROID_NAME=display_droid_name(vals["DROID_NAME"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_bluetooth(self):
        current_type = read_env().get("DROID_TYPE", "R")
        droid_header_icon = globals()[DROID_TYPE_ICONS.get(current_type, "MODEL_ICON_R2D2")]
        html = safe_render(BLUETOOTH_HTML, r2_svg=R2_SVG, nav=render_nav("bluetooth"), droid_header_icon=droid_header_icon)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_calibration(self):
        vals = read_env()
        html = safe_render(CALIBRATION_HTML, r2_svg=R2_SVG, nav=render_nav("calibration"), DROID_NAME=display_droid_name(vals["DROID_NAME"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_reset(self):
        html = safe_render(RESET_HTML, r2_svg=R2_SVG, nav=render_nav("reset"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _slot_picker_html(self) -> str:
        """Radio list of the 5 custom slots for forking a locked default into —
        occupied slots are flagged so a slot is never silently overwritten.
        Defaults to the first empty slot found; the user can pick any of them."""
        rows = ""
        first_empty_found = False
        for i in range(1, 6):
            slot = str(i)
            occupied = os.path.exists(_personality_map_path(slot))
            if occupied:
                name = read_personality_name(slot)
                label = f'<span class="occupied">Slot {slot} — "{name}" (will overwrite)</span>'
                checked = ""
            else:
                label = f'<span>Slot {slot} — empty</span>'
                checked = "checked" if not first_empty_found else ""
                first_empty_found = True
            rows += f"""
            <label class="slot-row">
              <input type="radio" name="targetSlot" value="{slot}" {checked}>
              {label}
            </label>"""
        return f'<label class="field-label" style="display:block;margin-bottom:8px">Save to slot</label><div class="slot-list">{rows}</div>'

    def _serve_edit_personality(self, slot):
        import json as _json
        is_locked = not str(slot).isdigit()
        name = read_personality_name(slot)
        map_path = _personality_map_path(slot)
        traits = {"brave": 3, "curious": 3, "sassy": 3, "playful": 3, "sensitive": 3}
        if os.path.exists(map_path):
            try:
                with open(map_path) as f:
                    data = _json.load(f)
                saved_traits = data.get("traits", {})
                traits = {k: saved_traits.get(k, v) for k, v in traits.items()}
            except Exception:
                pass

        if is_locked:
            display_name = PERSONALITY_DEFAULT_NAMES.get(str(slot), name)
            locked_note = f'<p class="locked-note">{display_name} is a default and can\'t be overwritten — saving will create a new custom personality.</p>'
            save_dialog_title = "Save As New Personality"
            locked_warning = f'<p class="locked-note">{display_name} is a default and can\'t be overwritten. Save your changes as a new custom personality below.</p>'
            name_value = ""
            name_placeholder = "e.g. My R2 Tweaks"
            slot_picker = self._slot_picker_html()
        else:
            display_name = name
            locked_note = ""
            save_dialog_title = "Save Personality"
            locked_warning = ""
            name_value = name
            name_placeholder = f"Personality Profile {slot}"
            slot_picker = ""

        html = safe_render(
            EDIT_PERSONALITY_HTML,
            r2_svg=R2_SVG,
            slot=str(slot),
            profile_name=display_name,
            locked_note=locked_note,
            save_dialog_title=save_dialog_title,
            locked_warning=locked_warning,
            name_value=name_value,
            name_placeholder=name_placeholder,
            slot_picker=slot_picker,
            brave=str(traits["brave"]),
            curious=str(traits["curious"]),
            sassy=str(traits["sassy"]),
            playful=str(traits["playful"]),
            sensitive=str(traits["sensitive"]),
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _serve_json(self, data):
        import json as _json
        body = _json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.makedirs(MAP_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ConfigHandler)
    print(f"KYBER Configuration Server running at http://{socket.gethostname()}.local:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nConfig server stopped.", flush=True)
