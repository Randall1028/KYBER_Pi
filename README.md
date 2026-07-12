# KYBER

**K**inetic **Y**ammering and **B**ehavioral **E**ngine **R**outines — an open-source conversational AI brain for Galaxy's Edge Droid Depot droids.

KYBER turns your Droid Depot droid into a voice-driven companion: it listens, talks back with a personality you shape, reacts with real motion and sound, and runs autonomously off a Raspberry Pi Zero 2W tucked inside (or alongside) the droid itself.

This project is a hobbyist build, made for the Star Wars fan/maker community. It's free, and it's meant to stay that way — see [License](#license) below.

---

## What it does

- **Real conversation** — a persistent voice pipeline (wake-free listening, voice activity detection, speech-to-text, an LLM for both the reply and a live emotion read) drives everything else.
- **A personality you control** — five trait sliders (brave, curious, sassy, playful, sensitive) shape how your droid talks, independent of which sound bank it plays from. Mix and match freely.
- **Built-in character personalities** — locked, ready-to-use personality profiles for R2-D2, BB-8, Chopper, and BD-1, tuned to match each character. A-series ships with a neutral, blank-slate personality you can build out yourself.
- **Chassis-aware movement** — R, BB, C, A, and BD chassis types each get correctly scaled motor behavior for every gesture, calibrated to that chassis's actual turning and driving characteristics.
- **A calibration wizard** — walks you through a short spin-test to correct for your specific unit's left/right motor balance, so gestures land true regardless of small manufacturing variance.
- **Autonomous modes**, triggered by voice:
  - **Pet Entertainer** — fast, erratic movement bursts, meant to give a cat or dog something to chase.
  - **Expressive Mode** — more animated, frequent gesture movement during conversation.
  - **Hotel Sentry** — not a security patrol. It's a small, deliberate movement roughly every 15 minutes for up to 8 hours, timed specifically to keep a hotel room's motion-sensing AC/lights from timing out overnight. While active, it ignores everything it hears except the command to stop — so it won't get distracted
- **Beacon Relay** *(optional, on by default)* — scans for other droids and official Disney location beacons, and rebroadcasts your droid's own presence so nearby detectors and other droids can see it too.
- **Mainframe** — a browser-based control panel (reachable at `http://<your-droid>.local:5001`) for onboarding, personality editing, sound profile mapping, network setup, motor calibration, and switching between STT/LLM providers.
- **Multi-provider support** — choose your speech-to-text engine (Deepgram, Groq, OpenAI, Google, or AssemblyAI) and your LLM (Gemini, OpenAI, Anthropic, or Groq) independently, and switch anytime from Mainframe.

---

## Requirements

- A Raspberry Pi Zero 2W
- A Droid Depot droid (R-series, BB-series, C-series, A-series, or BD-series)
- A USB microphone
- API keys for at least one speech-to-text provider and one LLM provider (free tiers exist for several of these)

---

## Installation

1. Flash Raspberry Pi OS Lite onto your Pi (the [Raspberry Pi Imager](https://www.raspberrypi.com/software/) lets you set hostname, WiFi, and SSH access ahead of time under its advanced options).
2. Boot the Pi and SSH in.
3. Clone this repo to `~/kyber` (the path is hardcoded, so it needs to live there):
   ```bash
   git clone https://github.com/Randall1028/KYBER_Pi.git ~/kyber
   cd ~/kyber
   ```
4. Run the installer:
   ```bash
   ./install.sh
   ```
5. Once it finishes, open `http://<your-hostname>.local:5001` in a browser and follow the onboarding wizard — it walks you through API keys, choosing your droid's model and personality, WiFi, and pairing (claiming) your droid over Bluetooth.

That's it — once the wizard finishes, your droid is listening.

---

## License

KYBER is released under the [MIT License](LICENSE) — free to use, modify, and share, including commercially.

That said: KYBER is free because building droids should be fun and accessible, not a revenue stream. If you use it in a project, please credit everyone involved, and I'd love to hear what you build.

---

## Acknowledgments

Built on top of [`bleak`](https://github.com/hbldh/bleak), [`pyDroidDepot`](https://pypi.org/project/pyDroidDepot/), and [`bluezero`](https://github.com/ukBaz/python-bluezero).
