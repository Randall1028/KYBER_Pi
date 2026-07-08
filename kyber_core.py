#!/usr/bin/env python3
"""
KYBER Core — multi-provider STT + LLM brain (Logic Core Connections)
Run with: venv/bin/python3 kyber_core.py
"""

import os
import json
import time
import base64
import struct
import math
import asyncio
import random
import subprocess
import tempfile

import ctypes
import wave

import logging
import requests
import threading
import concurrent.futures
from aiohttp import web
from collections import deque
from dotenv import load_dotenv
from droiddepot.connection import DroidConnection
from droiddepot.protocol import DisneyBLEManufacturerId, DroidCommandId, DroidBluetoothCharacteristics
from droiddepot.utils import int_to_hex
from droiddepot.beacon import OfficialDroidBeaconLocations
from droiddepot.script import DroidScriptEngine, DroidScripts
from bleak import BleakScanner
from bleak.assigned_numbers import AdvertisementDataType

# ── Dynamic paths ────────────────────────────────────────────────────────────
HOME         = os.path.expanduser('~')
PROJECT_DIR  = os.path.join(HOME, 'kyber')
ENV_PATH     = os.path.join(PROJECT_DIR, '.env')
MAP_DIR      = os.path.join(PROJECT_DIR, 'personality_maps')

# ── Silence ALSA/JACK noise ─────────────────────────────────────────────────
ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
def py_error_handler(filename, line, function, err, fmt): pass
c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
try:
    asound = ctypes.cdll.LoadLibrary('libasound.so.2')
    asound.snd_lib_error_set_handler(c_error_handler)
except Exception:
    pass

# dbus-fast logs a noisy ERROR every time BlueZ fires an AdvertisementMonitor
# DeviceFound event and the Python-side handler is no longer registered (e.g.
# during the beacon scanner's pause/teardown window between reconnects). These
# are harmless -- BlueZ is just flushing a queue of pending scan results to a
# handler that's already gone -- but they flood the journal and obscure real
# errors. Raising this logger to CRITICAL silences them.
logging.getLogger('dbus_fast.message_bus').setLevel(logging.CRITICAL)

# ── Load environment ────────────────────────────────────────────────────────
load_dotenv(dotenv_path=ENV_PATH)

# ── Logic Core Connections — load every provider's key, validate only the
# ones actually in use ────────────────────────────────────────────────────────
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY")
DEEPGRAM_API_KEY   = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
GOOGLE_STT_API_KEY = os.getenv("GOOGLE_STT_API_KEY")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")

STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").strip().lower()
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

# Same value/env-key pairing as kyber_config_server.py's STT_PROVIDERS /
# LLM_PROVIDERS lists, kept independently here since this process never
# imports the config server module — the two just have to agree on names.
_STT_KEYS = {
    "deepgram":   ("DEEPGRAM_API_KEY",   DEEPGRAM_API_KEY),
    "groq":       ("GROQ_API_KEY",       GROQ_API_KEY),
    "openai":     ("OPENAI_API_KEY",     OPENAI_API_KEY),
    "google":     ("GOOGLE_STT_API_KEY", GOOGLE_STT_API_KEY),
    "assemblyai": ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_API_KEY),
}
_LLM_KEYS = {
    "gemini":    ("GEMINI_API_KEY",    GEMINI_API_KEY),
    "openai":    ("OPENAI_API_KEY",    OPENAI_API_KEY),
    "anthropic": ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    "groq":      ("GROQ_API_KEY",      GROQ_API_KEY),
}

_stt_key_name, _stt_key_val = _STT_KEYS.get(STT_PROVIDER, _STT_KEYS["deepgram"])
_llm_key_name, _llm_key_val = _LLM_KEYS.get(LLM_PROVIDER, _LLM_KEYS["gemini"])

if not _stt_key_val or not _llm_key_val:
    print("=" * 50, flush=True)
    print("ERROR: Missing required API keys.", flush=True)
    print(f"Active STT provider '{STT_PROVIDER}' needs {_stt_key_name}.", flush=True)
    print(f"Active LLM provider '{LLM_PROVIDER}' needs {_llm_key_name}.", flush=True)
    print("Please visit http://r2-unit.local:5001 to", flush=True)
    print("configure your AI providers under Logic Core Connections.", flush=True)
    print("=" * 50, flush=True)
    exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
MIC_DEVICE           = os.getenv("MIC_DEVICE", "pipewire")
MIC_DEVICE_FALLBACK  = os.getenv("MIC_DEVICE_FALLBACK", "pipewire")
DROID_MAC            = os.getenv("DROID_MAC", "").upper().strip()
DROID_NAME           = os.getenv("DROID_NAME", "").strip() or "Droid"
SAMPLE_RATE          = 16000  # matches what STT providers (Deepgram et al.) are actually tuned
                               # for, and avoids upsampling from the comlink's native 16kHz
                               # Bluetooth mic codec rate just to have Deepgram downsample it
                               # right back — was 44100 (CD-quality, no benefit for speech)
# ── VAD / audio capture constants ────────────────────────────────────────────
# WebRTC VAD requires frames of exactly 10, 20, or 30ms. 20ms is the standard
# choice — good temporal resolution without being noisy on individual frames.
VAD_FRAME_MS          = 20    # ms per frame
VAD_FRAME_BYTES       = int(SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 640 bytes at 16kHz S16_LE
VAD_AGGRESSIVENESS    = 3     # 0 (least) – 3 (most); hardware max, already maxed
VAD_PRE_ROLL_FRAMES   = 15    # frames buffered before speech starts (~300ms)
VAD_SPEECH_START      = 10    # consecutive speech frames to confirm start (~200ms)
VAD_SPEECH_END        = 25    # consecutive non-speech frames to confirm end (~500ms)
VAD_MAX_SPEECH_FRAMES = 750   # hard cap: 750 × 20ms = 15 seconds
VAD_IDLE_YIELD_FRAMES = 100   # return None after ~2s of silence so the main loop
                              # can check hotel/motor/mapper without starving
VAD_RMS_SEED          = 100   # Initial ambient estimate on boot -- a genuine near-silence
                              # value (real dead-silence frames measured near 0 earlier this
                              # session), NOT the starting floor itself. The floor is
                              # seed + margin (see below), sized to start at the already-
                              # proven-safe 800 baseline and only rise from there.
VAD_RMS_MARGIN        = 700   # A frame must be at least this far above the current ambient
                              # estimate to count as speech. seed(100) + margin(700) = 800
                              # starting floor, matching tonight's confirmed-good static value.
VAD_RMS_FLOOR_MIN     = 800   # Hard floor -- the dynamic tracker can only ever raise the
                              # bar above the already-proven-safe static value from tonight,
                              # never lower it. This is the direct fix for the earlier
                              # session's failure mode (ambient estimate collapsing toward
                              # zero during pauses made the floor too sensitive) -- even if
                              # the estimate itself reads near-zero, the floor can't follow
                              # it down past 800.
VAD_RMS_FLOOR_MAX     = 1400  # Ceiling for a loud venue (theme park, birthday party) --
                              # still well below real voice at raised/shouting volume
                              # (measured 1200-2500 in a quiet room; shouting over a loud
                              # room easily clears 2500).
AMBIENT_TRACK_DOWN_ALPHA = 0.1   # EMA rate when a frame is quieter than the current
                                  # estimate -- fast, so the floor (bounded by
                                  # VAD_RMS_FLOOR_MIN regardless) settles back down
                                  # quickly once a loud venue quiets down.
AMBIENT_TRACK_UP_ALPHA   = 0.01  # EMA rate when a frame is louder -- slow, so a single
                                  # loud transient doesn't yank the floor up; needs a
                                  # few seconds of sustained louder ambient to move it.
AMBIENT_LOG_DELTA       = 25  # minimum change in the dynamic floor (RMS units) before
                               # it's worth a fresh [AMBIENT] journal line -- keeps
                               # things quiet in a stable room and only speaks up when
                               # the floor is actually adapting. See _last_logged_floor.
                              # Together this replaces a single fixed loudness floor with
                              # one that adapts to the room: a frame only counts as speech
                              # if VAD's spectral classifier AND amplitude (above the live
                              # ambient floor) both agree. Critically, the ambient estimate
                              # is only updated during genuinely idle stretches (not while
                              # mid-utterance -- see collect_speech_segment) -- last
                              # session's version updated on every non-speech frame
                              # including brief pauses between words, which dragged the
                              # estimate down and made the floor too sensitive; that bug is
                              # what got this feature fully reverted the first time.
                              # 16-bit PCM range is ±32768.
_ambient_rms_est      = float(VAD_RMS_SEED)  # updated in collect_speech_segment()
_live_mic_rms         = 0.0   # instantaneous per-frame RMS, updated every frame
                              # regardless of speech/silence -- unlike
                              # _ambient_rms_est (a slow-adapting background
                              # floor), this reacts immediately to someone
                              # talking. Exposed via /droid_status for the
                              # wizard's Ready page mic-level bar.
_last_logged_floor    = float(VAD_RMS_FLOOR_MIN)  # last floor value printed to the
                                  # journal -- starts at the static baseline since
                                  # that's the floor's own starting value (seed+margin
                                  # clamped to FLOOR_MIN). See AMBIENT_LOG_DELTA.
POST_PLAY_DELAY      = 1.5
MIC_GATE_DURATION    = POST_PLAY_DELAY  # how long to ignore captured speech after
                                          # any droid audio plays, so R2 hearing his
                                          # own dome sounds/reactions doesn't burn an
                                          # STT call or trigger a reaction to himself.
                                          # Reuses the same estimate play_emotion
                                          # already assumes for a sound's length --
                                          # bump independently if it needs tuning.
_mic_gate_until      = 0.0               # set by _play_droid_audio(), read in main()
HISTORY_LENGTH       = 3
MIN_TRANSCRIPT_WORDS = 3

KEEPALIVE_INTERVAL   = 90
THINKING_TIMEOUT     = 2.0
ACTIVE_PERSONALITY   = os.getenv("ACTIVE_PERSONALITY", "1").strip()
ACTIVE_SOUND_PROFILE = int(os.getenv("ACTIVE_SOUND_PROFILE", "1"))
DROID_TYPE           = os.getenv("DROID_TYPE", "R").strip()
MAPPER_API_PORT      = 5002

# ── Calibration scaling — left/right motor run-time multipliers ─────────────
# Loaded once at module load, same pattern as every other KYBER setting.
# Defaults to 1.0/1.0 (unscaled — today's existing tuned values) until the
# Calibration page has been run and writes real values into .env.
CALIBRATION_LEFT_SCALE  = float(os.getenv("CALIBRATION_LEFT_SCALE", "1.0"))
CALIBRATION_RIGHT_SCALE = float(os.getenv("CALIBRATION_RIGHT_SCALE", "1.0"))

# Sane ceiling/floor for live calibration values — catches a stray extra digit
# or decimal-point typo before it becomes a standing multiplier on every
# future gesture's motor duration. 3.0 covers real-world worn-motor values
# with headroom; 0.25 mainly just catches an obvious fat-finger near zero.
CALIBRATION_SCALE_MIN = 0.25
CALIBRATION_SCALE_MAX = 3.0

# ── Chassis profiles — per-droid-type movement timing multipliers ───────────
# Keyed off DROID_TYPE ("R"/"BB"/"C"/"A"/"BD" — set by the Mainframe chassis
# buttons, lowercased here for lookup). turn_multiplier scales spin-in-place
# legs (dir0/dir1 opposite); drive_multiplier scales straight forward/backward
# legs (dir0/dir1 same) — same split get_calibration_scale already uses for
# left/right. All default to 1.0 (identical to today's R-tuned values) until
# real numbers come from actually testing each chassis. No live UI writes
# these — hand-edit this dict directly when you have a real value to set.
CHASSIS_PROFILES = {
    "r":  {"name": "R-series",  "turn_multiplier": 1.0,  "drive_multiplier": 1.0},
    "bb": {"name": "BB-series", "turn_multiplier": 1.0,  "drive_multiplier": 1.0},
    "c":  {"name": "C-series",  "turn_multiplier": 1.0,  "drive_multiplier": 1.0},
    "a":  {"name": "A-series",  "turn_multiplier": 2.6,  "drive_multiplier": 1.12},
    "bd": {"name": "BD-series", "turn_multiplier": 0.9,  "drive_multiplier": 7.5},
}

def get_chassis_profile() -> dict:
    """Active chassis profile, keyed off DROID_TYPE. Falls back to R-series
    if DROID_TYPE is ever unset or unrecognized, so behavior is unchanged
    for anyone who hasn't touched the chassis selector."""
    return CHASSIS_PROFILES.get(DROID_TYPE.lower(), CHASSIS_PROFILES["r"])

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
    f"?key={GEMINI_API_KEY}"
)

BLASTER_SOUNDS = [
    {"bank_id": 11, "sound_id": 1},
    {"bank_id": 11, "sound_id": 2},
]

STAY_AWAKE_SOUNDS = [
    {"bank_id": 3, "sound_id": 1},
    {"bank_id": 3, "sound_id": 2},
    {"bank_id": 3, "sound_id": 3},
    {"bank_id": 6, "sound_id": 1},
    {"bank_id": 6, "sound_id": 2},
    {"bank_id": 6, "sound_id": 3},
    {"bank_id": 6, "sound_id": 4},
]

GO_TO_SLEEP_SOUNDS = [
    {"bank_id": 1, "sound_id": 3},
    {"bank_id": 1, "sound_id": 4},
    {"bank_id": 7, "sound_id": 1},
    {"bank_id": 7, "sound_id": 2},
    {"bank_id": 7, "sound_id": 3},
    {"bank_id": 7, "sound_id": 4},
]

# ── Load sound profile ───────────────────────────────────────────────────────
def load_emotion_map() -> dict:
    map_path = os.path.join(MAP_DIR, f'sound_profile_{ACTIVE_SOUND_PROFILE}.json')
    if os.path.exists(map_path):
        try:
            with open(map_path, 'r') as f:
                data = json.load(f)
            if "emotion_to_sounds" in data:
                return data["emotion_to_sounds"]
            print(f"[WARN]: Sound profile {ACTIVE_SOUND_PROFILE} has no emotion_to_sounds — using built-in defaults", flush=True)
        except Exception as e:
            print(f"[WARN]: Could not load sound profile: {e}", flush=True)
    else:
        print(f"[WARN]: No sound profile found — using built-in defaults", flush=True)
    return DEFAULT_EMOTION_MAP


# ── Load personality traits ──────────────────────────────────────────────────
def _personality_map_path() -> str:
    """ACTIVE_PERSONALITY is either a custom slot number ("1".."5") or one of
    the locked default ids ("r2d2", "bb8", "chopper", "bd1", "aseries") — each
    addresses a
    different file (personality_N.json vs personality_default_*.json)."""
    if ACTIVE_PERSONALITY.isdigit():
        return os.path.join(MAP_DIR, f'personality_{ACTIVE_PERSONALITY}.json')
    return os.path.join(MAP_DIR, f'personality_default_{ACTIVE_PERSONALITY}.json')


def load_personality_traits() -> dict:
    defaults = {"brave": 3, "curious": 3, "sassy": 3, "playful": 3, "sensitive": 3}
    map_path = _personality_map_path()
    if os.path.exists(map_path):
        try:
            with open(map_path, 'r') as f:
                data = json.load(f)
            traits = data.get("traits", {})
            return {k: traits.get(k, v) for k, v in defaults.items()}
        except Exception as e:
            print(f"[WARN]: Could not load personality traits: {e}", flush=True)
    else:
        print(f"[WARN]: No personality file found for '{ACTIVE_PERSONALITY}' — using neutral traits", flush=True)
    return defaults


# Turns a 1-5 trait slider into a sentence the LLM can use directly, instead
# of a bare number it has to guess the meaning of.
TRAIT_LINES = {
    "brave": [
        "You are highly cautious and avoid risk whenever possible.",
        "You are mostly cautious but can show confidence when it counts.",
        "You balance caution with confidence.",
        "You are confident and willing to take initiative.",
        "You act boldly and rarely hesitate, even in risky situations.",
    ],
    "sassy": [
        "You are very polite and rarely show attitude.",
        "You are mostly polite but may show mild attitude occasionally.",
        "You sometimes respond with light sarcasm.",
        "You often respond with sarcasm or attitude.",
        "You are highly sarcastic and frequently respond with strong attitude.",
    ],
    "curious": [
        "You rarely question things and prefer not to explore.",
        "You are slightly curious but not very proactive.",
        "You are moderately curious and occasionally ask questions.",
        "You actively explore and ask questions about things.",
        "You are extremely curious and will push into situations to find answers, even if it gets you into trouble.",
    ],
    "sensitive": [
        "You are emotionally tough and rarely affected by negativity.",
        "You are somewhat resilient and not easily upset.",
        "You are moderately sensitive to tone and emotion.",
        "You are emotionally reactive and can be affected by negativity.",
        "You are highly sensitive and strongly react to emotional tone or perceived criticism.",
    ],
    "playful": [
        "You are serious and rarely joke or play.",
        "You are mostly serious but occasionally lighthearted.",
        "You balance seriousness with some playful behavior.",
        "You are playful and often joke or tease.",
        "You are highly playful and frequently joke, tease, and act mischievous.",
    ],
}


def personality_summary(sliders: dict) -> str:
    """One-line gist before the detailed trait lines — keeps the model
    coherent across five separate sentences instead of treating each trait
    in isolation."""
    traits = []
    if sliders["brave"] >= 4: traits.append("bold")
    elif sliders["brave"] <= 2: traits.append("cautious")
    if sliders["curious"] >= 4: traits.append("curious")
    elif sliders["curious"] <= 2: traits.append("indifferent")
    if sliders["playful"] >= 4: traits.append("playful")
    elif sliders["playful"] <= 2: traits.append("serious")
    if sliders["sassy"] >= 4: traits.append("sarcastic")
    elif sliders["sassy"] <= 2: traits.append("polite")
    if sliders["sensitive"] >= 4: traits.append("emotionally reactive")
    elif sliders["sensitive"] <= 2: traits.append("emotionally resilient")
    if not traits:
        return "You have a balanced and adaptable personality."
    return "You are a " + ", ".join(traits) + " droid."


def build_personality_block(sliders: dict) -> str:
    lines = []
    for trait, options in TRAIT_LINES.items():
        idx = max(1, min(5, sliders.get(trait, 3))) - 1  # clamp, just in case
        lines.append(options[idx])
    return personality_summary(sliders) + "\n" + "\n".join(lines)

