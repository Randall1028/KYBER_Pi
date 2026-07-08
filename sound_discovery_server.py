#!/usr/bin/env python3
"""
Droid Sound Discovery Server
Proxies sound playback through the brain's local API (port 5002).
The brain keeps the BLE connection — no direct droid connection here.
"""

import asyncio
import json
import os
import signal
import subprocess
import time
import aiohttp
from aiohttp import web
from dotenv import dotenv_values

# ── Dynamic paths ────────────────────────────────────────────────────────────
HOME        = os.path.expanduser('~')
PROJECT_DIR = os.path.join(HOME, 'kyber')
ENV_PATH    = os.path.join(PROJECT_DIR, '.env')
MAP_DIR     = os.path.join(PROJECT_DIR, 'personality_maps')
PORT        = 5000
CONFIG_PORT = 5001
BRAIN_API   = "http://127.0.0.1:5002"

# ── Read active sound profile from .env ──────────────────────────────────────
def get_active_sound_profile() -> int:
    vals = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    return int(vals.get("MAPPER_SOUND_PROFILE", vals.get("ACTIVE_SOUND_PROFILE", "1")))

# ── R Unit sound map ─────────────────────────────────────────────────────────
RUNIT_BANKS = {
    1:  {"name": "General Use",         "sounds": 4},
    2:  {"name": "Droid Depot",         "sounds": 4},
    3:  {"name": "Resistance",          "sounds": 3},
    4:  {"name": "Unknown",             "sounds": 1},
    5:  {"name": "Droid Detector",      "sounds": 1},
    6:  {"name": "Dok Ondar's",         "sounds": 4},
    7:  {"name": "First Order",         "sounds": 5},
    8:  {"name": "Initial Activation",  "sounds": 1},
    9:  {"name": "Motor Sound",         "sounds": 1},
    11: {"name": "Blaster Accessory",   "sounds": 2},
    12: {"name": "Thruster Accessory",  "sounds": 2},
}

SOUND_LIST = []
for bank_id, info in RUNIT_BANKS.items():
    for sound_id in range(1, info["sounds"] + 1):
        SOUND_LIST.append({
            "bank_id": bank_id,
            "sound_id": sound_id,
            "bank_name": info["name"],
            "label": f"{info['name']} — Sound {sound_id}"
        })

current_index = 0
labels = {}
skipped = set()
_last_activity = time.time()


def touch_activity():
    global _last_activity
    _last_activity = time.time()


def sound_key(bank_id, sound_id):
    return f"{bank_id}_{sound_id}"


def get_save_path() -> str:
    sound_profile = get_active_sound_profile()
    os.makedirs(MAP_DIR, exist_ok=True)
    return os.path.join(MAP_DIR, f'sound_profile_{sound_profile}.json')


def read_sound_profile_name(slot: int) -> str:
    map_path = os.path.join(MAP_DIR, f'sound_profile_{slot}.json')
    if os.path.exists(map_path):
        try:
            with open(map_path) as f:
                data = json.load(f)
            return data.get("name", f"Sound Profile {slot}")
        except Exception:
            pass
    return f"Sound Profile {slot}"


def write_sound_profile_name(slot: int, name: str):
    map_path = os.path.join(MAP_DIR, f'sound_profile_{slot}.json')
    existing = {}
    if os.path.exists(map_path):
        try:
            with open(map_path) as f:
                existing = json.load(f)
        except Exception:
            pass
    existing["name"] = name
    os.makedirs(MAP_DIR, exist_ok=True)
    with open(map_path, "w") as f:
        json.dump(existing, f, indent=2)


def restart_kyber_service():
    """Mirrors kyber_config_server.py's restart_kyber_service() — a saved sound
    profile is read once at the brain's launch, same as a saved personality,
    so it needs the same immediate restart rather than going stale silently."""
    try:
        subprocess.Popen(['sudo', 'systemctl', 'restart', 'kyber.service'])
    except Exception:
        pass


def load_progress():
    global labels, skipped, current_index
    save_path = get_save_path()
    if os.path.exists(save_path):
        with open(save_path) as f:
            data = json.load(f)
        if "sound_to_emotions" in data:
            labels = data["sound_to_emotions"]
        elif "labels" in data:
            labels = data["labels"]
        else:
            labels = {}
        skipped = set(data.get("skipped", []))
        current_index = 0  # Always start at the beginning
        print(f"Loaded sound profile {get_active_sound_profile()}: {len(labels)} labeled, {len(skipped)} skipped")
    else:
        labels = {}
        skipped = set()
        current_index = 0
        print(f"Starting fresh — sound profile {get_active_sound_profile()}")


def save_progress():
    sound_profile = get_active_sound_profile()
    save_path = get_save_path()

    emotion_map = {}
    for key, emotion_list in labels.items():
        parts = key.split("_")
        if len(parts) != 2:
            continue
        bank_id, sound_id = int(parts[0]), int(parts[1])
        for emotion in emotion_list:
            if emotion not in emotion_map:
                emotion_map[emotion] = []
            emotion_map[emotion].append({"bank_id": bank_id, "sound_id": sound_id})

    # Preserve existing fields (like "name") from the JSON
    existing = {}
    if os.path.exists(save_path):
        try:
            with open(save_path) as f:
                existing = json.load(f)
        except Exception:
            pass

    existing.update({
        "sound_profile": sound_profile,
        "current_index": current_index,
        "emotion_to_sounds": emotion_map,
        "sound_to_emotions": labels,
        "skipped": list(skipped),
        "stats": {
            "total": len(SOUND_LIST),
            "labeled": len(labels),
            "skipped": len(skipped)
        }
    })

    with open(save_path, "w") as f:
        json.dump(existing, f, indent=2)

    print(f"Saved sound profile {sound_profile} map to {save_path}")