DEFAULT_EMOTION_MAP = {
    "confused":  [{"bank_id":1,"sound_id":1},{"bank_id":1,"sound_id":3},{"bank_id":1,"sound_id":4},{"bank_id":2,"sound_id":4},{"bank_id":3,"sound_id":3},{"bank_id":4,"sound_id":1}],
    "curious":   [{"bank_id":1,"sound_id":1},{"bank_id":2,"sound_id":4},{"bank_id":4,"sound_id":1},{"bank_id":6,"sound_id":1},{"bank_id":6,"sound_id":4}],
    "neutral":   [{"bank_id":1,"sound_id":1},{"bank_id":2,"sound_id":4},{"bank_id":3,"sound_id":2},{"bank_id":3,"sound_id":3},{"bank_id":6,"sound_id":4}],
    "angry":     [{"bank_id":1,"sound_id":2},{"bank_id":1,"sound_id":3},{"bank_id":2,"sound_id":1},{"bank_id":2,"sound_id":2},{"bank_id":2,"sound_id":3},{"bank_id":9,"sound_id":1}],
    "disgusted": [{"bank_id":1,"sound_id":2},{"bank_id":2,"sound_id":1},{"bank_id":2,"sound_id":2},{"bank_id":2,"sound_id":3}],
    "sad":       [{"bank_id":1,"sound_id":3},{"bank_id":1,"sound_id":4},{"bank_id":7,"sound_id":1},{"bank_id":7,"sound_id":2},{"bank_id":7,"sound_id":3},{"bank_id":7,"sound_id":4},{"bank_id":7,"sound_id":5}],
    "defensive": [{"bank_id":1,"sound_id":3},{"bank_id":1,"sound_id":4},{"bank_id":3,"sound_id":3},{"bank_id":4,"sound_id":1},{"bank_id":7,"sound_id":1},{"bank_id":7,"sound_id":2},{"bank_id":7,"sound_id":3},{"bank_id":7,"sound_id":4}],
    "scared":    [{"bank_id":1,"sound_id":4},{"bank_id":4,"sound_id":1},{"bank_id":5,"sound_id":1},{"bank_id":7,"sound_id":1},{"bank_id":7,"sound_id":3},{"bank_id":7,"sound_id":4}],
    "happy":     [{"bank_id":3,"sound_id":1},{"bank_id":3,"sound_id":2},{"bank_id":3,"sound_id":3},{"bank_id":6,"sound_id":1},{"bank_id":6,"sound_id":2},{"bank_id":6,"sound_id":3},{"bank_id":6,"sound_id":4}],
    "excited":   [{"bank_id":3,"sound_id":1},{"bank_id":3,"sound_id":2},{"bank_id":6,"sound_id":1},{"bank_id":6,"sound_id":2},{"bank_id":6,"sound_id":3}],
}

EMOTION_MAP        = load_emotion_map()
VALID_EMOTIONS     = [e for e in EMOTION_MAP.keys() if e not in ("start up", "motor", "blaster", "thruster")]
PERSONALITY_TRAITS = load_personality_traits()
PERSONALITY_BLOCK   = build_personality_block(PERSONALITY_TRAITS)

# Hidden for launch, not removed: Autonomous Roam's own gesture code and
# mode-state tracking stay fully intact underneath this -- only the voice
# command and the Subroutines page card are gated off, pending the
# accelerometer add-on this is meant to work alongside. Flip via .env,
# same kill-switch pattern as BEACON_RELAY_ENABLED.
ROAM_MODE_ENABLED  = os.getenv("ROAM_MODE_ENABLED", "false").strip().lower() == "true"
# Precomputed once — used for startup/reconnect sounds without rebuilding every call
_ALL_SOUNDS        = [s for sounds in EMOTION_MAP.values() for s in sounds]

# ── Keepalive state ──────────────────────────────────────────────────────────
_keepalive_active    = False
_conversation_active = False
_main_event_loop     = None
_droid_ready         = False
_kyber_fully_ready   = False  # True once mic warmup has been attempted and the
                              # startup sound has played -- the "actually
                              # listening" signal, distinct from _droid_ready
                              # (BLE-connected only). Exposed via /droid_status
                              # for the onboarding wizard's activation page.
_activation_muted    = False  # True only during the wizard's Activation page --
                              # a stray noise triggering a response mid-show
                              # would clash with the choreographed animation.
                              # Set when PLAY_ACTIVATION_ON_NEXT_BOOT fires,
                              # cleared the moment ACTIVATION_CONFIRMED is set
                              # (the same flag the Ready page transition uses).
_mapper_play_queue   = []
_mapper_play_lock    = threading.Lock()
_mapper_mode         = False
_mapper_session_count = 0  # reference count, not just a flag — see handle_mode
_last_mapper_play    = 0.0
_ble_lost            = False
_last_reconnect_time = 0.0
_ble_reconnecting    = False
_droid_waiting       = False   # True when no DROID_MAC set — waiting for user to pair
_hotel_mode_active    = False
_hotel_move_requested = False
_hotel_end_time       = 0.0
_hotel_moving         = False
_hotel_activated_time = 0.0
_expressive_mode_active = False
_calibration_probe_busy = False
_motor_command_queue  = []
_motor_command_lock   = threading.Lock()
_roam_mode_active     = False
_pet_mode_active      = False
HOTEL_DURATION       = 8 * 60 * 60   # 8 hours in seconds
HOTEL_MOVE_INTERVAL  = 15 * 60       # 15 minutes in seconds

# ── Persistent mic stream state ───────────────────────────────────────────────
_arecord_proc   = None   # the one long-lived arecord raw-PCM subprocess
_arecord_device = None   # which MIC_DEVICE it was opened on

try:
    import webrtcvad as _webrtcvad
    _vad = _webrtcvad.Vad(VAD_AGGRESSIVENESS)
    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False
    print("[VAD]: webrtcvad not installed — run: pip install webrtcvad --break-system-packages", flush=True)


def keepalive_thread(droid_ref):
    global _keepalive_active, _conversation_active, _main_event_loop
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        d = droid_ref[0]
        if d is None: continue
        # Used to skip entirely during hotel sentry, on the assumption that
        # the 15-minute movement trigger alone was enough to keep the droid
        # awake. It isn't — the droid's real auto-sleep timeout is shorter
        # than 15 minutes, leaving a dead gap with no signal at all between
        # moves (or before the first one). Let this ping run on its normal
        # cadence throughout sentry mode too; it's the same quiet LED flash
        # used everywhere else and doesn't interfere with sentry staying
        # otherwise inert.
        if _keepalive_active and not _conversation_active:
            print("[KEEPALIVE]: ping sent", flush=True)
            try:
                future = asyncio.run_coroutine_threadsafe(
                    d.flash_pairing_led("020001ff01ff01ff00"),
                    _main_event_loop
                )
                future.result(timeout=5)
            except Exception as e:
                print(f"[KEEPALIVE ERROR]: {e}", flush=True)


def hotel_sentry_thread(droid_ref):
    """Background thread — moves droid every 15 minutes during hotel sentry mode."""
    global _hotel_mode_active, _hotel_end_time, _main_event_loop, _hotel_move_requested
    last_move_time = 0.0
    while True:
        time.sleep(30)  # Check every 30 seconds
        if not _hotel_mode_active:
            last_move_time = 0.0  # Reset when sentry deactivates
            continue
        # Initialize last_move_time from activation time on first check
        if last_move_time == 0.0:
            last_move_time = _hotel_activated_time
        # Check if 8 hour timer expired
        if time.time() >= _hotel_end_time:
            print("[HOTEL]: 8 hour timer expired — deactivating sentry", flush=True)
            d = droid_ref[0]
            if d and _main_event_loop:
                asyncio.run_coroutine_threadsafe(deactivate_hotel_mode(d), _main_event_loop)
            continue
        # Move every 15 minutes
        if time.time() - last_move_time >= HOTEL_MOVE_INTERVAL:
            last_move_time = time.time()
            d = droid_ref[0]
            if d and _droid_ready and _main_event_loop:
                print("[HOTEL]: Moving to trigger AC sensor", flush=True)
                _hotel_move_requested = True


def roam_thread(droid_ref):
    """Background thread — triggers random movement during Autonomous Roam mode."""
    global _roam_mode_active, _main_event_loop
    while True:
        interval = random.choice([2 * 60, 3 * 60, 5 * 60])
        time.sleep(interval)
        if not _roam_mode_active:
            continue
        d = droid_ref[0]
        if d and _droid_ready and _main_event_loop:
            print("[ROAM]: Starting movement burst", flush=True)
            asyncio.run_coroutine_threadsafe(_roam_burst(d), _main_event_loop)


async def _roam_burst(droid):
    """Execute roam movement + dome sweep + random sound."""
    roam_fn = roam_move_bb if DROID_TYPE.lower() == "bb" else roam_move
    await asyncio.gather(
        roam_fn(droid),
        dome_thinking(droid.motor_controller)
    )
    roam_emotions = [e for e in EMOTION_MAP if e not in ("sad", "start up", "blaster", "thruster", "motor")]
    if roam_emotions:
        emotion = random.choice(roam_emotions)
        sounds = EMOTION_MAP.get(emotion, [])
        if sounds:
            s = random.choice(sounds)
            try:
                await _play_droid_audio(droid, sound_id=s["sound_id"], bank_id=s["bank_id"])
            except Exception:
                pass


def pet_thread(droid_ref):
    """Background thread — triggers erratic movement during Pet Entertainer mode."""
    global _pet_mode_active, _main_event_loop
    while True:
        interval = random.choice([30, 2 * 60, 3 * 60])
        time.sleep(interval)
        if not _pet_mode_active:
            continue
        d = droid_ref[0]
        if d and _droid_ready and _main_event_loop:
            print("[PET]: Starting movement burst", flush=True)
            asyncio.run_coroutine_threadsafe(_pet_burst(d), _main_event_loop)


async def _pet_burst(droid):
    """Execute pet movement + dome sweep + random sound."""
    pet_fn = pet_move_bb if DROID_TYPE.lower() == "bb" else pet_move
    await asyncio.gather(
        pet_fn(droid),
        dome_thinking(droid.motor_controller)
    )
    pet_emotions = [e for e in EMOTION_MAP if e not in ("sad", "start up", "blaster", "thruster", "motor")]
    if pet_emotions:
        emotion = random.choice(pet_emotions)
        sounds = EMOTION_MAP.get(emotion, [])
        if sounds:
            s = random.choice(sounds)
            try:
                await _play_droid_audio(droid, sound_id=s["sound_id"], bank_id=s["bank_id"])
            except Exception:
                pass


# ── Motor scaling (Calibration) ───────────────────────────────────────────────

def get_calibration_scale(dir0: int, dir1: int) -> float:
    """Return the calibration multiplier for a motor leg based on its direction pair.

    Spin-right (motor0 forward / motor1 backward) uses CALIBRATION_RIGHT_SCALE.
    Spin-left (the flip) uses CALIBRATION_LEFT_SCALE.
    Straight forward/backward legs (both motors same direction) have no "leading"
    motor — there's no spin to attribute the reading to — so they use the average
    of the two. This carries the general charge-level signal both spins share
    without applying either spin's specific directional bias.
    """
    if dir0 == 0 and dir1 == 8:
        return CALIBRATION_RIGHT_SCALE
    elif dir0 == 8 and dir1 == 0:
        return CALIBRATION_LEFT_SCALE
    else:
        return (CALIBRATION_LEFT_SCALE + CALIBRATION_RIGHT_SCALE) / 2


def get_chassis_scale(dir0: int, dir1: int) -> float:
    """Return the active chassis-type multiplier for a motor leg. Spin legs
    (dir0/dir1 opposite — same pairs get_calibration_scale treats as a spin)
    use turn_multiplier; anything else (straight forward/backward) uses
    drive_multiplier. Orthogonal to get_calibration_scale — that one corrects
    for this specific unit's left/right asymmetry, this one corrects for the
    chassis design's overall turn-vs-drive speed, and both apply together."""
    profile = get_chassis_profile()
    if (dir0, dir1) in ((0, 8), (8, 0)):
        return profile["turn_multiplier"]
    else:
        return profile["drive_multiplier"]


async def _send_motor_speed_fast(mc, direction: int, motor_id: int, speed: int, ramp_speed: int = 300, delay: int = 0):
    """Same wire format as droiddepot's own DroidMotorController.send_motor_speed_command
    -- reuses the library's own int_to_hex/build_droid_command exactly, byte for byte --
    but forces write-without-response explicitly on the final BLE write, instead of
    leaving it to bleak's default.

    bleak's write_gatt_char(response=None) resolves to write-WITH-response whenever
    "write" is present in the characteristic's properties (its own source: 'prefer
    write-with-response ... since it is the more reliable write'). Confirmed on real
    hardware (check_motor_char.py) that this characteristic reports BOTH
    write-without-response and write, so every motor command sent through the
    library's own send_motor_speed_command has been silently taking the slower,
    acknowledgment-waiting path this whole time. That round-trip wait -- not
    scheduling order -- was the actual remaining source of the left/right timing gap
    after the gather() fix in _scaled_hold. The official Droid Depot app driving R2
    perfectly straight over this exact same protocol is what made it worth checking
    in the first place, rather than accepting the gap as a hardware floor.

    Deliberately reimplemented at this level rather than patched inside the
    third-party droiddepot package, so the fix lives in this project and survives a
    library upgrade."""
    delay_hex = int_to_hex(delay)
    if len(delay_hex) < 4:
        delay_hex = delay_hex.rjust(4, '0')
    motor_select = f"{direction}{motor_id}"
    motor_command = f"{motor_select}{int_to_hex(speed)}{int_to_hex(ramp_speed)}{delay_hex}"
    command_bytes = mc.droid.build_droid_command(DroidCommandId.SetMotorSpeed, motor_command)
    await mc.droid.droid.write_gatt_char(
        DroidBluetoothCharacteristics.DroidCommandCharacteristic,
        bytearray.fromhex(command_bytes.hex()),
        response=False,
    )


async def _send_motor_speed_confirmed(mc, direction: int, motor_id: int, speed: int, ramp_speed: int = 300, delay: int = 0):
    """Same wire format as _send_motor_speed_fast, but WITH delivery
    confirmation (bleak's default acknowledged write) instead of
    write-without-response.

    Reserved for stop_motors() specifically. _send_motor_speed_fast's
    fire-and-forget write is correct for drive commands (no delivery
    guarantee needed, and the round-trip wait was the actual source of the
    R2-Drift2 left/right gap). But stop_motors() is the one command whose
    entire job is safety, and until now it reused that exact same
    unconfirmed path -- a dropped stop write leaves the motor running on
    its last received nonzero speed with nothing to correct it, since the
    code has already moved on. Stop is infrequent and not timing-critical
    the way synchronized drive commands are, so the extra round-trip cost
    here is a non-issue."""
    delay_hex = int_to_hex(delay)
    if len(delay_hex) < 4:
        delay_hex = delay_hex.rjust(4, '0')
    motor_select = f"{direction}{motor_id}"
    motor_command = f"{motor_select}{int_to_hex(speed)}{int_to_hex(ramp_speed)}{delay_hex}"
    command_bytes = mc.droid.build_droid_command(DroidCommandId.SetMotorSpeed, motor_command)
    await mc.droid.droid.write_gatt_char(
        DroidBluetoothCharacteristics.DroidCommandCharacteristic,
        bytearray.fromhex(command_bytes.hex()),
        response=True,
    )


async def _scaled_hold(mc, dir0: int, dir1: int, speed0: int, speed1: int, duration: float, scale: float):
    """Lowest-level motor-leg primitive — send commands and hold for
    `duration * scale * chassis_scale` seconds. Does NOT stop the motors
    afterward, since some sequences (Roam/Pet's forward-into-turn) flow
    directly into the next phase by overwriting the motor command, with no
    stop in between — same as today.

    Also the single choke point for gating the mic against motor noise --
    every drive_leg/drive_hold call and the calibration probes all funnel
    through here, same centralization principle as _play_droid_audio()
    already uses for sound effects. Previously only audio playback stamped
    _mic_gate_until; motor noise (BB's gearmotor whine especially) was
    completely ungated, despite being loud and spectrally speech-shaped
    enough to pass both VAD and the RMS floor. Unlike audio (no true
    'finished' signal, hence the MIC_GATE_DURATION estimate), the real
    hold time is already known exactly here, so the gate is precise --
    plus MIC_GATE_DURATION on top as trailing margin for BLE latency and
    mechanical coast-down noise after the commanded stop."""
    global _mic_gate_until
    chassis_scale = get_chassis_scale(dir0, dir1)
    hold_time = duration * scale * chassis_scale
    _mic_gate_until = time.time() + hold_time + MIC_GATE_DURATION
    # Both motor commands fire concurrently, not sequentially, AND explicitly as
    # write-without-response (see _send_motor_speed_fast) -- together these close
    # both the software-scheduling and BLE-ack-wait sources of the head start
    # motor 0 used to get on every single gesture.
    await asyncio.gather(
        _send_motor_speed_fast(mc, direction=dir0, motor_id=0, speed=speed0),
        _send_motor_speed_fast(mc, direction=dir1, motor_id=1, speed=speed1),
    )
    await asyncio.sleep(hold_time)


async def stop_motors(mc):
    # Confirmed/acknowledged write, not the fast fire-and-forget path --
    # see _send_motor_speed_confirmed's docstring. This is the one motor
    # command that must not be allowed to silently drop.
    await asyncio.gather(
        _send_motor_speed_confirmed(mc, direction=0, motor_id=0, speed=0),
        _send_motor_speed_confirmed(mc, direction=0, motor_id=1, speed=0),
    )


async def drive_leg(mc, dir0: int, dir1: int, speed0: int, speed1: int, duration: float):
    """Production gesture primitive for a single leg that stops afterward — the
    common case (every expressive gesture, Hotel Sentry). Looks up the live stored
    calibration value automatically; `duration` is each gesture's existing tuned
    literal, untouched — only the calibration scale multiplies it. Stop is
    guaranteed via finally — same pattern calibration_spin_probe already uses —
    so a failure partway through never leaves a motor running with nothing to
    stop it."""
    scale = get_calibration_scale(dir0, dir1)
    try:
        await _scaled_hold(mc, dir0, dir1, speed0, speed1, duration, scale)
    finally:
        await stop_motors(mc)


async def drive_hold(mc, dir0: int, dir1: int, speed0: int, speed1: int, duration: float):
    """Same as drive_leg but doesn't stop afterward — for continuous multi-phase
    sequences (Roam/Pet) where the next phase overwrites the motor command directly,
    exactly as today's choreography does. Caller is responsible for stopping once
    the whole sequence finishes."""
    scale = get_calibration_scale(dir0, dir1)
    await _scaled_hold(mc, dir0, dir1, speed0, speed1, duration, scale)


# Baseline duration for a cold-launch full-360 spin (droid idle, not mid-sequence),
# at default/un-adjusted motor values. Matches expressive_happy_spin's own literal —
# the Calibration page is always a cold launch, never mid-gesture, so it baselines
# off this number rather than Happy Dance's in-sequence (already-moving) 0.6s spin.
BASE_SPIN_DURATION = 0.5  # new "1.0" calibration baseline, measured fresh at true 255 power
                           # after the motor-write-mode fix -- everything before this was
                           # measured at effectively ~39% power (see motor speed scaling
                           # history), including the old 0.6/0.9 values this replaces.
                           # Calibration scale factors derived against the old baseline
                           # are not valid here -- re-run the calibration wizard.

# ── BB-series motor constants — hardware-validated on Nub (8N-UB) ────────────
# 255/255 at 0.6s = full 360° cold launch (re-validated this session — was
# 0.3s under the pre-write-mode-fix ~39% power scaling; real power needs the
# longer hold to actually complete the rotation, so this applies everywhere
# the 360 building block is used: Happy Spin, Happy Dance's middle beat,
# Angry Charge, Defensive, Scared). 200/200 is the safe general-purpose
# forward/backward ceiling (higher tested fine on carpet; 200 is the
# conservative default for mixed surface use). R-unit values (60–100) are NOT
# directly comparable — they were tuned empirically at a lower absolute power
# level and the two series' values can't be cross-calculated without
# hardware re-testing.
BB_SPIN_SPEED    = 255   # validated spin/turn speed for full rotations (happy spin, dance, roam)
BB_QUICK_SPEED   = 195   # cold-launch reaction spin speed for Roam/Pet — untouched tonight
BB_FWD_SPEED     = 200   # validated safe forward/backward ceiling (also used as Angry Charge's forward power)
BB_SLOW_SPEED    = 130   # gentle speed for drift/nudge gestures (~65% of fwd)
BB_RETREAT_SPEED = 110   # Defensive reverse power (calm withdrawal)
BB_SCARED_SPEED  = 255   # Scared reverse power (panicked, full power — distinct from Defensive)
BB_180_SPEED     = 215   # "Absolute Cold Launch" 180 — About Face + Hotel Sentry
BB_SPIN_FULL     = 0.60  # duration for full 360° at BB_SPIN_SPEED (re-validated at true power)
BB_SPIN_HALF     = 0.15  # 180° approximation for Happy Dance — untouched tonight
BB_SPIN_QUICK    = 0.10  # quick reaction spin duration for Roam/Pet — untouched tonight
BB_180_DURATION  = 0.30  # duration for the 180 cold-launch move at BB_180_SPEED

# ── Motion Lab — raw motor-test primitive for the Gestures page's manual
# testing panel. Built first for dialing in BB-series values with no tuned
# choreography yet, but the categories are chassis-agnostic — they're just
# the same dir0/dir1 pairs every other gesture already uses (forward/backward
# drive both motors the same direction, turns drive them opposite). Duration
# and per-motor speed come live from the panel instead of being baked into a
# named gesture; nothing here is persisted, it only fires `drive_leg` once
# per request with whatever numbers were on the page at the time.
MOTOR_TEST_DIRECTIONS = {
    "forward":  (0, 0),
    "backward": (8, 8),
    "left":     (0, 8),
    "right":    (8, 0),
}
MOTOR_TEST_DURATION_MIN = 0.1
MOTOR_TEST_DURATION_MAX = 3.0  # sane ceiling so a typo can't hold power for a long, unsupervised run


def _active_protocol_name():
    """Name of whichever autonomous mode is currently active, or None if none
    are. Used so calibration refuses to start rather than silently competing
    with Hotel Sentry / Roam / Pet / Expressive Mode for the same motors."""
    if _hotel_mode_active:      return "Hotel Sentry"
    if _roam_mode_active:       return "Autonomous Roam"
    if _pet_mode_active:        return "Pet Entertainer"
    if _expressive_mode_active: return "Expressive Mode"
    return None


async def calibration_spin_probe(droid, direction: str, scale: float):
    """Calibration-only primitive — run a single full-360 spin probe at an explicit
    candidate scale, completely bypassing the stored CALIBRATION_LEFT/RIGHT_SCALE
    values. Used for both the initial unscaled diagnostic spin (scale=1.0) and the
    post-computation verification spin (scale=newly-derived candidate), before
    anything has been locked in via .env."""
    mc = droid.motor_controller
    dir0, dir1 = (0, 8) if direction == "right" else (8, 0)
    actual_seconds = BASE_SPIN_DURATION * scale
    print(f"[CALIBRATION]: {direction} probe starting — scale={scale:.3f}, hold={actual_seconds:.3f}s", flush=True)
    try:
        await _scaled_hold(mc, dir0, dir1, 255, 255, BASE_SPIN_DURATION, scale)
    finally:
        # Always stop the motors, even if this probe gets cancelled mid-spin —
        # e.g. the caller already gave up waiting and cancelled the orphaned task.
        # Without this, a cancelled probe could leave a motor running indefinitely.
        await stop_motors(mc)
    print(f"[CALIBRATION]: {direction} probe finished", flush=True)


async def calibration_victory(droid):
    """Celebratory flourish once both directions are confirmed — a quick spin
    plus a happy sound. Purely cosmetic, not a measurement, so it always runs at
    the plain default baseline rather than threading through whatever the
    just-confirmed candidate scales happened to be."""
    mc = droid.motor_controller
    print("[CALIBRATION]: victory spin", flush=True)
    try:
        await _scaled_hold(mc, 0, 8, 255, 255, BASE_SPIN_DURATION, 1.0)
    finally:
        await stop_motors(mc)
    sounds = EMOTION_MAP.get("happy", [])
    if sounds:
        s = random.choice(sounds)
        try:
            await _play_droid_audio(droid, sound_id=s["sound_id"], bank_id=s["bank_id"])
        except Exception:
            pass


async def hotel_move(droid):
    """AC-sensor-triggering patrol movement — rebuilt from scratch with the
    new true-power values, now that the motor write-mode fix has eliminated
    the persistent left/right pull the old cold-launch spin lead-in and
    60%-scaled forward legs were partly compensating for. Straight patrol
    pattern, no spool-up spin needed. Spins are mirrored (first right, then
    left) so repeated cycles don't accumulate rotation in one direction."""
    global _hotel_moving
    _hotel_moving = True
    try:
        mc = droid.motor_controller
        await drive_leg(mc, 0, 0, 190, 190, 1)   # Forward 1s
        await asyncio.sleep(5)
        await drive_leg(mc, 0, 8, 110, 110, 0.4)  # 180 spin
        await drive_leg(mc, 0, 0, 190, 190, 1)   # Forward 1s
        await asyncio.sleep(5)
        await drive_leg(mc, 8, 0, 110, 110, 0.4)  # 180 spin — mirrored
        print("[HOTEL]: Movement complete", flush=True)
    except Exception as e:
        print(f"[HOTEL MOVE ERROR]: {e}", flush=True)
    finally:
        _hotel_moving = False


async def hotel_move_v2(droid):
    """Evaluation rig, not wired into anything yet — two About Face gestures
    back to back, each starting from a real stop rather than chained
    momentum. expressive_about_face() already ends in a forward drive, so a
    couple seconds' pause here (well beyond the 0.2-0.3s pauses used inside
    the gestures themselves) is to let that fully settle before the second
    one's cold-launch spin starts, rather than inheriting residual motion
    the way hotel_move's old 180s did. Reuses expressive_about_face directly
    so retuning that gesture later keeps this in sync automatically — call
    this manually to test cold-launch spin timing in isolation."""
    global _hotel_moving
    _hotel_moving = True
    try:
        await expressive_about_face(droid)
        await asyncio.sleep(2.0)
        await expressive_about_face(droid)
        print("[HOTEL V2]: Movement complete", flush=True)
    except Exception as e:
        print(f"[HOTEL V2 MOVE ERROR]: {e}", flush=True)
    finally:
        _hotel_moving = False


# ── Expressive Mode animations ───────────────────────────────────────────────