async def play_sound_via_brain(bank_id: int, sound_id: int):
    """Send play command to brain's local API instead of talking to droid directly."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BRAIN_API}/play",
                json={"bank_id": bank_id, "sound_id": sound_id},
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    print(f"[WARN] Brain API error: {result.get('reason', 'unknown')}")
    except Exception as e:
        print(f"[WARN] Could not reach brain API: {e}")


async def play_current():
    if current_index < len(SOUND_LIST):
        s = SOUND_LIST[current_index]
        await play_sound_via_brain(s["bank_id"], s["sound_id"])
        print(f"Playing: {s['label']}")


# ── API Routes ──────────────────────────────────────────────────────────────

async def api_status(request):
    touch_activity()
    s = SOUND_LIST[current_index] if current_index < len(SOUND_LIST) else None
    key = sound_key(s["bank_id"], s["sound_id"]) if s else None
    sound_profile = get_active_sound_profile()
    return web.json_response({
        "current_index": current_index,
        "total": len(SOUND_LIST),
        "current": s,
        "current_key": key,
        "current_labels": labels.get(key, []),
        "labeled": len(labels),
        "skipped": len(skipped),
        "done": current_index >= len(SOUND_LIST),
        "sound_profile": sound_profile,
        "name": read_sound_profile_name(sound_profile),
    })


async def api_play(request):
    touch_activity()
    await play_current()
    s = SOUND_LIST[current_index]
    return web.json_response({"ok": True, "playing": s["label"]})


async def api_next(request):
    global current_index
    touch_activity()
    if current_index < len(SOUND_LIST) - 1:
        current_index += 1
    return web.json_response({"ok": True, "current_index": current_index})


async def api_back(request):
    global current_index
    touch_activity()
    if current_index > 0:
        current_index -= 1
    return web.json_response({"ok": True, "current_index": current_index})


async def api_skip(request):
    global current_index
    touch_activity()
    s = SOUND_LIST[current_index]
    key = sound_key(s["bank_id"], s["sound_id"])
    skipped.add(key)
    save_progress()
    if current_index < len(SOUND_LIST) - 1:
        current_index += 1
    return web.json_response({"ok": True, "skipped": key})


async def api_label(request):
    global current_index
    touch_activity()
    data = await request.json()
    s = SOUND_LIST[current_index]
    key = sound_key(s["bank_id"], s["sound_id"])
    emotion_list = data.get("emotions", [])

    if emotion_list:
        labels[key] = emotion_list
        skipped.discard(key)
        save_progress()
        if current_index < len(SOUND_LIST) - 1:
            current_index += 1
            await play_current()
        return web.json_response({"ok": True, "labeled": key, "emotions": emotion_list})
    return web.json_response({"ok": False, "reason": "no emotions provided"})


async def api_jump(request):
    global current_index
    touch_activity()
    data = await request.json()
    idx = data.get("index", current_index)
    if 0 <= idx < len(SOUND_LIST):
        current_index = idx
    return web.json_response({"ok": True, "current_index": current_index})


async def api_save(request):
    touch_activity()
    data = {}
    try:
        data = await request.json()
    except Exception:
        pass
    name = (data.get("name") or "").strip()
    sound_profile = get_active_sound_profile()
    if name:
        write_sound_profile_name(sound_profile, name)
    save_progress()
    saved_name = read_sound_profile_name(sound_profile)
    restart_kyber_service()
    return web.json_response({
        "ok": True,
        "saved_to": f"sound_profile_{sound_profile}.json",
        "name": saved_name,
        "labeled": len(labels),
    })


async def api_shutdown(request):
    """Save progress then shut down — brain keeps running."""
    if labels:
        save_progress()
    print("\n[SHUTDOWN]: Sound mapper closed by user.")
    asyncio.get_event_loop().call_later(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return web.json_response({"ok": True})


async def index(request):
    touch_activity()
    html_path = os.path.join(PROJECT_DIR, "sound_discovery_ui.html")
    with open(html_path) as f:
        return web.Response(text=f.read(), content_type="text/html")


# ── Startup ─────────────────────────────────────────────────────────────────

async def startup(app):
    # Verify brain API is reachable before proceeding
    print("Checking brain API connection...")
    for attempt in range(10):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{BRAIN_API}/ready",
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    data = await resp.json()
                    if data.get("ready"):
                        print("Brain API connected — droid ready")
                        break
        except Exception:
            pass
        print(f"  Waiting for brain API... ({attempt + 1}/10)")
        await asyncio.sleep(2)
    else:
        print("[WARN] Could not reach brain API — sounds may not play")

    # Tell brain to suspend listening while mapper is active
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(f"{BRAIN_API}/mode", json={"active": True},
                               timeout=aiohttp.ClientTimeout(total=2))
    except Exception:
        pass

    load_progress()
    print(f"Ready — {len(SOUND_LIST)} sounds to explore")


async def shutdown_handler(app):
    if labels:
        save_progress()
    # Tell brain to resume listening
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(f"{BRAIN_API}/mode", json={"active": False},
                               timeout=aiohttp.ClientTimeout(total=2))
    except Exception:
        pass


def main():
    app = web.Application()
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown_handler)

    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/play", api_play)
    app.router.add_post("/api/next", api_next)
    app.router.add_post("/api/back", api_back)
    app.router.add_post("/api/skip", api_skip)
    app.router.add_post("/api/label", api_label)
    app.router.add_post("/api/jump", api_jump)
    app.router.add_post("/api/save", api_save)
    app.router.add_post("/api/shutdown", api_shutdown)

    print(f"\nDroid Sound Mapper — {len(SOUND_LIST)} sounds")
    print(f"Open http://r2-unit.local:{PORT} in your browser\n")
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