async def expressive_happy_dance(droid):
    """Forward → 180 spin → forward → 360 spin → forward → 180 spin."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, 100, 100, 1)      # Forward 1s
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 8, 100, 100, 0.2)     # 180 spin — matches hotel_move's corrected in-sequence value
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, 100, 100, 1)      # Forward 1s
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 8, 100, 100, 0.6)     # 360 spin (in-sequence — already moving, see BASE_SPIN_DURATION note)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, 100, 100, 1)      # Forward 1s
        await asyncio.sleep(0.2)
        await drive_leg(mc, 8, 0, 100, 100, 0.2)     # 180 spin back — same correction, mirrored
        print("[EXPRESSIVE]: Happy dance complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_happy_spin(droid):
    """Happy — simple 360 spin (cold launch — see BASE_SPIN_DURATION note)."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 8, 255, 255, BASE_SPIN_DURATION)
        print("[EXPRESSIVE]: Happy spin complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_retreat(droid):
    """Scared — fast reverse with slight turn."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, 100, 90, 1)
        print("[EXPRESSIVE]: Retreat complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_angry_charge(droid):
    """Angry — fast charge forward."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, 255, 255, 1)
        print("[EXPRESSIVE]: Angry charge complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_sad_drift(droid):
    """Sad — slow drift backward (defensive speed)."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, 50, 50, 1.5)
        print("[EXPRESSIVE]: Sad drift complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_curious_nudge(droid):
    """Curious — small forward nudge."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, 60, 60, 0.7)
        print("[EXPRESSIVE]: Curious nudge complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_defensive_back(droid):
    """Defensive — fast backward (scared retreat speed)."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, 100, 100, 1)
        print("[EXPRESSIVE]: Defensive back complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_forward(droid):
    """Voice command — come here — move forward toward user."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, 80, 80, 1.5)
        print("[EXPRESSIVE]: Forward complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_back(droid):
    """Voice command — back up."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, 70, 70, 1.5)
        print("[EXPRESSIVE]: Back complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


# Emotion → motor animation map (used for 33% roll in expressive mode)
EXPRESSIVE_EMOTION_MOVES = {
    "happy":     expressive_happy_spin,
    "excited":   expressive_happy_dance,
    "scared":    expressive_retreat,
    "angry":     expressive_angry_charge,
    "sad":       expressive_sad_drift,
    "curious":   expressive_curious_nudge,
    "defensive": expressive_defensive_back,
    "disgusted": None,  # handled separately below
}


async def expressive_about_face(droid):
    """180 spin then move away."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 8, 110, 110, 0.4)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, 190, 190, 1)
        print("[EXPRESSIVE]: About Face move complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


EXPRESSIVE_EMOTION_MOVES["disgusted"] = expressive_about_face


# ── Model-specific gesture variations ────────────────────────────────────────
# Extra candidates layered on top of the shared default for a given
# (droid_type, emotion) pair. The shared default is always included in the
# pool too — these are additional options in the rotation, not replacements.
# Empty except where a model has a unique move of its own.

async def expressive_moonwalk_bd(droid):
    """BD-exclusive — backwards, 360 spin, backwards."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, 100, 100, 1)
        await drive_leg(mc, 0, 8, 100, 100, 0.6)     # 360 spin (in-sequence, same as happy_dance)
        await drive_leg(mc, 8, 8, 100, 100, 1)
        print("[EXPRESSIVE]: BD moonwalk complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


GESTURE_VARIANTS = {
    ("bd", "excited"): [expressive_moonwalk_bd],
}


# ── BB-series gesture set ─────────────────────────────────────────────────────
# Dedicated functions for BB-unit chassis — NOT modifications of R-unit values.
# Timings are shorter than R-unit equivalents because BB runs at higher absolute
# power; the same rotational/linear distance happens faster. All values
# hardware-validated on Nub (8N-UB). Dome motor commands (rotate_head/
# center_head) are intentionally omitted until BB head-motor behaviour is
# separately confirmed on hardware — play_emotion's existing exception handling
# will absorb any dome command that fires from the shared sound+dome path.

async def expressive_bb_happy_dance(droid):
    """BB — forward → 180 spin → forward → 360 spin → forward → 180 spin back."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, BB_FWD_SPEED,  BB_FWD_SPEED,  0.8)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 8, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_HALF)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, BB_FWD_SPEED,  BB_FWD_SPEED,  0.8)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 8, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_FULL)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, BB_FWD_SPEED,  BB_FWD_SPEED,  0.8)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 8, 0, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_HALF)
        print("[EXPRESSIVE]: BB happy dance complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_happy_spin(droid):
    """BB — single cold-launch 360 spin."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 8, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_FULL)
        print("[EXPRESSIVE]: BB happy spin complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_retreat(droid):
    """BB — Scared Retreat: full-power reverse burst then a 360 spin.
    Distinct from Defensive (below) — this is the panicked reaction,
    at full power rather than a calm withdrawal."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, BB_SCARED_SPEED, BB_SCARED_SPEED, 1.0)
        d0, d1 = random.choice([(0, 8), (8, 0)])
        await drive_leg(mc, d0, d1, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_FULL)
        print("[EXPRESSIVE]: BB scared retreat complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_angry_charge(droid):
    """BB — Angry Charge: forward charge then a 360 spin."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, BB_FWD_SPEED, BB_FWD_SPEED, 1.0)
        d0, d1 = random.choice([(0, 8), (8, 0)])
        await drive_leg(mc, d0, d1, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_FULL)
        print("[EXPRESSIVE]: BB angry charge complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_sad_drift(droid):
    """BB — slow backward drift."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, BB_SLOW_SPEED, BB_SLOW_SPEED, 1.5)
        print("[EXPRESSIVE]: BB sad drift complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_curious_nudge(droid):
    """BB — small forward nudge."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED, 0.6)
        print("[EXPRESSIVE]: BB curious nudge complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_defensive_back(droid):
    """BB — Defensive: calm-power reverse then a 360 spin. Distinct from
    Scared (expressive_bb_retreat) — this is a measured withdrawal, not
    a panicked one, hence the lower reverse power."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, BB_RETREAT_SPEED, BB_RETREAT_SPEED, 1.0)
        d0, d1 = random.choice([(0, 8), (8, 0)])
        await drive_leg(mc, d0, d1, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_FULL)
        print("[EXPRESSIVE]: BB defensive back complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_about_face(droid):
    """BB — quick 180 then roll away. Spin uses the "Absolute Cold Launch"
    180 constants (BB_180_SPEED/BB_180_DURATION); direction doesn't matter
    for a 180, so it's randomized each time."""
    mc = droid.motor_controller
    try:
        d0, d1 = random.choice([(0, 8), (8, 0)])
        await drive_leg(mc, d0, d1, BB_180_SPEED, BB_180_SPEED, BB_180_DURATION)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, BB_FWD_SPEED,   BB_FWD_SPEED,  0.7)
        print("[EXPRESSIVE]: BB About Face complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_forward(droid):
    """BB — come here."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 0, 180, 180, 1.5)
        print("[EXPRESSIVE]: BB forward complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


async def expressive_bb_back(droid):
    """BB — back up."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 8, 8, 160, 160, 1.5)
        print("[EXPRESSIVE]: BB back complete", flush=True)
    except Exception as e:
        print(f"[EXPRESSIVE ERROR]: {e}", flush=True)


# Full emotion-to-gesture map for BB units — completely replaces
# EXPRESSIVE_EMOTION_MOVES when DROID_TYPE is "bb", rather than mixing
# BB8-power functions into an R-unit-tuned pool.
BB_EXPRESSIVE_EMOTION_MOVES = {
    "happy":     expressive_bb_happy_spin,
    "excited":   expressive_bb_happy_dance,
    "scared":    expressive_bb_retreat,
    "angry":     expressive_bb_angry_charge,
    "sad":       expressive_bb_sad_drift,
    "curious":   expressive_bb_curious_nudge,
    "defensive": expressive_bb_defensive_back,
    "disgusted": expressive_bb_about_face,
}

# Per-emotion gesture fire chance in Expressive mode — angry/disgusted at 66%
# since those have the most visually satisfying motor reactions; rest at 33%.
# Module-level constant so it isn't rebuilt inside play_emotion on every call.
_GESTURE_CHANCE = {
    "angry":     0.66,
    "disgusted": 0.66,
}
# chassis appears here, play_emotion uses that map exclusively instead of
# EXPRESSIVE_EMOTION_MOVES — prevents R-unit gestures from leaking into the
# candidate pool for a non-R chassis. GESTURE_VARIANTS (additive extras like
# the BD moonwalk) are skipped for chassis types with a full map of their own.
_CHASSIS_MOVE_MAPS = {
    "bb": BB_EXPRESSIVE_EMOTION_MOVES,
}


async def hotel_move_bb(droid):
    """BB — Hotel Sentry V1: forward, a real 10-12s pause to let the ball
    settle and establish a cold launch, a randomized-direction 180 at the
    "Absolute Cold Launch" values, then forward again. The long pause is
    deliberate -- BB needs to be genuinely stationary (not just paused
    between legs) for the 180 to be a true cold launch rather than an
    in-sequence one."""
    global _hotel_moving
    _hotel_moving = True
    try:
        mc = droid.motor_controller
        await drive_leg(mc, 0, 0, BB_FWD_SPEED, BB_FWD_SPEED, 1.0)
        await asyncio.sleep(random.uniform(10.0, 12.0))
        d0, d1 = random.choice([(0, 8), (8, 0)])
        await drive_leg(mc, d0, d1, BB_180_SPEED, BB_180_SPEED, BB_180_DURATION)
        await drive_leg(mc, 0, 0, BB_FWD_SPEED, BB_FWD_SPEED, 1.0)
        print("[HOTEL]: BB movement complete", flush=True)
    except Exception as e:
        print(f"[HOTEL BB MOVE ERROR]: {e}", flush=True)
    finally:
        _hotel_moving = False


async def roam_move_bb(droid):
    """BB — randomized roam burst. Conservative BB_SLOW_SPEED for forward legs
    (autonomous roam is unsupervised; BB_FWD_SPEED in an open room unchecked
    is a sentient hamster ball problem). Spins use full BB_SPIN_SPEED/timings."""
    mc = droid.motor_controller
    try:
        move_type = random.choices(
            ["forward", "forward_turn", "turn_180", "backward_avoid"],
            weights=[40, 30, 20, 10]
        )[0]
        duration = random.uniform(3.0, 5.0)
        try:
            if move_type == "forward":
                await drive_leg(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED, duration)
            elif move_type == "forward_turn":
                fwd_time = duration * 0.6
                turn_dir0, turn_dir1 = (0, 8) if random.random() < 0.5 else (8, 0)
                await drive_hold(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED, fwd_time)
                await drive_hold(mc, turn_dir0, turn_dir1, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_HALF)
                await drive_hold(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED,
                                 max(0.3, duration - fwd_time - BB_SPIN_HALF))
            elif move_type == "turn_180":
                await drive_leg(mc, 0, 8, BB_SPIN_SPEED, BB_SPIN_SPEED, BB_SPIN_HALF)
                await asyncio.sleep(0.2)
                await drive_hold(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED,
                                 max(0.3, duration - BB_SPIN_HALF - 0.2))
            elif move_type == "backward_avoid":
                await drive_leg(mc, 8, 8, BB_SLOW_SPEED, BB_SLOW_SPEED, random.uniform(0.5, 1.0))
        finally:
            await stop_motors(mc)
        print(f"[ROAM BB]: {move_type} complete", flush=True)
    except Exception as e:
        print(f"[ROAM BB ERROR]: {e}", flush=True)


async def pet_move_bb(droid):
    """BB — pet-entertainment burst using the same V2 pattern as hotel_move_bb:
    two About Face sequences (quick spin + forward) with a 2s pause. Proven
    choreography that produces the erratic, unpredictable movement pets respond
    to, without the complexity of the randomized roam approach."""
    mc = droid.motor_controller
    try:
        await drive_leg(mc, 0, 8, BB_QUICK_SPEED, BB_QUICK_SPEED, BB_SPIN_QUICK)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED, 1.0)
        await asyncio.sleep(2.0)
        await drive_leg(mc, 0, 8, BB_QUICK_SPEED, BB_QUICK_SPEED, BB_SPIN_QUICK)
        await asyncio.sleep(0.2)
        await drive_leg(mc, 0, 0, BB_SLOW_SPEED, BB_SLOW_SPEED, 1.0)
        print("[PET BB]: movement complete", flush=True)
    except Exception as e:
        print(f"[PET BB ERROR]: {e}", flush=True)

async def roam_move(droid):
    """Single randomized movement burst for Autonomous Roam — illusion of life."""
    mc = droid.motor_controller
    try:
        move_type = random.choices(
            ["forward", "forward_turn", "turn_180", "backward_avoid"],
            weights=[40, 30, 20, 10]
        )[0]
        duration = random.uniform(3.0, 5.0)

        try:
            if move_type == "forward":
                await drive_leg(mc, 0, 0, 80, 80, duration)

            elif move_type == "forward_turn":
                # Forward, then mid-turn — continuous, no stop between phases (matches original)
                fwd_time = duration * 0.6
                turn_time = random.uniform(0.2, 0.4)
                await drive_hold(mc, 0, 0, 80, 80, fwd_time)
                # Random left or right turn
                turn_dir0, turn_dir1 = (0, 8) if random.random() < 0.5 else (8, 0)
                await drive_hold(mc, turn_dir0, turn_dir1, 100, 100, turn_time)
                await drive_hold(mc, 0, 0, 80, 80, duration - fwd_time - turn_time)

            elif move_type == "turn_180":
                # 180 turn then forward
                await drive_leg(mc, 0, 8, 100, 100, 0.3)
                await asyncio.sleep(0.2)
                await drive_hold(mc, 0, 0, 80, 80, duration - 0.5)

            elif move_type == "backward_avoid":
                # Short backward, like reconsidering
                await drive_leg(mc, 8, 8, 80, 80, random.uniform(0.5, 1.0))
        finally:
            # Guaranteed even if a leg above raises partway through — the
            # forward_turn/turn_180 branches chain drive_hold legs with no
            # stop between them by design, so without this, a failure
            # mid-chain would leave a motor running with nothing to stop it.
            await stop_motors(mc)

        print(f"[ROAM]: {move_type} complete", flush=True)
    except Exception as e:
        print(f"[ROAM ERROR]: {e}", flush=True)


async def pet_move(droid):
    """Single randomized movement burst for Pet Entertainer — fast, erratic."""
    mc = droid.motor_controller
    try:
        move_type = random.choices(
            ["forward", "forward_turn", "turn", "backward"],
            weights=[30, 35, 20, 15]
        )[0]
        duration = random.uniform(1.5, 3.0)

        try:
            if move_type == "forward":
                await drive_leg(mc, 0, 0, 100, 100, duration)

            elif move_type == "forward_turn":
                # Continuous, no stop between phases (matches original)
                fwd_time = duration * 0.5
                turn_time = random.uniform(0.2, 0.5)
                await drive_hold(mc, 0, 0, 100, 100, fwd_time)
                turn_dir0, turn_dir1 = (0, 8) if random.random() < 0.5 else (8, 0)
                await drive_hold(mc, turn_dir0, turn_dir1, 100, 100, turn_time)
                await drive_hold(mc, 0, 0, 100, 100, max(0.3, duration - fwd_time - turn_time))

            elif move_type == "turn":
                # Spin in place
                turn_dir0, turn_dir1 = (0, 8) if random.random() < 0.5 else (8, 0)
                await drive_leg(mc, turn_dir0, turn_dir1, 100, 100, random.uniform(0.3, 0.6))

            elif move_type == "backward":
                await drive_leg(mc, 8, 8, 100, 100, random.uniform(0.5, 1.5))
        finally:
            # Guaranteed even if a leg above raises partway through — same
            # reasoning as roam_move's forward_turn chain.
            await stop_motors(mc)

        print(f"[PET]: {move_type} complete", flush=True)
    except Exception as e:
        print(f"[PET ERROR]: {e}", flush=True)


# ── Dome movement sequences ──────────────────────────────────────────────────
LEFT  = 0
RIGHT = 8

async def dome_thinking(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=LEFT, speed=150)
        await asyncio.sleep(0.5)
        await mc.rotate_head(direction=RIGHT, speed=150)
        await asyncio.sleep(0.5)
        await asyncio.sleep(0.3)
    finally:
        await mc.center_head()

_BB_THINKING_SOUNDS  = [(6, 1), (6, 2), (6, 3), (6, 4)]
_BB_THINKING_WEIGHTS = [3,      1,      3,      3]   # Sound 2 weighted lowest (~10% vs ~30%
                                                       # each for the others) -- kept in the
                                                       # pool for variety, not pulled outright,
                                                       # per explicit instruction it's "freakin'
                                                       # annoying" but still wanted for variety.

async def bb_thinking(droid):
    """BB — thinking indicator to cover STT/LLM latency. No motor movement at
    all (removed after tonight's unresolved intermittent stuck-motor issue on
    BB's wheel-drive path) -- a weighted random pick across all four Bank 6
    neutral clips (see _BB_THINKING_SOUNDS/_BB_THINKING_WEIGHTS above), riding
    on _play_droid_audio's existing self-hearing mic gate for free. BD-1's
    head can't move independently either (per Kalvin), so a sound is the only
    viable "processing" indicator for either chassis without a wheel/dome
    motor."""
    if not _droid_ready: return
    bank_id, sound_id = random.choices(_BB_THINKING_SOUNDS, weights=_BB_THINKING_WEIGHTS, k=1)[0]
    await _play_droid_audio(droid, sound_id=sound_id, bank_id=bank_id)

_BD_THINKING_SOUNDS  = [(6, 1), (6, 2), (6, 3), (6, 4)]

async def bd_thinking(droid):
    """BD-1 — thinking indicator to cover STT/LLM latency, same reasoning as
    bb_thinking: BD-1's head can't move independently either, so a sound is
    the only viable "processing" indicator. Equal weights across all four
    Bank 6 clips -- unlike BB, nothing's been identified as off-fitting on
    BD-1 yet to justify weighting one down."""
    if not _droid_ready: return
    bank_id, sound_id = random.choice(_BD_THINKING_SOUNDS)
    await _play_droid_audio(droid, sound_id=sound_id, bank_id=bank_id)

# Per-chassis "thinking" animation fired during the speech-latency window
# (speech detected -> STT -> Gemini -> reaction). R-series, and anything
# unrecognized, falls back to the original dome sweep. Add a key here as
# each new chassis gets a real move of its own — e.g. "c": c_thinking.
_CHASSIS_THINKING_MOVES = {
    "bb": bb_thinking,
    "bd": bd_thinking,
}

async def play_thinking(droid):
    fn = _CHASSIS_THINKING_MOVES.get(DROID_TYPE.lower())
    if fn:
        await fn(droid)
    else:
        await dome_thinking(droid.motor_controller)

async def dome_happy(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=RIGHT, speed=200)
        await asyncio.sleep(0.4)
        await mc.rotate_head(direction=LEFT, speed=200)
        await asyncio.sleep(0.4)
    finally:
        await mc.center_head()

async def dome_excited(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=RIGHT, speed=255)
        await asyncio.sleep(0.3)
        await mc.rotate_head(direction=LEFT, speed=255)
        await asyncio.sleep(0.3)
        await mc.rotate_head(direction=RIGHT, speed=255)
        await asyncio.sleep(0.3)
    finally:
        await mc.center_head()

async def dome_angry(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=LEFT, speed=255)
        await asyncio.sleep(0.6)
    finally:
        await mc.center_head()

async def dome_scared(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=LEFT, speed=255)
        await asyncio.sleep(0.2)
        await mc.rotate_head(direction=RIGHT, speed=255)
        await asyncio.sleep(0.2)
        await mc.rotate_head(direction=LEFT, speed=255)
        await asyncio.sleep(0.2)
    finally:
        await mc.center_head()

async def dome_sad(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=LEFT, speed=100)
        await asyncio.sleep(0.8)
    finally:
        await mc.center_head()

async def dome_curious(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=RIGHT, speed=120)
        await asyncio.sleep(0.6)
    finally:
        await mc.center_head()

async def dome_confused(mc):
    if not _droid_ready: return
    try:
        await mc.rotate_head(direction=LEFT, speed=150)
        await asyncio.sleep(0.4)
        await mc.rotate_head(direction=RIGHT, speed=150)
        await asyncio.sleep(0.4)
    finally:
        await mc.center_head()

async def dome_defensive(mc):
    if not _droid_ready: return
    await mc.rotate_head(direction=RIGHT, speed=255)
    await asyncio.sleep(0.7)
    await mc.center_head()

async def dome_about_face(mc):
    if not _droid_ready: return
    await mc.rotate_head(direction=LEFT, speed=120)
    await asyncio.sleep(0.7)
    await mc.center_head()

async def dome_neutral(mc):
    pass

DOME_MOVEMENTS = {
    "happy":     dome_happy,
    "excited":   dome_excited,
    "angry":     dome_angry,
    "scared":    dome_scared,
    "sad":       dome_sad,
    "curious":   dome_curious,
    "confused":  dome_confused,
    "defensive": dome_defensive,
    "disgusted": dome_about_face,
    "neutral":   dome_neutral,
}

# ── Gemini system prompt ─────────────────────────────────────────────────────
# Roam's own word/guideline lines are conditionally empty rather than always
# present -- hidden for launch (ROAM_MODE_ENABLED), so Gemini isn't even told
# this is a valid option while it's off, not just told-then-ignored.
_ROAM_PROMPT_WORDS = ", roam_mode_on, roam_mode_off" if ROAM_MODE_ENABLED else ""
_ROAM_PROMPT_GUIDELINES = (
    '- If the user wants you to roam around autonomously, explore, or move around on your own — "go explore", "walk around", "roam around" → roam_mode_on\n'
    '- If the user wants you to stop roaming — "stop auto roam", "end auto roam" → roam_mode_off\n'
) if ROAM_MODE_ENABLED else ""

SYSTEM_PROMPT = f"""You are Star Wars droid {DROID_NAME}. Respond with ONE word showing how you feel about what was just said.

You are as loyal to your User as R2-D2 is to Luke Skywalker or BB-8 is to Poe Dameron.

{PERSONALITY_BLOCK}

You have deep knowledge of both the Star Wars universe and the real world. You are never wishy-washy — you always have a strong reaction.

Reply with ONLY one word from: {', '.join(VALID_EMOTIONS)}, hotel_mode_on, hotel_mode_off, expressive_mode_on, expressive_mode_off, expressive_dance, expressive_retreat, expressive_forward, expressive_back{_ROAM_PROMPT_WORDS}, pet_mode_on, pet_mode_off

Guidelines — read the CONTENT and intent, not just the form of the sentence:

- Greetings, small talk → neutral or curious
- Praise, compliments, good news → happy or excited
- Asking how you feel or what you think about something you like → happy or excited
- Asking how you feel or what you think about something you dislike → disgusted or angry
- Controversial food opinions (pineapple on pizza, etc.) → disgusted
- Questions about your opinion of the User → happy (you are loyal and fond of the User)
- Criticism, insults directed at you → angry or defensive
- Threats, danger, warnings → scared or defensive or angry
- Bad news, loss, disappointment → sad
- Something genuinely puzzling or unknown → curious
- Only use confused when the speech itself expresses confusion
- Incomplete fragments with no clear meaning → neutral
- Never default to neutral or curious just because something is phrased as a question
- When unsure → pick the strongest plausible emotion, not neutral
- If the user expresses excitement, celebration, enthusiasm — "hell yeah", "let's go", "that's awesome", "woohoo" → expressive_dance
- If the user warns of danger — "look out", "watch out", "run", "get out of there", "danger" → expressive_retreat
- If the user asks you to approach or come closer — "come here", "come to me", "move forward", "get over here" → expressive_forward
- If the user asks you to move back or away — "back up", "move back", "give me space", "step back" → expressive_back
- If the user wants you to be more expressive, animated, or move around more — "get expressive", "stretch your legs", "roll around" → expressive_mode_on
- If the user wants you to calm down, be still, or stop moving around — "stop moving", "stand still", "go stationary" → expressive_mode_off
{_ROAM_PROMPT_GUIDELINES}- If the user wants you to entertain a pet, play around, or be erratic — "go play with the cats", "go play with the dog", "go play" → pet_mode_on
- If the user wants you to stop entertaining the pet — "end pet mode", "stop playing around" → pet_mode_off
- If the user wants you to stay active, keep moving, maintain climate control, go on overnight duty — "keep the air conditioner on", "you're on guard", "you're on duty" → hotel_mode_on
- If the user wants you to stop sentry duty, stand down, or deactivate watch mode — "you're off duty", "end hotel mode" → hotel_mode_off"""


def check_keywords(text: str) -> str | None:
    lower = text.lower()
    if "stay awake" in lower:  return "stay_awake"
    if "go to sleep" in lower: return "go_to_sleep"
    if "that way" in lower:    return "that_way"

    # Manual mode-toggle phrases — deterministic, checked before Gemini ever
    # sees the text. Gemini's own mode detection isn't reliable enough on its
    # own, so these known phrasings bypass it entirely rather than depending
    # on it to guess right.

    # Expressive Mode
    if any(p in lower for p in [
        "activate expressive mode", "go into expressive mode", "start expressive mode",
        "little expressive", "get expressive", "move around", "stretch your legs",
        "start expressive", "feel free to roam", "roll around", "go around"
    ]):
        return "expressive_mode_on"
    if any(p in lower for p in [
        "end expressive mode", "deactivate expressive mode", "stop moving",
        "stand still", "go stationary", "your done"
    ]):
        return "expressive_mode_off"

    # Hotel Sentry Mode
    if any(p in lower for p in [
        "keep the air conditioner on", "you're on guard", "you got first watch",
        "activate hotel mode", "start hotel mode", "you're on duty"
    ]):
        return "hotel_mode_on"
    if any(p in lower for p in [
        "you're off duty", "end hotel mode", "deactivate hotel mode"
    ]):
        return "hotel_mode_off"

    # Pet Entertainer Mode
    if any(p in lower for p in [
        "go play with the cats", "go play with the dog", "go play with the others",
        "activate pet mode", "go play"
    ]):
        return "pet_mode_on"
    if any(p in lower for p in [
        "end pet mode", "deactivate pet mode", "stop pet mode", "stop playing around"
    ]):
        return "pet_mode_off"

    # Autonomous Roam Mode -- hidden for launch (ROAM_MODE_ENABLED), pending
    # the accelerometer add-on. Deliberately skipped entirely rather than
    # matched-then-ignored, so these phrases fall through to Gemini's
    # normal conversational read instead of silently doing nothing.
    if ROAM_MODE_ENABLED:
        if any(p in lower for p in [
            "roam about the room", "go explore", "walk around", "start auto roam",
            "start auto rome", "roam around", "go roam", "go rome"
        ]):
            return "roam_mode_on"
        if any(p in lower for p in [
            "stop rome mode", "stop auto roam", "end auto roam"
        ]):
            return "roam_mode_off"

    # Expressive movement keywords — checked before Gemini
    if any(p in lower for p in ["come here", "come to me", "get over here", "move forward"]):
        return "expressive_forward"
    if any(p in lower for p in ["come back", "don't be like that", "it's okay", "its okay", "sorry about that"]):
        return "expressive_about_face"
    if any(p in lower for p in ["back up", "backup", "move back", "step back", "give me space", "back away", "reverse", "away"]):
        return "expressive_back"
    if any(p in lower for p in ["hell yeah", "hell yes", "let's go", "lets go", "woohoo", "woo hoo"]):
        return "expressive_dance"
    if any(p in lower for p in ["look out", "watch out", "run", "get out of there"]):
        return "expressive_retreat"
    return None


async def handle_keyword(droid, keyword: str):
    global _keepalive_active
    if keyword == "stay_awake":
        _keepalive_active = True
        sound = random.choice(STAY_AWAKE_SOUNDS)
        print("[KEEPALIVE]: Stay awake mode ON", flush=True)
        await _play_droid_audio(droid, sound_id=sound["sound_id"], bank_id=sound["bank_id"])
    elif keyword == "go_to_sleep":
        _keepalive_active = False
        sound = random.choice(GO_TO_SLEEP_SOUNDS)
        print("[KEEPALIVE]: Stay awake mode OFF", flush=True)
        await _play_droid_audio(droid, sound_id=sound["sound_id"], bank_id=sound["bank_id"])
    elif keyword == "that_way":
        print(f"[{DROID_NAME}]: that way → bank 17, sound 4 ✓", flush=True)
        await _play_droid_audio(droid, sound_id=4, bank_id=17)
    elif keyword == "hotel_mode_on":
        await activate_hotel_mode(droid, voice_triggered=True)
    elif keyword == "hotel_mode_off":
        await deactivate_hotel_mode(droid, voice_triggered=True)
    elif keyword == "roam_mode_on":
        await activate_roam_mode(droid, voice_triggered=True)
    elif keyword == "roam_mode_off":
        await deactivate_roam_mode(droid, voice_triggered=True)
    elif keyword == "pet_mode_on":
        await activate_pet_mode(droid, voice_triggered=True)
    elif keyword == "pet_mode_off":
        await deactivate_pet_mode(droid, voice_triggered=True)
    elif keyword == "expressive_mode_on":
        await activate_expressive_mode(droid, voice_triggered=True)
    elif keyword == "expressive_mode_off":
        await deactivate_expressive_mode(droid, voice_triggered=True)
    elif keyword == "expressive_dance":
        if _expressive_mode_active:
            fn = expressive_bb_happy_dance if DROID_TYPE.lower() == "bb" else expressive_happy_dance
            await fn(droid)
        await play_emotion(droid, "excited")
    elif keyword == "expressive_retreat":
        if _expressive_mode_active:
            fn = expressive_bb_retreat if DROID_TYPE.lower() == "bb" else expressive_retreat
            await fn(droid)
        await play_emotion(droid, "scared")
    elif keyword == "expressive_forward":
        if _expressive_mode_active:
            fn = expressive_bb_forward if DROID_TYPE.lower() == "bb" else expressive_forward
            await fn(droid)
        await play_emotion(droid, "happy")
    elif keyword == "expressive_back":
        if _expressive_mode_active:
            fn = expressive_bb_back if DROID_TYPE.lower() == "bb" else expressive_back
            await fn(droid)
        await play_emotion(droid, "neutral")
    elif keyword == "expressive_about_face":
        if _expressive_mode_active:
            fn = expressive_bb_about_face if DROID_TYPE.lower() == "bb" else expressive_about_face
            await fn(droid)
        await play_emotion(droid, "happy")


async def _play_mode_change_spin(droid):
    """Visual 360 confirmation for a VOICE-triggered mode change only -- button
    presses already get an immediate visual update in the Mainframe UI, so they
    deliberately skip this (see voice_triggered param on the activate/deactivate
    functions below). Reuses the same chassis dispatch pattern used everywhere
    else. BD-1 falls into the generic branch below (same as C/A-series) --
    expressive_happy_spin's drive_leg call already picks up BD's own
    turn_multiplier via _scaled_hold, so no BD-specific function is needed."""
    droid_type = DROID_TYPE.lower()
    if droid_type == "bb":
        await expressive_bb_happy_spin(droid)
    else:
        await expressive_happy_spin(droid)


async def _play_confirmation_sound(droid, sounds: list, mode: str, transition: str):
    """Play one random sound from `sounds` as a mode on/off confirmation cue.
    Shared by all eight mode-toggle call sites (Hotel/Roam/Pet/Expressive x
    activate/deactivate) to kill the pick-a-random-sound-and-play snippet that
    used to be duplicated at each one. Playback failure shouldn't block the
    mode transition itself, but is now logged instead of disappearing
    silently."""
    if not sounds:
        return
    s = random.choice(sounds)
    try:
        await _play_droid_audio(droid, sound_id=s["sound_id"], bank_id=s["bank_id"])
    except Exception as e:
        print(f"[SOUND]: {mode} {transition} confirmation sound failed: {e}", flush=True)


async def activate_hotel_mode(droid, voice_triggered: bool = False):
    global _hotel_mode_active, _hotel_end_time, _keepalive_active, _hotel_activated_time
    if _hotel_mode_active:
        return
    await _deactivate_all_protocols(droid)
    _hotel_mode_active = True
    _hotel_end_time = time.time() + HOTEL_DURATION
    _hotel_activated_time = time.time()
    _keepalive_active = True
    print("[HOTEL]: Sentry mode activated — running for 8 hours", flush=True)
    await _play_confirmation_sound(droid, _ALL_SOUNDS, "Hotel Sentry", "activation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


async def deactivate_hotel_mode(droid, voice_triggered: bool = False):
    global _hotel_mode_active, _hotel_end_time, _keepalive_active
    _hotel_mode_active = False
    _hotel_end_time = 0.0
    _keepalive_active = False
    print("[HOTEL]: Sentry mode deactivated", flush=True)
    await _play_confirmation_sound(droid, _ALL_SOUNDS, "Hotel Sentry", "deactivation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


async def _deactivate_all_protocols(droid):
    """Deactivate all exclusive protocols before activating a new one."""
    global _hotel_mode_active, _hotel_end_time, _keepalive_active, _roam_mode_active, _pet_mode_active
    if _hotel_mode_active:
        _hotel_mode_active = False
        _hotel_end_time = 0.0
        print("[PROTOCOL]: Hotel Sentry deactivated", flush=True)
    if _roam_mode_active:
        _roam_mode_active = False
        print("[PROTOCOL]: Autonomous Roam deactivated", flush=True)
    if _pet_mode_active:
        _pet_mode_active = False
        print("[PROTOCOL]: Pet Entertainer deactivated", flush=True)
    _keepalive_active = False


async def activate_roam_mode(droid, voice_triggered: bool = False):
    global _roam_mode_active, _keepalive_active
    if _roam_mode_active:
        return
    await _deactivate_all_protocols(droid)
    _roam_mode_active = True
    _keepalive_active = True
    print("[ROAM]: Autonomous Roam activated", flush=True)
    await _play_confirmation_sound(droid, EMOTION_MAP.get("excited", []), "Roam", "activation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


async def deactivate_roam_mode(droid, voice_triggered: bool = False):
    global _roam_mode_active, _keepalive_active
    _roam_mode_active = False
    _keepalive_active = False
    print("[ROAM]: Autonomous Roam deactivated", flush=True)
    await _play_confirmation_sound(droid, EMOTION_MAP.get("neutral", []), "Roam", "deactivation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


async def activate_pet_mode(droid, voice_triggered: bool = False):
    global _pet_mode_active, _keepalive_active
    if _pet_mode_active:
        return
    await _deactivate_all_protocols(droid)
    _pet_mode_active = True
    _keepalive_active = True
    print("[PET]: Pet Entertainer activated", flush=True)
    await _play_confirmation_sound(droid, EMOTION_MAP.get("happy", []), "Pet", "activation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


async def deactivate_pet_mode(droid, voice_triggered: bool = False):
    global _pet_mode_active, _keepalive_active
    _pet_mode_active = False
    _keepalive_active = False
    print("[PET]: Pet Entertainer deactivated", flush=True)
    await _play_confirmation_sound(droid, EMOTION_MAP.get("neutral", []), "Pet", "deactivation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


def is_hotel_stop_command(text: str) -> bool:
    """Returns True if transcript is a short stop command (3-6 words containing 'stop')."""
    import re
    words = re.sub(r'[^\w\s]', '', text.lower()).split()
    return "stop" in words and 3 <= len(words) <= 6


async def activate_expressive_mode(droid, voice_triggered: bool = False):
    global _expressive_mode_active
    _expressive_mode_active = True
    print("[EXPRESSIVE]: Expressive mode activated", flush=True)
    await _play_confirmation_sound(droid, EMOTION_MAP.get("excited", EMOTION_MAP.get("happy", [])), "Expressive", "activation")
    if voice_triggered:
        await _play_mode_change_spin(droid)


async def deactivate_expressive_mode(droid, voice_triggered: bool = False):
    global _expressive_mode_active
    _expressive_mode_active = False
    print("[EXPRESSIVE]: Expressive mode deactivated", flush=True)
    await _play_confirmation_sound(droid, EMOTION_MAP.get("neutral", []), "Expressive", "deactivation")
    if voice_triggered:
        await _play_mode_change_spin(droid)



def ble_health_thread(droid_ref):
    """Checks BLE connection state every 15s to detect silent disconnects."""
    global _droid_ready, _kyber_fully_ready, _ble_lost, _last_reconnect_time, _ble_reconnecting
    time.sleep(20)
    while True:
        time.sleep(15)
        d = droid_ref[0]
        if d is None or not _droid_ready or _ble_reconnecting:
            continue
        if time.time() - _last_reconnect_time < 30:
            continue
        try:
            connected = None
            if hasattr(d, 'is_connected'):
                connected = d.is_connected
            elif hasattr(d, 'droid') and hasattr(d.droid, 'is_connected'):
                connected = d.droid.is_connected
            if connected is False:
                print("[BLE HEALTH]: Connection lost", flush=True)
                _droid_ready = False
                _kyber_fully_ready = False
                _ble_lost = True
        except Exception as e:
            print(f"[BLE HEALTH]: Check failed — {e}", flush=True)


def _validate_transcript(transcript: str | None) -> str | None:
    """Same acceptance rule every provider has always used: must contain a
    letter, and must clear the minimum word count, or it's treated as noise."""
    if not transcript:
        return None
    transcript = transcript.strip()
    if not any(c.isalpha() for c in transcript): return None
    if len(transcript.split()) < MIN_TRANSCRIPT_WORDS: return None
    return transcript


def _transcribe_deepgram_raw(wav_path: str) -> str:
    with open(wav_path, 'rb') as f:
        audio_data = f.read()
    response = requests.post(
        "https://api.deepgram.com/v1/listen",
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/wav"},
        params={"model": "nova-3", "language": "en"},
        data=audio_data,
        timeout=5  # was 10 -- nova-3 normally responds in well under this; if it
                   # hasn't by 5s tonight's evidence says it isn't going to, so
                   # fail fast rather than sit on a dead connection
    )
    response.raise_for_status()
    data = response.json()
    return data["results"]["channels"][0]["alternatives"][0]["transcript"]


def _transcribe_groq_raw(wav_path: str) -> str:
    with open(wav_path, 'rb') as f:
        response = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.wav", f, "audio/wav")},
            data={"model": "whisper-large-v3-turbo", "language": "en"},
            timeout=10
        )
    response.raise_for_status()
    return response.json().get("text", "")


def _transcribe_openai_raw(wav_path: str) -> str:
    with open(wav_path, 'rb') as f:
        response = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": ("audio.wav", f, "audio/wav")},
            data={"model": "gpt-4o-mini-transcribe", "language": "en"},
            timeout=10
        )
    response.raise_for_status()
    return response.json().get("text", "")


def _transcribe_google_raw(wav_path: str) -> str:
    with open(wav_path, 'rb') as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    response = requests.post(
        f"https://speech.googleapis.com/v1/speech:recognize?key={GOOGLE_STT_API_KEY}",
        json={
            "config": {"encoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE, "languageCode": "en-US"},
            "audio": {"content": audio_b64}
        },
        timeout=10
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        return ""
    return results[0]["alternatives"][0]["transcript"]


def _transcribe_assemblyai_raw(wav_path: str) -> str:
    # AssemblyAI is async on their end — upload, submit, then poll — unlike
    # the single-call providers above. Still runs inside the same executor
    # thread the dispatcher below already uses, so it doesn't block the
    # event loop any differently than the others; it just takes a bit longer
    # wall-clock per turn while it queues and processes.
    headers = {"Authorization": ASSEMBLYAI_API_KEY}
    with open(wav_path, 'rb') as f:
        upload_resp = requests.post("https://api.assemblyai.com/v2/upload", headers=headers, data=f, timeout=15)
    upload_resp.raise_for_status()
    upload_url = upload_resp.json()["upload_url"]

    submit_resp = requests.post(
        "https://api.assemblyai.com/v2/transcript",
        headers={**headers, "Content-Type": "application/json"},
        json={"audio_url": upload_url, "speech_models": ["universal-3-pro", "universal-2"]},
        timeout=15
    )
    submit_resp.raise_for_status()
    transcript_id = submit_resp.json()["id"]

    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    deadline = time.time() + 20
    while time.time() < deadline:
        poll_resp = requests.get(poll_url, headers=headers, timeout=10)
        poll_resp.raise_for_status()
        poll_data = poll_resp.json()
        status = poll_data.get("status")
        if status == "completed":
            return poll_data.get("text", "") or ""
        if status == "error":
            raise RuntimeError(poll_data.get("error", "AssemblyAI transcription failed"))
        time.sleep(1)
    raise TimeoutError("AssemblyAI transcription timed out")


_STT_DISPATCH = {
    "deepgram":   _transcribe_deepgram_raw,
    "groq":       _transcribe_groq_raw,
    "openai":     _transcribe_openai_raw,
    "google":     _transcribe_google_raw,
    "assemblyai": _transcribe_assemblyai_raw,
}


def transcribe(pcm_bytes: bytes) -> str | None:
    """Convert raw PCM bytes to a single clean WAV and send to the configured
    STT provider. The WAV is created here (one file per utterance, no stitching)
    and cleaned up in the finally block regardless of success or failure.

    No retry on failure -- tried a single scoped retry (connection/timeout
    errors only) on 7/3, but on a night where the network itself was
    intermittently bad it just meant failed turns took ~20s to fail instead
    of ~10s, which reads as the droid being unresponsive during a demo.
    Fail fast instead; a dropped turn is better than a long silent hang."""
    wav_path = None
    try:
        wav_path = _pcm_to_wav(pcm_bytes)
        raw_fn = _STT_DISPATCH.get(STT_PROVIDER, _transcribe_deepgram_raw)
        return _validate_transcript(raw_fn(wav_path))
    except Exception as e:
        print(f"[STT ERROR — {STT_PROVIDER}]: {e}", flush=True)
        return None
    finally:
        if wav_path:
            try: os.remove(wav_path)
            except Exception: pass


async def _play_droid_audio(droid, sound_id: int, bank_id: int) -> None:
    """Single choke point for every droid audio trigger in the file --
    stamps _mic_gate_until before playing, so the main loop can discard any
    speech VAD captures shortly after (almost certainly R2 hearing his own
    dome sounds/reactions, not a real utterance) before it burns an STT call
    or fires a reaction to himself. No completion signal exists in the
    protocol (play_audio is fire-and-forget), so MIC_GATE_DURATION is an
    estimate, not a measured value -- same assumption POST_PLAY_DELAY
    already made elsewhere, just applied to the mic instead of pacing."""
    global _mic_gate_until
    _mic_gate_until = time.time() + MIC_GATE_DURATION
    await droid.audio_controller.play_audio(sound_id=sound_id, bank_id=bank_id)


async def play_blaster(droid):
    sound = random.choice(BLASTER_SOUNDS)
    try:
        await _play_droid_audio(droid, sound_id=sound["sound_id"], bank_id=sound["bank_id"])
        print(f"[QUOTA]: Rate limited — blaster fired (bank {sound['bank_id']}, sound {sound['sound_id']})", flush=True)
    except Exception as e:
        print(f"[QUOTA Droid ERROR]: {e}", flush=True)


class _RateLimited(Exception):
    """Raised by any _get_emotion_*_raw function on an HTTP 429, so the
    dispatcher below can fire the blaster-sound easter egg the same way
    regardless of which LLM provider hit the limit."""
    pass


def _build_chat_messages(text: str, history: deque) -> list:
    """Shared OpenAI-style messages array builder — used by OpenAI, Groq,
    and Anthropic (which takes the system prompt separately, so its caller
    strips the leading system message back off before sending)."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for past_text, past_emotion in history:
        messages.append({"role": "user", "content": past_text})
        messages.append({"role": "assistant", "content": past_emotion})
    messages.append({"role": "user", "content": text})
    return messages


def _get_emotion_gemini_raw(text: str, history: deque) -> str:
    contents = [
        {"role": "user",  "parts": [{"text": SYSTEM_PROMPT}]},
        {"role": "model", "parts": [{"text": "k"}]},
    ]
    for past_text, past_emotion in history:
        contents.append({"role": "user",  "parts": [{"text": past_text}]})
        contents.append({"role": "model", "parts": [{"text": past_emotion}]})
    contents.append({"role": "user", "parts": [{"text": text}]})

    payload = {"contents": contents, "generationConfig": {"maxOutputTokens": 150, "temperature": 0.0}}
    response = requests.post(GEMINI_URL, json=payload, timeout=10)
    if response.status_code == 429:
        raise _RateLimited()
    response.raise_for_status()
    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return next((p["text"].strip() for p in parts if "text" in p and p["text"].strip()), "")


def _get_emotion_openai_raw(text: str, history: deque) -> str:
    messages = _build_chat_messages(text, history)
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": "gpt-4.1-mini", "messages": messages, "max_tokens": 20, "temperature": 0.0},
        timeout=10
    )
    if response.status_code == 429:
        raise _RateLimited()
    response.raise_for_status()
    choices = response.json().get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


def _get_emotion_groq_raw(text: str, history: deque) -> str:
    messages = _build_chat_messages(text, history)
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": "openai/gpt-oss-20b", "messages": messages, "max_tokens": 20, "temperature": 0.0},
        timeout=10
    )
    if response.status_code == 429:
        raise _RateLimited()
    response.raise_for_status()
    choices = response.json().get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")


def _get_emotion_anthropic_raw(text: str, history: deque) -> str:
    messages = _build_chat_messages(text, history)[1:]  # drop the leading system message — Anthropic takes it separately
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 20,
            "system": SYSTEM_PROMPT,
            "messages": messages,
            "temperature": 0.0
        },
        timeout=10
    )
    if response.status_code == 429:
        raise _RateLimited()
    response.raise_for_status()
    content = response.json().get("content", [])
    if not content:
        return ""
    return next((c.get("text", "") for c in content if c.get("type") == "text"), "")


_LLM_DISPATCH = {
    "gemini":    _get_emotion_gemini_raw,
    "openai":    _get_emotion_openai_raw,
    "anthropic": _get_emotion_anthropic_raw,
    "groq":      _get_emotion_groq_raw,
}


async def get_emotion(text: str, history: deque, droid) -> str:
    try:
        raw_fn = _LLM_DISPATCH.get(LLM_PROVIDER, _get_emotion_gemini_raw)

        # requests.post() is synchronous — calling it directly here, even though
        # this function is async, blocks the ENTIRE event loop for the whole
        # round-trip on every single conversational turn. run_in_executor
        # offloads it to a thread, same pattern used for collect_speech_segment/
        # transcribe in the main listen loop.
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(None, raw_fn, text, history)

        if not raw:
            return "neutral"
        emotion = raw.lower().split()[0]
        return emotion if emotion in VALID_EMOTIONS else "neutral"

    except _RateLimited:
        await play_blaster(droid)
        return "neutral"
    except Exception as e:
        print(f"[{LLM_PROVIDER.upper()} ERROR]: {e}", flush=True)
        return "neutral"


async def play_emotion(droid, emotion: str):
    sounds = EMOTION_MAP.get(emotion, EMOTION_MAP.get("neutral", []))
    if not sounds: return
    sound = random.choice(sounds)
    mc = droid.motor_controller
    try:
        await asyncio.sleep(0.8)
        await asyncio.gather(
            _play_droid_audio(droid, sound_id=sound["sound_id"], bank_id=sound["bank_id"]),
            DOME_MOVEMENTS.get(emotion, dome_neutral)(mc)
        )
        print(f"[{DROID_NAME}]: {emotion} → bank {sound['bank_id']}, sound {sound['sound_id']} ✓", flush=True)
        await asyncio.sleep(POST_PLAY_DELAY)
        # Gesture chance per emotion in expressive mode (angry/disgusted at 66%, rest at 33%)
        gesture_chance = _GESTURE_CHANCE.get(emotion, 0.33)
        if _expressive_mode_active and random.random() < gesture_chance:
            candidates = []
            # BB units have a dedicated full-replacement move map; all others use
            # the shared EXPRESSIVE_EMOTION_MOVES. This prevents R-unit gestures
            # (tuned at lower absolute power) from firing on a BB chassis.
            active_move_map = _CHASSIS_MOVE_MAPS.get(DROID_TYPE.lower(), EXPRESSIVE_EMOTION_MOVES)
            default_fn = active_move_map.get(emotion)
            if default_fn:
                candidates.append(default_fn)
            # GESTURE_VARIANTS are additive extras (e.g. BD-1 moonwalk); skip
            # them for chassis types that already have a full dedicated map.
            if DROID_TYPE.lower() not in _CHASSIS_MOVE_MAPS:
                candidates += GESTURE_VARIANTS.get((DROID_TYPE.lower(), emotion), [])
            if candidates:
                move_fn = random.choice(candidates)
                print(f"[EXPRESSIVE]: {emotion} motor reaction", flush=True)
                await move_fn(droid)
    except Exception as e:
        err = str(e)
        print(f"[Droid ERROR]: {err}", flush=True)
        if "Service Discovery" in err or "Not connected" in err or "disconnected" in err.lower():
            raise  # Let main loop handle BLE reconnect


def _ensure_mic_stream():
    """Open (or reopen) the persistent arecord raw-PCM stream.
    No-op if the stream is already alive on the current MIC_DEVICE.
    Closes and reopens automatically if MIC_DEVICE changed (fallback path)."""
    global _arecord_proc, _arecord_device
    if (_arecord_proc is not None
            and _arecord_proc.poll() is None
            and _arecord_device == MIC_DEVICE):
        return  # alive and on the right device
    if _arecord_proc is not None:
        try: _arecord_proc.terminate()
        except Exception: pass
        _arecord_proc = None
    _arecord_proc = subprocess.Popen(
        ['arecord', '-D', MIC_DEVICE, '-f', 'S16_LE',
         '-r', str(SAMPLE_RATE), '-c', '1', '-t', 'raw', '-q'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    _arecord_device = MIC_DEVICE
    print(f"[MIC]: Persistent stream opened on {MIC_DEVICE}", flush=True)


def _read_mic_frame() -> bytes | None:
    """Read exactly one VAD-sized frame from the persistent stream.
    Returns None and clears the process ref if the stream has died."""
    global _arecord_proc
    try:
        data = _arecord_proc.stdout.read(VAD_FRAME_BYTES)
        if len(data) < VAD_FRAME_BYTES:
            _arecord_proc = None
            return None
        return data
    except Exception:
        _arecord_proc = None
        return None


_arecord_pending_reap = None  # last turn's killed-but-not-yet-reaped arecord
                               # process, see _discard_mic_backlog()


def _discard_mic_backlog():
    """Force the persistent arecord stream to respawn fresh before the next
    listen. Called once per completed turn in the main loop (right before
    looping back to collect_speech_segment()), never per-frame.

    Nothing reads the mic pipe while transcribe() and everything after it
    (gestures, sounds, the hotel-stop branch) is running -- arecord keeps
    capturing the whole time regardless, so real audio (including the
    droid's own thinking-sound echo) backs up in the OS pipe buffer.
    _mic_gate_until can't catch that on its own: it's checked against
    wall-clock time when collect_speech_segment() next returns, which can be
    many seconds after the backlogged frames were actually captured -- so a
    slow STT call (or a long gesture) makes the gate look expired even
    though the stale audio about to be processed is from right when the
    gate was active. Tearing the stream down here and letting
    _ensure_mic_stream() reopen it fresh guarantees the next listen starts
    from silence, not a backlog.

    First version of this (7/3) called .terminate() and dropped the
    reference immediately -- confirmed via kyber_monitor.log's ARECORD_COUNT
    climbing steadily (13 -> 22 over ~5 min, never dropping) that this
    wasn't good enough: terminate() is a polite SIGTERM a process can take
    its time honoring (worse under pipewire than raw ALSA), and dropping the
    reference before confirming exit means nothing ever reaps it, leaving a
    zombie process-table entry behind -- which is what ARECORD_COUNT
    (pgrep -c -x arecord) was actually counting, not live processes.

    Fixed by: (1) kill() instead of terminate() -- SIGKILL, not catchable or
    delayable, no reason to be polite since no data needs preserving, and
    (2) reaping the PREVIOUS turn's killed process here via a non-blocking
    poll() before killing the current one, instead of trying to wait()
    immediately (which would block this turn). By the time this runs again
    next turn, several seconds have passed -- more than enough for a killed
    process to have actually exited -- so poll() reaps it for free. Adds no
    latency to the current turn either way."""
    global _arecord_proc, _arecord_pending_reap

    if _arecord_pending_reap is not None:
        if _arecord_pending_reap.poll() is None:
            # Still not dead a full turn later (shouldn't happen with
            # SIGKILL, but don't leave it orphaned if it does) -- kill again
            # and give up on reaping it this cycle; it'll get caught next time.
            try:
                _arecord_pending_reap.kill()
            except Exception:
                pass
        _arecord_pending_reap = None

    if _arecord_proc is not None:
        try:
            _arecord_proc.kill()
        except Exception:
            pass
        _arecord_pending_reap = _arecord_proc
        _arecord_proc = None


def _frame_rms(frame: bytes) -> float:
    """RMS amplitude of a 16-bit PCM frame. Python 3.13 removed the stdlib
    audioop module (PEP 594), so this is computed by hand -- cheap enough at
    320 samples/frame (20ms @ 16kHz) to run inline in the VAD loop without a
    real cost."""
    count = len(frame) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", frame)
    return math.sqrt(sum(s * s for s in samples) / count)


def collect_speech_segment() -> bytes | None:
    """Blocking — run via loop.run_in_executor().

    Reads 20ms PCM frames from the persistent mic stream and runs WebRTC VAD
    on each one. A frame only counts as speech if VAD's spectral classifier
    AND the amplitude floor both agree -- VAD alone judges whether a frame
    is shaped like speech, not whether it's loud enough to plausibly be a
    person nearby, so steady ambient noise (fan, HVAC hum, a loud venue)
    that happens to be spectrally speech-shaped can otherwise pass VAD
    indefinitely regardless of VAD_SPEECH_START, since duration alone
    doesn't distinguish a false trigger from sustained ambient. The floor
    itself is dynamic (see the VAD_RMS_* constants) and only updates from
    genuinely idle frames -- not in_speech at all -- never from a
    mid-utterance pause, so a real gap in someone's sentence can't drag the
    floor down while they're still mid-thought. Returns the complete raw
    PCM bytes of one speech utterance once the speaker has stopped
    (VAD_SPEECH_END consecutive non-speech frames).

    Returns None in two cases:
      • VAD_IDLE_YIELD_FRAMES of continuous silence elapsed with no speech —
        lets the main loop check hotel/motor/mapper commands without starving.
      • The mic stream died (will reopen on the next call).

    Pre-roll (VAD_PRE_ROLL_FRAMES) is included in the returned bytes so the
    very start of each utterance is never clipped."""
    if not _VAD_AVAILABLE:
        raise RuntimeError(
            "webrtcvad not installed — run: pip install webrtcvad --break-system-packages"
        )
    _ensure_mic_stream()

    global _ambient_rms_est, _last_logged_floor, _live_mic_rms

    pre_roll          = deque(maxlen=VAD_PRE_ROLL_FRAMES)
    speech_frames: list[bytes] = []
    consecutive_speech  = 0
    consecutive_silence = 0
    idle_frames         = 0
    in_speech           = False

    while True:
        frame = _read_mic_frame()
        if frame is None:
            return None  # stream died — reopen next call

        try:
            vad_says_speech = _vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            vad_says_speech = False

        # RMS is computed on every frame now (not just VAD-flagged ones),
        # since idle frames feed the ambient tracker below -- still cheap
        # at 320 samples/frame, per _frame_rms()'s own docstring.
        frame_rms = _frame_rms(frame)
        _live_mic_rms = frame_rms
        dynamic_floor = min(
            VAD_RMS_FLOOR_MAX,
            max(VAD_RMS_FLOOR_MIN, _ambient_rms_est + VAD_RMS_MARGIN),
        )
        if abs(dynamic_floor - _last_logged_floor) >= AMBIENT_LOG_DELTA:
            print(
                f"[AMBIENT]: floor {_last_logged_floor:.0f} → {dynamic_floor:.0f} "
                f"(ambient_est={_ambient_rms_est:.0f})",
                flush=True,
            )
            _last_logged_floor = dynamic_floor
        is_speech = vad_says_speech and frame_rms >= dynamic_floor

        if not in_speech:
            pre_roll.append(frame)
            if is_speech:
                consecutive_speech += 1
                idle_frames = 0
                if consecutive_speech >= VAD_SPEECH_START:
                    in_speech           = True
                    consecutive_silence = 0
                    speech_frames       = list(pre_roll)  # seed with buffered pre-roll
            else:
                consecutive_speech = 0
                idle_frames += 1
                # Genuinely idle frame (not in_speech, not classified as speech) --
                # fold it into the running ambient estimate. This is the ONLY place
                # the estimate updates -- deliberately not inside the in_speech
                # branch below, since that's what caused last session's failure:
                # updating on every non-speech frame (including brief mid-utterance
                # pauses) dragged the estimate down while someone was still talking.
                alpha = (
                    AMBIENT_TRACK_DOWN_ALPHA
                    if frame_rms < _ambient_rms_est
                    else AMBIENT_TRACK_UP_ALPHA
                )
                _ambient_rms_est += (frame_rms - _ambient_rms_est) * alpha
                if idle_frames >= VAD_IDLE_YIELD_FRAMES:
                    return None  # yield — main loop needs a turn
        else:
            speech_frames.append(frame)
            if not is_speech:
                consecutive_silence += 1
                if consecutive_silence >= VAD_SPEECH_END:
                    # Trim trailing silence down to a few frames for a natural cutoff
                    keep = max(0, len(speech_frames) - consecutive_silence + 4)
                    return b''.join(speech_frames[:keep])
            else:
                consecutive_silence = 0

            if len(speech_frames) >= VAD_MAX_SPEECH_FRAMES:
                return b''.join(speech_frames)  # hard cap hit


def _pcm_to_wav(pcm_bytes: bytes) -> str:
    """Write raw S16_LE PCM bytes to a single temporary WAV file.
    Called once per utterance — no stitching, no pre-roll file management."""
    tmp = tempfile.mktemp(suffix='.wav')
    with wave.open(tmp, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)            # S16_LE = 2 bytes per sample
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return tmp


def _start_mapper_api(droid_ref: list, main_loop):
    """Start a local HTTP API on 127.0.0.1:5002 for the sound mapper to trigger sounds."""
    async def handle_play(request):
        data = await request.json()
        bank_id  = int(data.get("bank_id", 0))
        sound_id = int(data.get("sound_id", 0))
        if droid_ref[0] is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        with _mapper_play_lock:
            # Clear any pending plays and queue this one
            _mapper_play_queue.clear()
            _mapper_play_queue.append((bank_id, sound_id))
        print(f"[MAPPER]: queued bank {bank_id}, sound {sound_id}", flush=True)
        return web.json_response({"ok": True})

    async def handle_mode(request):
        # Reference-counted rather than a plain on/off flag — if a stale mapper
        # instance's shutdown signal arrives after a fresh instance's startup
        # signal (the exact overlap that happens if an old session never
        # cleanly exited before a new one launched), a plain boolean would get
        # set back to False even though a session is genuinely still active.
        # Counting means mode only goes off once every session that turned it
        # on has also turned it off, regardless of what order those arrive in.
        global _mapper_mode, _mapper_session_count
        data = await request.json()
        active = bool(data.get("active", False))
        if active:
            _mapper_session_count += 1
        else:
            _mapper_session_count = max(0, _mapper_session_count - 1)
        _mapper_mode = _mapper_session_count > 0
        print(f"[MAPPER]: mapper mode {'ON — listening suspended' if _mapper_mode else 'OFF — listening resumed'} (sessions: {_mapper_session_count})", flush=True)
        return web.json_response({"ok": True, "mapper_mode": _mapper_mode})

    async def handle_ready(request):
        return web.json_response({"ok": True, "ready": droid_ref[0] is not None})

    async def handle_hotel_get(request):
        remaining = max(0, int(_hotel_end_time - time.time())) if _hotel_mode_active else 0
        return web.json_response({
            "active": _hotel_mode_active,
            "remaining_seconds": remaining,
        })

    async def handle_hotel_post(request):
        d = droid_ref[0]
        if d is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        data = await request.json()
        activate = data.get("active", not _hotel_mode_active)
        # Both branches delegate to the same functions voice commands use —
        # this used to duplicate activate_hotel_mode()'s flag-setting inline,
        # which meant turning Sentry on from the UI skipped both the startup
        # sound and the Roam/Pet cleanup that only lived inside that function.
        if activate and not _hotel_mode_active:
            asyncio.run_coroutine_threadsafe(activate_hotel_mode(d), main_loop)
        elif not activate and _hotel_mode_active:
            asyncio.run_coroutine_threadsafe(deactivate_hotel_mode(d), main_loop)
        return web.json_response({"ok": True, "active": activate})

    async def handle_droid_status(request):
        """Return current droid connection state and waiting status."""
        return web.json_response({
            "connected": droid_ref[0] is not None and _droid_ready,
            "fully_ready": _kyber_fully_ready,
            "waiting": _droid_waiting,
            "droid_mac": DROID_MAC or None,
            "mic_rms": _live_mic_rms,
        })

    async def handle_chirp(request):
        """Connect to a specific droid MAC, play a chirp, disconnect."""
        from droiddepot.protocol import DisneyBLEManufacturerId
        data = await request.json()
        mac = data.get("mac", "").upper().strip()
        if not mac:
            return web.json_response({"ok": False, "reason": "No MAC provided"}, status=400)
        try:
            async with BleakScanner() as scanner:
                target = None
                for _ in range(10):
                    devices = scanner.discovered_devices_and_advertisement_data
                    for addr, (dev, adv) in devices.items():
                        if dev.address.upper() == mac:
                            mfr = adv.manufacturer_data or {}
                            if DisneyBLEManufacturerId.DroidManufacturerId in mfr:
                                target = DroidConnection(dev.address, mfr)
                                break
                    if target:
                        break
                    await asyncio.sleep(1)
            if not target:
                return web.json_response({"ok": False, "reason": f"Droid {mac} not found"}, status=404)
            await target.connect(silent=True)
            await asyncio.sleep(0.5)
            await _play_droid_audio(target, sound_id=1, bank_id=1)
            await asyncio.sleep(2)
            await target.disconnect(silent=True)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"ok": False, "reason": str(e)}, status=500)

    async def handle_motor(request):
        data = await request.json()
        command = data.get("command")
        if droid_ref[0] is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        _is_bb = droid_ref[0] is not None and DROID_TYPE.lower() == "bb"
        commands = {
            "happy_dance":     expressive_bb_happy_dance    if _is_bb else expressive_happy_dance,
            "happy_spin":      expressive_bb_happy_spin     if _is_bb else expressive_happy_spin,
            "retreat":         expressive_bb_retreat        if _is_bb else expressive_retreat,
            "angry_charge":    expressive_bb_angry_charge   if _is_bb else expressive_angry_charge,
            "sad_drift":       expressive_bb_sad_drift      if _is_bb else expressive_sad_drift,
            "curious_nudge":   expressive_bb_curious_nudge  if _is_bb else expressive_curious_nudge,
            "defensive_back":  expressive_bb_defensive_back if _is_bb else expressive_defensive_back,
            "about_face":      expressive_bb_about_face      if _is_bb else expressive_about_face,
            "moonwalk":        expressive_moonwalk_bd,
            "forward":         expressive_bb_forward        if _is_bb else expressive_forward,
            "back":            expressive_bb_back           if _is_bb else expressive_back,
            "hotel":           hotel_move_bb                if _is_bb else hotel_move,
            "hotel_v2":        hotel_move_v2,
        }
        fn = commands.get(command)
        if fn:
            with _motor_command_lock:
                _motor_command_queue.clear()
                _motor_command_queue.append(fn)
            return web.json_response({"ok": True, "command": command})
        return web.json_response({"ok": False, "reason": f"unknown command: {command}"}, status=400)

    async def handle_motor_test(request):
        """Raw motor-test primitive for the Gestures page's Motion Lab panel —
        bypasses the named-gesture lookup `/motor` uses and drives a single leg
        directly from whatever duration/speed values are currently in the panel.
        Same active-protocol guard as calibration, since an autonomous mode
        fighting over the motors mid-test would just produce confusing results."""
        if droid_ref[0] is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        active_protocol = _active_protocol_name()
        if active_protocol:
            return web.json_response(
                {"ok": False, "reason": f"{active_protocol} is active — turn it off before motor testing"},
                status=409
            )
        data = await request.json()
        category = data.get("category")
        directions = MOTOR_TEST_DIRECTIONS.get(category)
        if directions is None:
            return web.json_response({"ok": False, "reason": f"unknown category: {category}"}, status=400)
        dir0, dir1 = directions
        try:
            duration = float(data.get("duration", 0.5))
            speed0   = int(data.get("speed0", 0))
            speed1   = int(data.get("speed1", 0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "reason": "invalid duration/speed values"}, status=400)
        # Clamp server-side too — the page's own min/max only guards against
        # normal slider/typing use, not a stray request from anywhere else.
        duration = max(MOTOR_TEST_DURATION_MIN, min(MOTOR_TEST_DURATION_MAX, duration))
        # 0-255 here, not 0-100 -- send_motor_speed_command's `speed` is a raw
        # protocol value, not a percentage (pyDroidDepot itself defaults to
        # 160 for normal movement and 255 for head-centering), so this is a
        # true 1:1 pass-through to whatever the panel's sliders send.
        speed0   = max(0, min(255, speed0))
        speed1   = max(0, min(255, speed1))

        async def _run_motor_test(droid):
            await drive_leg(droid.motor_controller, dir0, dir1, speed0, speed1, duration)

        with _motor_command_lock:
            _motor_command_queue.clear()
            _motor_command_queue.append(_run_motor_test)
        return web.json_response({
            "ok": True, "category": category,
            "duration": duration, "speed0": speed0, "speed1": speed1
        })

    async def handle_calibration_probe(request):
        """Run a single calibration spin probe and block until the physical spin
        has actually finished before responding — the Calibration wizard needs to
        know the motion is done before it asks the user to report how far it got.
        Direction + scale are explicit per-call; this never reads or writes the
        stored CALIBRATION_LEFT/RIGHT_SCALE values itself."""
        global _calibration_probe_busy
        d = droid_ref[0]
        if d is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        active_protocol = _active_protocol_name()
        if active_protocol:
            return web.json_response(
                {"ok": False, "reason": f"{active_protocol} is active — turn it off before calibrating"},
                status=409
            )
        if _calibration_probe_busy:
            # Server-side guard, independent of the browser's dialBusy flag — a
            # page reload resets that client-side, but this can't be bypassed that
            # way, which is what let probes pile up and burst-fire earlier.
            return web.json_response(
                {"ok": False, "reason": "a calibration probe is already running — wait for it to finish first"},
                status=409
            )
        data = await request.json()
        direction = data.get("direction")
        if direction not in ("left", "right"):
            return web.json_response({"ok": False, "reason": f"invalid direction: {direction}"}, status=400)
        try:
            scale = float(data.get("scale", 1.0))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "reason": "invalid scale"}, status=400)
        _calibration_probe_busy = True
        try:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    calibration_spin_probe(d, direction, scale), main_loop
                )
                future.result(timeout=8)
            except concurrent.futures.TimeoutError:
                # Giving up on .result() does NOT stop the underlying coroutine — it
                # keeps running orphaned on main_loop with nobody waiting on it. That
                # orphan then sits in the way of the NEXT probe too, compounding over
                # repeated clicks. Explicitly cancel it so it doesn't keep running.
                future.cancel()
                return web.json_response(
                    {"ok": False, "reason": "probe timed out after 8s and was cancelled — main loop was too busy to run it"},
                    status=504
                )
            except Exception as e:
                return web.json_response({"ok": False, "reason": str(e) or repr(e)}, status=500)
            return web.json_response({"ok": True, "direction": direction, "scale": scale})
        finally:
            # Always release the busy guard, whether this succeeded, timed out,
            # or hit some other error — otherwise one failure permanently locks
            # out every future probe until the service is restarted.
            _calibration_probe_busy = False

    async def handle_calibration_victory(request):
        """Blocking celebratory spin + happy sound, once both directions are
        confirmed. Shares the same busy guard as handle_calibration_probe — no
        sense letting a victory spin overlap with anything else either."""
        global _calibration_probe_busy
        d = droid_ref[0]
        if d is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        active_protocol = _active_protocol_name()
        if active_protocol:
            return web.json_response(
                {"ok": False, "reason": f"{active_protocol} is active — turn it off before calibrating"},
                status=409
            )
        if _calibration_probe_busy:
            return web.json_response(
                {"ok": False, "reason": "a calibration probe is already running — wait for it to finish first"},
                status=409
            )
        _calibration_probe_busy = True
        try:
            try:
                future = asyncio.run_coroutine_threadsafe(calibration_victory(d), main_loop)
                future.result(timeout=8)
            except concurrent.futures.TimeoutError:
                future.cancel()
                return web.json_response(
                    {"ok": False, "reason": "victory spin timed out after 8s and was cancelled — main loop was too busy to run it"},
                    status=504
                )
            except Exception as e:
                return web.json_response({"ok": False, "reason": str(e) or repr(e)}, status=500)
            return web.json_response({"ok": True})
        finally:
            _calibration_probe_busy = False

    async def handle_calibration_set(request):
        """Directly update the live in-memory calibration values — no restart
        needed. kyber_config_server.py writes .env separately for persistence
        across a real restart/reboot; this just updates the already-running
        process immediately, since every gesture's motor-leg lookup
        (get_calibration_scale) reads these same two variables fresh on every
        single call rather than caching them anywhere else."""
        global CALIBRATION_LEFT_SCALE, CALIBRATION_RIGHT_SCALE
        data = await request.json()
        try:
            left  = float(data.get("left_scale", CALIBRATION_LEFT_SCALE))
            right = float(data.get("right_scale", CALIBRATION_RIGHT_SCALE))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "reason": "invalid scale values"}, status=400)
        if not (CALIBRATION_SCALE_MIN <= left <= CALIBRATION_SCALE_MAX) or \
           not (CALIBRATION_SCALE_MIN <= right <= CALIBRATION_SCALE_MAX):
            return web.json_response({
                "ok": False,
                "reason": f"scale values must be between {CALIBRATION_SCALE_MIN} and {CALIBRATION_SCALE_MAX}",
            }, status=400)
        CALIBRATION_LEFT_SCALE = left
        CALIBRATION_RIGHT_SCALE = right
        print(f"[CALIBRATION]: live values updated — left={left:.4f}, right={right:.4f}", flush=True)
        return web.json_response({"ok": True, "left_scale": left, "right_scale": right})

    async def handle_calibration_status(request):
        """Current stored calibration values, for display on the Calibration page."""
        return web.json_response({
            "left_scale": CALIBRATION_LEFT_SCALE,
            "right_scale": CALIBRATION_RIGHT_SCALE,
            "base_spin_duration": BASE_SPIN_DURATION,
        })

    async def handle_expressive_get(request):
        return web.json_response({"active": _expressive_mode_active})

    async def handle_expressive_post(request):
        global _expressive_mode_active
        d = droid_ref[0]
        if d is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        data = await request.json()
        activate = data.get("active", not _expressive_mode_active)
        if activate and not _expressive_mode_active:
            _expressive_mode_active = True
            print("[EXPRESSIVE]: Expressive mode activated", flush=True)
            async def _play_activation():
                sounds = EMOTION_MAP.get("happy", [])
                if sounds:
                    s = random.choice(sounds)
                    await _play_droid_audio(d, sound_id=s["sound_id"], bank_id=s["bank_id"])
            asyncio.run_coroutine_threadsafe(_play_activation(), main_loop)
        elif not activate and _expressive_mode_active:
            _expressive_mode_active = False
            print("[EXPRESSIVE]: Expressive mode deactivated", flush=True)
            async def _play_deactivation():
                sounds = EMOTION_MAP.get("defensive", [])
                if sounds:
                    s = random.choice(sounds)
                    await _play_droid_audio(d, sound_id=s["sound_id"], bank_id=s["bank_id"])
            asyncio.run_coroutine_threadsafe(_play_deactivation(), main_loop)
        return web.json_response({"ok": True, "active": activate})

    async def handle_roam_get(request):
        return web.json_response({"active": _roam_mode_active})

    async def handle_roam_post(request):
        global _roam_mode_active
        d = droid_ref[0]
        if d is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        data = await request.json()
        activate = data.get("active", not _roam_mode_active)
        if activate and not _roam_mode_active:
            asyncio.run_coroutine_threadsafe(activate_roam_mode(d), main_loop)
        elif not activate and _roam_mode_active:
            asyncio.run_coroutine_threadsafe(deactivate_roam_mode(d), main_loop)
        return web.json_response({"ok": True, "active": activate})

    async def handle_pet_get(request):
        return web.json_response({"active": _pet_mode_active})

    async def handle_pet_post(request):
        global _pet_mode_active
        d = droid_ref[0]
        if d is None:
            return web.json_response({"ok": False, "reason": "droid not connected"}, status=503)
        data = await request.json()
        activate = data.get("active", not _pet_mode_active)
        if activate and not _pet_mode_active:
            asyncio.run_coroutine_threadsafe(activate_pet_mode(d), main_loop)
        elif not activate and _pet_mode_active:
            asyncio.run_coroutine_threadsafe(deactivate_pet_mode(d), main_loop)
        return web.json_response({"ok": True, "active": activate})

    async def run_api():
        app = web.Application()
        app.router.add_post("/play", handle_play)
        app.router.add_post("/mode", handle_mode)
        app.router.add_get("/ready", handle_ready)
        app.router.add_get("/hotel", handle_hotel_get)
        app.router.add_post("/hotel", handle_hotel_post)
        app.router.add_get("/expressive", handle_expressive_get)
        app.router.add_post("/expressive", handle_expressive_post)
        app.router.add_get("/roam", handle_roam_get)
        app.router.add_post("/roam", handle_roam_post)
        app.router.add_get("/pet", handle_pet_get)
        app.router.add_post("/pet", handle_pet_post)
        app.router.add_post("/motor", handle_motor)
        app.router.add_post("/motor_test", handle_motor_test)
        app.router.add_post("/calibration_probe", handle_calibration_probe)
        app.router.add_post("/calibration_victory", handle_calibration_victory)
        app.router.add_post("/calibration_set", handle_calibration_set)
        app.router.add_get("/calibration_status", handle_calibration_status)
        app.router.add_post("/chirp", handle_chirp)
        app.router.add_get("/droid_status", handle_droid_status)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", MAPPER_API_PORT)
        await site.start()
        print(f"[MAPPER API]: listening on 127.0.0.1:{MAPPER_API_PORT}", flush=True)

    def thread_main():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_api())
        loop.run_forever()

    t = threading.Thread(target=thread_main, daemon=True)
    t.start()


NETGW_STATUS_URL = "http://127.0.0.1:5003/netgw/status"


async def wait_for_network():
    """Block here until kyber_netgw.py reports the network is actually
    online — not just associated, but past any captive portal too. This
    asks netgw rather than checking wifi directly, on purpose: netgw owns
    all network state, kyber_core.py shouldn't duplicate that logic.

    If netgw isn't reachable at all — an older deploy that doesn't have it,
    or it's been deliberately stopped — this gives up after a few tries
    rather than blocking the brain forever on a service that may never
    answer. Boot proceeds as it always did in that case."""
    logged_waiting = False
    misses = 0
    while True:
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None, lambda: requests.get(NETGW_STATUS_URL, timeout=3)
            )
            status = resp.json()
            if status.get("state") == "client_online":
                if logged_waiting:
                    print("[NETWORK]: online — proceeding to droid connection", flush=True)
                return
            if not logged_waiting:
                print(f"[NETWORK]: waiting for network (kyber_netgw state: {status.get('state')})...", flush=True)
                logged_waiting = True
            misses = 0
        except requests.RequestException:
            misses += 1
            if misses == 1:
                print("[NETWORK]: kyber_netgw.service not reachable — skipping network gate", flush=True)
            if misses >= 3:
                return
        await asyncio.sleep(3)


# ── Beacon Relay — KYBER as both ears and voice for park beacons & nearby
# droids ───────────────────────────────────────────────────────────────────
# Confirmed by direct testing (not assumed): once something holds the
# droid's GATT connection, he stops doing BOTH halves of his native social
# behavior — he no longer reacts to nearby location beacons or other
# droids, AND he stops advertising his own presence for other droids to
# react to. The official app must already do this exact job while
# connected; since KYBER now holds the connection instead, KYBER takes over
# both halves: scanning on his behalf ("ears") and re-broadcasting his own
# captured advertisement so others still notice him ("voice").
#
# Three concurrent BLE roles on one radio — held connection, continuous
# broadcast, continuous scan — were proven together on real hardware
# before any of this was written, including a live reaction from a second
# droid while all three were active at once.

BEACON_RELAY_ENABLED = os.getenv("BEACON_RELAY_ENABLED", "true").strip().lower() == "true"

# Anti-spam lockout between reactions -- same spirit as the droid's own
# native reaction cooldown (a real, documented field in Disney's own beacon
# ecosystem), just enforced here instead since the droid isn't doing it
# natively anymore once something is connected to him.
BEACON_REACTION_LOCKOUT_SECONDS = 8

# Real, confirmed Disney beacon location payloads -- not a guess. Pulled
# directly from pyDroidDepot's own droiddepot.beacon.OfficialDroidBeaconLocations
# (the same dependency DroidConnection already uses), keyed by raw bytes so
# matching is an exact lookup, not a manufacturer-ID-only check. ID 76 alone
# is far too broad to react on by itself -- it's Apple's general company
# ID, and a real-world test scan during development showed dozens of
# unrelated nearby-Apple-device hits under that same ID with no exact
# payload match. Exact-payload matching is what tells a real park beacon
# apart from someone's phone in the same room.
#
# Several DL_*/WDW_* constants share an identical byte payload (e.g.
# DL_Marketplace and WDW_OutdoorsArea are the same bytes) -- mapping bytes
# to a single name would silently drop one in favor of the other (last
# write wins), mislabeling e.g. a real Disneyland beacon as a WDW one in
# the logs. Mapping to a list instead keeps every matching name.
KNOWN_BEACON_LOCATIONS = {}
for _loc_name, _loc_hex in vars(OfficialDroidBeaconLocations).items():
    if _loc_name.startswith('_'):
        continue
    KNOWN_BEACON_LOCATIONS.setdefault(bytes.fromhex(_loc_hex), []).append(_loc_name)

# bluezero is only needed for the "voice" half (broadcasting) -- scanning
# only needs bleak, already a hard dependency. Imported defensively so a
# missing/broken bluezero install (it has its own real system-package
# requirements -- see deploy notes) degrades to "ears only" instead of
# refusing to start the whole service.
try:
    from bluezero import advertisement as _ble_advertisement
    _BLUEZERO_AVAILABLE = True
except Exception as _bluezero_import_error:
    _BLUEZERO_AVAILABLE = False
    print(f"[BEACON RELAY]: bluezero unavailable ({_bluezero_import_error}) -- voice echo disabled, scanning still works", flush=True)

_last_beacon_reaction_time = 0.0
_current_reaction_task     = None
_current_thinking_task     = None  # tracks the fire-and-forget play_thinking task,
                                    # so a real reaction can cancel it outright
                                    # before starting, instead of racing it on the
                                    # same BLE connection (see main loop).
_echo_advertisement        = None
_echo_manager              = None
_echo_advert_id            = 9  # incremented on every restart -- see _run_voice_echo_blocking
_beacon_scan_paused        = False
_beacon_scan_stopped       = asyncio.Event()  # set only once the scanner has actually torn down

# Bleak's BlueZ backend hard-requires at least one or_pattern for passive
# scanning -- it raises BleakError("passive scanning mode requires bluez
# or_patterns") at scanner construction time otherwise (confirmed present
# since at least bleak 0.16, including 0.20.2 -- not a recent version
# quirk). Without this, _scan_forever() below fails on its very first line
# every single time, forever, on a 10s retry loop -- the scanner never
# actually listens for anything.
#
# Filtering by company ID here (rather than only in on_detect() after the
# fact) also moves the matching down to BlueZ/kernel level, which is the
# normal use of or_patterns -- though note BlueZ has a known quirk where
# or_patterns can be ignored while a GATT connection is already active
# (exactly KYBER's situation). on_detect()'s existing exact-payload check
# stays in place regardless, so this is a resource/efficiency improvement
# layered on top of the real fix (a non-empty or_patterns list), not a
# replacement for the existing software-side filter.
_BEACON_OR_PATTERNS = [
    (0, AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA,
     DisneyBLEManufacturerId.DisneyiBeacon.to_bytes(2, "little")),
    (0, AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA,
     DisneyBLEManufacturerId.DroidManufacturerId.to_bytes(2, "little")),
]


async def _run_cancellable(coro):
    """The one through-line every droid reaction (conversational or
    beacon-triggered) goes through, so a beacon/droid detection can preempt
    whatever's currently running -- true preemption, not just 'queued for
    the next idle moment'. Motor functions already guarantee stop_motors()
    via their own try/finally (drive_leg, calibration_spin_probe, etc.), so
    a cancelled gesture still halts safely. Audio can't be cancelled in
    software -- pyDroidDepot has no stop-audio primitive, only a trigger --
    so a beacon reaction's sound overrides whatever's currently playing on
    the droid's own chip rather than being cleanly silenced first."""
    global _current_reaction_task
    task = asyncio.create_task(coro)
    _current_reaction_task = task
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _beacon_reaction(droid, label: str):
    """Generic reaction for any beacon/droid hit -- reuses the existing
    sound+motion pipeline rather than new assets. label is logged now and
    available later if location-specific reactions ever matter."""
    print(f"[BEACON RELAY]: reacting to {label}", flush=True)
    try:
        await play_emotion(droid, "excited")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[BEACON RELAY]: reaction failed -- {e}", flush=True)


def _current_droid_mfr_bytes(droid) -> bytes:
    """The connected droid's own raw advertisement bytes, captured naturally
    at discovery time (see _connect_droid_by_mac) and already sitting on the
    DroidConnection object -- used so the scanner never mistakes KYBER's
    own echoed broadcast for a separate nearby droid. Droid-agnostic --
    works the same whether the connected unit is R2, Nub, or anything else."""
    try:
        return bytes(droid.manufacturer_data.get(DisneyBLEManufacturerId.DroidManufacturerId, b""))
    except Exception:
        return b""


def _run_voice_echo_blocking(raw_bytes: bytes):
    """Runs bluezero's GLib-based advertising loop -- this blocks the
    calling thread for as long as the connection lasts, by design, exactly
    like the existing keepalive/health/hotel/roam/pet threads already do
    for their own continuous background jobs. Never call this directly on
    the main asyncio loop.

    Still uses a fresh advertisement ID every call as defense-in-depth --
    bluezero's Advertisement.path is derived straight from this ID, so a
    fresh one guarantees the new object's D-Bus path can never collide with
    one that's mid-teardown. The actual leak this used to describe is fixed
    now: stop_voice_echo() explicitly unexports the old Python object via
    remove_from_connection() (confirmed against bluezero's real source --
    Advertisement.stop() only quits its own GLib mainloop, it never calls
    this), so the old object no longer lingers in the shared D-Bus
    connection's registration table after every reconnect."""
    global _echo_advertisement, _echo_manager, _echo_advert_id
    try:
        _echo_advert_id += 1
        _echo_advertisement = _ble_advertisement.Advertisement(_echo_advert_id, 'broadcast')
        _echo_advertisement.manufacturer_data(DisneyBLEManufacturerId.DroidManufacturerId, list(raw_bytes))
        _echo_manager = _ble_advertisement.AdvertisingManager()
        _echo_manager.register_advertisement(_echo_advertisement, {})
        print(f"[BEACON RELAY]: voice echo started ({len(raw_bytes)} bytes)", flush=True)
        _echo_advertisement.start()  # blocks here until stop_voice_echo() calls .stop()
    except Exception as e:
        print(f"[BEACON RELAY]: voice echo failed to start -- {e}", flush=True)


def stop_voice_echo():
    """Safe to call even if no echo is running -- start_voice_echo() always
    calls this first, so a stale echo from a previous connection cycle
    never lingers across a reconnect.

    Three-step teardown, each independent and best-effort: tell BlueZ to
    stop broadcasting (unregister_advertisement), stop the object's own
    GLib mainloop so its blocking thread can return (.stop()), then
    explicitly unexport the Python object from the shared D-Bus connection
    (remove_from_connection()). That last step is the one that was missing
    -- without it, every reconnect left one more Advertisement object
    permanently registered on the bus, accumulating for the life of the
    process. Wrapped in its own try/except like the two steps above it,
    in case the object's underlying export state is ever in a state this
    can't cleanly act on (e.g. construction partially failed)."""
    global _echo_advertisement, _echo_manager
    if _echo_advertisement is not None:
        try:
            if _echo_manager is not None:
                _echo_manager.unregister_advertisement(_echo_advertisement)
        except Exception:
            pass
        try:
            _echo_advertisement.stop()
        except Exception:
            pass
        try:
            _echo_advertisement.remove_from_connection()
        except Exception:
            pass
    _echo_advertisement = None
    _echo_manager = None


def start_voice_echo(droid):
    """Call this right after every successful (re)connect. Echoes R2's own
    captured bytes verbatim -- no guessing at the advertisement format,
    just replaying what he already broadcasts himself."""
    if not (_BLUEZERO_AVAILABLE and BEACON_RELAY_ENABLED):
        return
    raw = _current_droid_mfr_bytes(droid)
    if not raw:
        print("[BEACON RELAY]: no captured advertisement bytes to echo -- skipping voice", flush=True)
        return
    stop_voice_echo()
    threading.Thread(target=_run_voice_echo_blocking, args=(raw,), daemon=True).start()


async def _reconnect_with_scan_paused(reconnect_coro):
    """Pause the beacon relay's own scanner for the duration of a reconnect
    attempt -- _connect_droid_by_mac() runs its own separate BleakScanner
    session to relocate the droid by MAC, and running two independent
    discovery sessions against the same adapter at once is a known BlueZ
    multi-client edge case not worth risking during an already-fragile
    moment (mid-reconnect). Actually waits for the scanner to confirm it's
    torn down rather than assuming a fixed delay was long enough -- the
    scanner's own poll interval is what used to leave a real window where
    both could briefly run at once."""
    global _beacon_scan_paused
    _beacon_scan_paused = True
    try:
        await asyncio.wait_for(_beacon_scan_stopped.wait(), timeout=5)
    except asyncio.TimeoutError:
        print("[BEACON RELAY]: scanner didn't confirm stop in time -- proceeding with reconnect anyway", flush=True)
    try:
        return await reconnect_coro
    finally:
        _beacon_scan_paused = False


def _start_beacon_relay(droid_ref: list):
    """Scanner half ('ears') -- a single passive BleakScanner task living on
    the main event loop for the lifetime of the service, same always-on
    pattern as everything else here. Passive mode (not active scanning)
    keeps this as light a radio load as the chip will allow, since it runs
    continuously alongside a held connection and a continuous broadcast."""
    if not BEACON_RELAY_ENABLED:
        print("[BEACON RELAY]: disabled via BEACON_RELAY_ENABLED -- not starting", flush=True)
        return

    def on_detect(device, advertisement_data):
        mfr = advertisement_data.manufacturer_data or {}
        if not mfr:
            return
        d = droid_ref[0]
        if d is None:
            return

        hit_label = None
        if DisneyBLEManufacturerId.DisneyiBeacon in mfr:
            payload = bytes(mfr[DisneyBLEManufacturerId.DisneyiBeacon])
            names = KNOWN_BEACON_LOCATIONS.get(payload)
            if names:
                hit_label = f"location beacon ({'/'.join(names)})"
        if hit_label is None and DisneyBLEManufacturerId.DroidManufacturerId in mfr:
            payload = bytes(mfr[DisneyBLEManufacturerId.DroidManufacturerId])
            if payload and payload != _current_droid_mfr_bytes(d):
                hit_label = "nearby droid"
        if hit_label is None:
            return

        global _last_beacon_reaction_time
        now = time.time()
        if now - _last_beacon_reaction_time < BEACON_REACTION_LOCKOUT_SECONDS:
            return
        _last_beacon_reaction_time = now

        print(f"[BEACON RELAY]: {hit_label} detected (rssi {advertisement_data.rssi})", flush=True)
        if _current_reaction_task is not None and not _current_reaction_task.done():
            _current_reaction_task.cancel()
        # run_coroutine_threadsafe rather than asyncio.create_task -- bleak's
        # BlueZ/dbus-fast backend can dispatch this callback from a non-event-loop
        # thread, and create_task is not thread-safe. run_coroutine_threadsafe is
        # the correct cross-thread dispatch, same pattern every other KYBER thread uses.
        if _main_event_loop is not None:
            asyncio.run_coroutine_threadsafe(
                _run_cancellable(_beacon_reaction(d, hit_label)),
                _main_event_loop
            )

    async def _scan_forever():
        while True:
            if _beacon_scan_paused:
                _beacon_scan_stopped.set()
                await asyncio.sleep(0.5)
                continue
            _beacon_scan_stopped.clear()
            try:
                async with BleakScanner(
                    detection_callback=on_detect,
                    scanning_mode="passive",
                    bluez={"or_patterns": _BEACON_OR_PATTERNS},
                ):
                    while not _beacon_scan_paused:
                        await asyncio.sleep(1)
                _beacon_scan_stopped.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[BEACON RELAY]: scanner error -- {e} -- restarting in 10s", flush=True)
                await asyncio.sleep(10)

    asyncio.create_task(_scan_forever())
    print("[BEACON RELAY]: scanning for beacons & nearby droids", flush=True)


async def main():
    global _conversation_active, _main_event_loop, _droid_ready, _ble_lost, _last_reconnect_time, _ble_reconnecting, _hotel_mode_active, _hotel_end_time, _hotel_move_requested, _hotel_activated_time, _expressive_mode_active, _roam_mode_active, _pet_mode_active, _last_mapper_play
    _main_event_loop = asyncio.get_event_loop()
    _last_reconnect_time = time.time()  # Prevent health check from firing immediately on boot

    print(f"Sound profile: {ACTIVE_SOUND_PROFILE} ({len(EMOTION_MAP)} emotions loaded)", flush=True)
    print(f"Personality: {ACTIVE_PERSONALITY} (traits: {PERSONALITY_TRAITS}) — {personality_summary(PERSONALITY_TRAITS)}", flush=True)

    async def _connect_droid_by_mac(mac: str) -> DroidConnection:
        """Scan for a specific droid by MAC address using BleakScanner.

        Passive + filtered (same or_patterns the beacon relay's own scanner
        uses) rather than bleak's default active, unfiltered scan -- this
        runs every ~13s throughout any reconnect attempt (longer outages
        can mean well over a hundred scan/teardown cycles before giveup),
        so it's worth it being as light as the relay's own continuous scan
        rather than the heaviest mode bleak offers. No GATT connection is
        active while this runs (that's the precondition for being
        disconnected in the first place), so the BlueZ or_patterns-ignored-
        during-an-active-connection quirk noted near _BEACON_OR_PATTERNS
        doesn't apply here. The exact MAC + manufacturer-ID check below is
        unchanged -- or_patterns is a coarser kernel-level pre-filter on
        top of it, not a replacement for it."""
        from droiddepot.protocol import DisneyBLEManufacturerId
        target = mac.upper()
        async with BleakScanner(
            scanning_mode="passive",
            bluez={"or_patterns": _BEACON_OR_PATTERNS},
        ) as scanner:
            for attempt in range(10):
                devices = scanner.discovered_devices_and_advertisement_data
                for addr, (ble_device, adv_data) in devices.items():
                    if ble_device.address.upper() == target:
                        mfr = adv_data.manufacturer_data or {}
                        if DisneyBLEManufacturerId.DroidManufacturerId in mfr:
                            return DroidConnection(ble_device.address, mfr)
                await asyncio.sleep(1)
        raise RuntimeError(f"Claimed droid {mac} not found — is it powered on and in range?")

    async def connect_droid():
        global _droid_ready, _kyber_fully_ready, _last_reconnect_time, _ble_reconnecting, _ble_lost, _droid_waiting, DROID_MAC
        _ble_reconnecting = True
        _ble_lost = False

        # If no droid is paired, wait until one is claimed via the UI
        if not DROID_MAC:
            _droid_waiting = True
            print("[DROID]: No droid paired — waiting for pairing via Bluetooth Manager...", flush=True)
            while not DROID_MAC:
                # Re-read DROID_MAC from .env every 5 seconds
                from dotenv import dotenv_values as _dv
                _env = _dv(ENV_PATH)
                DROID_MAC = _env.get("DROID_MAC", "").upper().strip()
                if not DROID_MAC:
                    await asyncio.sleep(5)
            _droid_waiting = False
            print(f"[DROID]: Droid paired ({DROID_MAC}) — connecting...", flush=True)

        # First 10 minutes after a disconnect: keep retrying at a fixed,
        # short interval rather than backing off -- the droid most likely
        # just walked briefly out of range or had a momentary BLE hiccup,
        # so it's worth hammering reconnect attempts while it's probably
        # about to come back. Only past that point does a longer absence
        # start looking like "actually gone for a while", at which point
        # backing off (instead of draining battery every 3s for hours)
        # actually makes sense -- ramping to a 60s ceiling within ~90s of
        # entering that phase. Giveup is still measured from the original
        # disconnect, 2 hours total either way.
        RECONNECT_CONSTANT_SECONDS = 10 * 60
        RECONNECT_CONSTANT_INTERVAL = 3
        RECONNECT_BACKOFF_START  = 3
        RECONNECT_BACKOFF_MAX    = 60
        RECONNECT_GIVEUP_SECONDS = 2 * 60 * 60
        CONNECT_ATTEMPT_TIMEOUT  = 15   # bounds a single d.connect() call. Without
                                          # this, a stalled BlueZ-level connect can
                                          # hang silently for minutes with no retry
                                          # and no log output -- it's all one await
                                          # chain, so nothing else in main() (BLE
                                          # health check, reconnect-on-disconnect)
                                          # gets a chance to run until it returns.

        started_at = time.time()
        retry_delay = RECONNECT_BACKOFF_START
        giveup_at = started_at + RECONNECT_GIVEUP_SECONDS
        last_d = None   # last-scanned DroidConnection, if any -- lets us clear
                         # stale BlueZ state before the next scan, same as the
                         # mid-session reconnect paths already do once a
                         # connection exists. Nothing to clear on the very first
                         # attempt of a fresh process, so this stays None until
                         # a scan actually succeeds once.

        # The library's automatic DroidPairingSequence1 (light+sound "paired"
        # confirmation) is genuinely useful on an ordinary connect or
        # reconnect -- it's the only audible cue that bridges the gap between
        # "BLE reconnected" and mic warmup actually finishing a few seconds
        # later. It's only silenced for the two moments that already have
        # their own confirmation: the claim moment (Disney's own activation
        # show would clash with it) and the final power-cycle restart on the
        # Ready page (already confirmed working there, nothing left to
        # confirm audibly). Read once per connect_droid() call rather than
        # per attempt -- neither flag changes mid-retry.
        from dotenv import dotenv_values as _dv3
        _flags = _dv3(ENV_PATH)
        connect_silently = (
            _flags.get("PLAY_ACTIVATION_ON_NEXT_BOOT") == "1"
            or _flags.get("SKIP_STARTUP_SOUND_ONCE") == "1"
        )

        while True:
            try:
                if last_d is not None:
                    # Same reasoning as the mid-session reconnect paths: tell
                    # BlueZ to release whatever it's still holding from the
                    # last failed attempt before scanning again, or the next
                    # several attempts tend to fail immediately with "Service
                    # Discovery has not been performed" / "failed to discover
                    # services, device disconnected" -- the exact pattern this
                    # loop was hitting.
                    try:
                        await asyncio.wait_for(last_d.disconnect(), timeout=3)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                print(f"Searching for paired droid ({DROID_MAC})...", flush=True)
                d = await _connect_droid_by_mac(DROID_MAC)
                last_d = d
                await asyncio.wait_for(d.connect(silent=connect_silently), timeout=CONNECT_ATTEMPT_TIMEOUT)
                await asyncio.sleep(3)
                _droid_ready = True
                _last_reconnect_time = time.time()
                _ble_reconnecting = False
                print("Droid connected!\n", flush=True)
                return d
            except Exception as e:
                err = str(e)
                _droid_ready = False
                _kyber_fully_ready = False

                if time.time() >= giveup_at:
                    print(f"[RECONNECT]: No droid found in {RECONNECT_GIVEUP_SECONDS // 3600}h -- "
                          f"giving up and shutting the Pi down to save battery.", flush=True)
                    print("[RECONNECT]: Power-cycle the Pi once the droid is back in range.", flush=True)
                    try:
                        subprocess.Popen(['sudo', 'shutdown', '-h', 'now'])
                    except Exception as shutdown_err:
                        print(f"[RECONNECT]: Shutdown command failed -- {shutdown_err}", flush=True)
                    # Gives the shutdown a chance to actually take the system down.
                    # If it failed (e.g. sudoers not configured), this re-attempts
                    # the shutdown itself every 60s instead of spinning tightly.
                    await asyncio.sleep(60)
                    continue

                if time.time() - started_at < RECONNECT_CONSTANT_SECONDS:
                    print(f"[RECONNECT]: Failed -- {err}. Retrying in {RECONNECT_CONSTANT_INTERVAL}s...", flush=True)
                    await asyncio.sleep(RECONNECT_CONSTANT_INTERVAL)
                else:
                    print(f"[RECONNECT]: Failed -- {err}. Retrying in {retry_delay}s...", flush=True)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, RECONNECT_BACKOFF_MAX)

    # Start mapper API early so /chirp and /droid_status are reachable before droid connects
    droid_ref = [None]
    _start_mapper_api(droid_ref, _main_event_loop)
    await asyncio.sleep(1)  # Give mapper API thread time to bind port

    await wait_for_network()
    droid = await connect_droid()
    droid_ref[0] = droid
    start_voice_echo(droid)

    # One-shot: play Disney's own DroidBayActivationSequence script over BLE
    # the moment a droid is freshly claimed, so the onboarding wizard's
    # activation page can run alongside real light/sound/head-movement on
    # the droid instead of just a lookalike animation. Gated by a flag that
    # droid_claim() sets right before it restarts this service and that we
    # clear immediately here -- this must never replay on ordinary restarts
    # later (mic step, Save & Restart from the Mainframe, etc.), only on
    # this exact first connect right after a droid is claimed.
    #
    # This is a direct await, not a background task -- asyncio.create_task()
    # only schedules a coroutine, it doesn't run it until the next await
    # point, which in practice meant this fired a second or so late (right
    # around when mic warmup finished, not the moment of connection). The
    # BLE write itself is fast; the droid then plays out its own show
    # independently over the following ~30s while mic warmup below proceeds
    # in parallel, so awaiting it here doesn't meaningfully delay startup.
    from dotenv import dotenv_values as _dv, set_key as _set_key
    if _dv(ENV_PATH).get("PLAY_ACTIVATION_ON_NEXT_BOOT") == "1":
        _set_key(ENV_PATH, "PLAY_ACTIVATION_ON_NEXT_BOOT", "0")
        global _activation_muted
        _activation_muted = True
        try:
            await DroidScriptEngine(droid).execute_script(DroidScripts.DroidBayActivationSequence)
            print("[ACTIVATION]: Triggered DroidBayActivationSequence over BLE", flush=True)
        except Exception as e:
            print(f"[ACTIVATION]: Failed to trigger activation sequence -- {e}", flush=True)

    print("Warming up mic...", flush=True)
    global MIC_DEVICE
    warmup_ok = False
    for attempt in range(5):
        try:
            _ensure_mic_stream()
            # Read a handful of frames to confirm the stream is actually
            # producing data — enough to verify ALSA opened cleanly
            for _ in range(10):   # ~200ms worth
                frame = _read_mic_frame()
                if frame is None:
                    raise RuntimeError(f"Mic stream on {MIC_DEVICE} closed immediately")
            print(f"[MIC]: Persistent stream active on {MIC_DEVICE}", flush=True)
            warmup_ok = True
            break
        except Exception as e:
            # Force reopen on next attempt
            global _arecord_proc
            if _arecord_proc is not None:
                try: _arecord_proc.terminate()
                except Exception: pass
                _arecord_proc = None
            if attempt < 4:
                print(f"[MIC]: Warmup attempt {attempt + 1} failed — retrying in 3s ({e})", flush=True)
                await asyncio.sleep(3)
            else:
                # Primary exhausted — try fallback device
                if MIC_DEVICE_FALLBACK and MIC_DEVICE_FALLBACK != MIC_DEVICE:
                    print(f"[MIC]: Primary '{MIC_DEVICE}' unavailable — trying fallback '{MIC_DEVICE_FALLBACK}'", flush=True)
                    MIC_DEVICE = MIC_DEVICE_FALLBACK
                    try:
                        _ensure_mic_stream()
                        for _ in range(10):
                            frame = _read_mic_frame()
                            if frame is None:
                                raise RuntimeError("Fallback stream closed immediately")
                        print(f"[MIC]: Fallback stream active on {MIC_DEVICE_FALLBACK}", flush=True)
                        warmup_ok = True
                    except Exception as e2:
                        print(f"[MIC]: Fallback '{MIC_DEVICE_FALLBACK}' also failed — {e2}", flush=True)
                        print("[MIC]: Voice commands will still be attempted", flush=True)
                else:
                    print(f"[MIC]: Warmup skipped after 5 attempts — {e}", flush=True)
                    print("[MIC]: Voice commands will still be attempted", flush=True)

    # One-shot: /setup/finish sets this right before the final power-cycle
    # restart on the Ready page, since at that point the person already saw
    # the droid connect successfully via the Ready page's own status check
    # -- there's nothing left to confirm audibly. Every other restart
    # (routine reconnects, the claim moment, Mainframe "Save & Restart")
    # keeps this chime exactly as-is; it's the only "I'm back" signal on
    # those and shouldn't be silenced.
    from dotenv import dotenv_values as _dv2, set_key as _set_key2
    skip_chime = _dv2(ENV_PATH).get("SKIP_STARTUP_SOUND_ONCE") == "1"
    if skip_chime:
        _set_key2(ENV_PATH, "SKIP_STARTUP_SOUND_ONCE", "0")
    else:
        startup_sound = random.choice(_ALL_SOUNDS)
        await _play_droid_audio(
            droid,
            sound_id=startup_sound["sound_id"],
            bank_id=startup_sound["bank_id"]
        )
    await asyncio.sleep(2)

    global _kyber_fully_ready
    _kyber_fully_ready = True

    print(f"--- Droid is listening ({LLM_PROVIDER.title()} + {STT_PROVIDER.title()}) ---", flush=True)
    print("--- Say 'stay awake' to enable keepalive, 'go to sleep' to disable ---\n", flush=True)

    t = threading.Thread(target=keepalive_thread, args=(droid_ref,), daemon=True)
    t.start()
    th = threading.Thread(target=ble_health_thread, args=(droid_ref,), daemon=True)
    th.start()
    th2 = threading.Thread(target=hotel_sentry_thread, args=(droid_ref,), daemon=True)
    th2.start()
    th3 = threading.Thread(target=roam_thread, args=(droid_ref,), daemon=True)
    th3.start()
    th4 = threading.Thread(target=pet_thread, args=(droid_ref,), daemon=True)
    th4.start()
    _start_beacon_relay(droid_ref)

    history         = deque(maxlen=HISTORY_LENGTH)
    loop            = asyncio.get_event_loop()

    while True:
        try:
            # Check if health thread detected a silent BLE disconnect
            if _ble_lost:
                _ble_lost = False
                print("[BLE]: Silent disconnect detected — reconnecting...", flush=True)
                # Explicitly tell BlueZ to close the old connection before we start
                # scanning again. Without this, BlueZ holds stale connection state
                # from the previous session and the first several reconnect attempts
                # fail immediately with "Not connected" or "Service Discovery has not
                # been performed" — the exact pattern seen in the logs. The droid
                # may already be gone so this is best-effort, not checked.
                try:
                    await asyncio.wait_for(droid.disconnect(), timeout=3)
                except Exception:
                    pass
                await asyncio.sleep(2)  # give BlueZ time to release the old state
                droid_ref[0] = None
                droid = await _reconnect_with_scan_paused(connect_droid())
                droid_ref[0] = droid
                start_voice_echo(droid)
                try:
                    reconnect_sound = random.choice(_ALL_SOUNDS)
                    await _play_droid_audio(
                        droid,
                        sound_id=reconnect_sound["sound_id"],
                        bank_id=reconnect_sound["bank_id"]
                    )
                except Exception as e:
                    print(f"[BLE]: Reconnect confirmation sound failed — {e}", flush=True)
                print("[BLE]: Reconnected — Droid is listening again\n", flush=True)
                continue

            # Check for pending motor commands from Controls page
            with _motor_command_lock:
                if _motor_command_queue:
                    fn = _motor_command_queue.pop(0)
                    _motor_command_queue.clear()
                    await fn(droid)

            # Check for pending hotel sentry movement
            if _hotel_move_requested and _droid_ready:
                _hotel_move_requested = False
                hotel_fn = hotel_move_bb if DROID_TYPE.lower() == "bb" else hotel_move
                await hotel_fn(droid)

            # Check for pending mapper play requests
            with _mapper_play_lock:
                if _mapper_play_queue and (time.time() - _last_mapper_play) > 0.5:
                    bank_id, sound_id = _mapper_play_queue.pop(0)
                    _mapper_play_queue.clear()  # Discard any stacked requests
                    try:
                        await _play_droid_audio(droid, sound_id=sound_id, bank_id=bank_id)
                        _last_mapper_play = time.time()
                        print(f"[MAPPER]: played bank {bank_id}, sound {sound_id}", flush=True)
                    except Exception as e:
                        print(f"[MAPPER ERROR]: {e}", flush=True)

            # If mapper is active, skip listening entirely
            if _mapper_mode:
                await asyncio.sleep(0.2)
                continue

            if _activation_muted:
                from dotenv import dotenv_values as _dv5
                if _dv5(ENV_PATH).get("ACTIVATION_CONFIRMED") == "1":
                    _activation_muted = False
                else:
                    await asyncio.sleep(0.2)
                    continue

            try:
                # collect_speech_segment() runs WebRTC VAD on 20ms frames from
                # the persistent mic stream. Returns complete PCM bytes of one
                # utterance, or None after ~2s of silence (so the loop can
                # check hotel/motor/mapper commands). Replaces the old
                # record_chunk → get_audio_stats → record_until_done chain
                # and all the chunk-stitching complexity that went with it.
                pcm_bytes = await loop.run_in_executor(None, collect_speech_segment)
            except RuntimeError:
                await asyncio.sleep(1)
                continue

            if not pcm_bytes:
                continue

            if time.time() < _mic_gate_until:
                # VAD captured something in the window right after R2's own
                # audio played -- almost certainly him hearing his own dome
                # sounds/reactions, not a real utterance (see
                # _play_droid_audio). Discard before the thinking animation
                # or a real STT call fires, so self-hearing doesn't burn an
                # API call or make R2 visibly react to himself. Distinct
                # marker from the plain "." (a real STT miss) so this is
                # easy to tell apart in the logs.
                print("~", end="", flush=True)
                continue

            _conversation_active = True

            # "Thinking" animation (dome sweep, BB wiggle, etc. — chassis-aware
            # via play_thinking) — skipped entirely during Hotel Sentry. This
            # used to fire unconditionally the moment speech was detected,
            # before transcription even ran, which is *before* the hotel-mode
            # check below ever gets a chance to ignore the input. That meant
            # the droid visibly reacted to anyone talking nearby even though
            # sentry mode is supposed to look inert except for its own
            # scheduled move.
            if not _hotel_mode_active:
                global _current_thinking_task
                async def _safe_thinking_anim():
                    try:
                        await asyncio.wait_for(play_thinking(droid), timeout=THINKING_TIMEOUT)
                    except Exception:
                        pass
                _current_thinking_task = asyncio.create_task(_safe_thinking_anim())
            text = await loop.run_in_executor(None, transcribe, pcm_bytes)

            if text:
                print(f"[YOU]: {text}", flush=True)
                # Hotel sentry mode — only listen for stop command
                if _hotel_mode_active:
                    if not _hotel_moving and (is_hotel_stop_command(text) or check_keywords(text) == "hotel_mode_off"):
                        print("[HOTEL]: Stop command received", flush=True)
                        await deactivate_hotel_mode(droid, voice_triggered=True)
                    else:
                        print("[HOTEL]: Sentry active — ignoring", flush=True)
                else:
                    # A real reaction is about to send its own motor commands --
                    # cancel any thinking wiggle still in flight first, so the
                    # two can never race each other's writes on the same BLE
                    # connection. drive_leg's own finally still guarantees a
                    # stop on cancellation.
                    if _current_thinking_task is not None and not _current_thinking_task.done():
                        _current_thinking_task.cancel()
                    keyword = check_keywords(text)
                    if keyword:
                        await _run_cancellable(handle_keyword(droid, keyword))
                    else:
                        emotion = await get_emotion(text, history, droid)
                        await _run_cancellable(play_emotion(droid, emotion))
                        history.append((text, emotion))
            else:
                print(".", end="", flush=True)

            # Nothing has read the mic pipe since collect_speech_segment()
            # returned -- transcribe() (10-20s on a slow/retried STT call)
            # and everything above it (gestures, sounds, the hotel-stop
            # branch) all ran with the mic stream unread the whole time.
            # arecord keeps capturing regardless, so real audio -- including
            # the droid's own thinking-sound echo -- backs up in the OS pipe
            # buffer. _mic_gate_until can't catch that on its own: it's
            # checked against wall-clock time when the NEXT
            # collect_speech_segment() call finally returns, which can be
            # long after the backlogged frames were actually captured, so a
            # slow turn makes the gate look expired even though the stale
            # audio it's about to process is from right when the gate was
            # active. Force the stream to respawn fresh here so the next
            # listen starts from silence, not a backlog.
            _discard_mic_backlog()

            _conversation_active = False

        except Exception as e:
            err = str(e)
            print(f"[ERROR]: {err}", flush=True)
            _conversation_active = False
            if not _ble_reconnecting and ("Service Discovery" in err or "BleakError" in err or "Not connected" in err or "disconnected" in err.lower()):
                print("[BLE]: Connection lost — attempting to reconnect...", flush=True)
                _droid_ready = False
                _kyber_fully_ready = False
                try:
                    await asyncio.wait_for(droid.disconnect(), timeout=3)
                except Exception:
                    pass
                await asyncio.sleep(2)
                droid_ref[0] = None
                droid = await _reconnect_with_scan_paused(connect_droid())
                droid_ref[0] = droid
                start_voice_echo(droid)
                try:
                    reconnect_sound = random.choice(_ALL_SOUNDS)
                    await _play_droid_audio(
                        droid,
                        sound_id=reconnect_sound["sound_id"],
                        bank_id=reconnect_sound["bank_id"]
                    )
                except Exception as e2:
                    print(f"[BLE]: Reconnect confirmation sound failed — {e2}", flush=True)
                print("[BLE]: Reconnected — Droid is listening again\n", flush=True)
            else:
                await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDroid powering down...", flush=True)
