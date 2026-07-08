#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# kyber_monitor.sh — thermal & resource diagnostic for KYBER
#
# Logs a snapshot every INTERVAL seconds to kyber_monitor.log.
# Run in the background while KYBER is running normally, then look for
# temperature spikes and correlate them with:
#   • KYBER_THREADS climbing over time → background thread accumulation
#   • BT_RSS_KB climbing over time    → D-Bus advertisement object leak
#   • ARECORD_COUNT > 1               → arecord subprocess pileup
#   • THROTTLED != "ok"               → Pi has already started throttling
#
# WHAT THE FIELDS MEAN:
#   TEMP_C        — CPU temperature (throttle starts ~70°C, hard cap ~80°C)
#   THROTTLED     — vcgencmd get_throttled hex; "ok" = 0x0 (no flags set)
#                   bit 0 = undervoltage, bit 1 = freq capped, bit 2 = throttled
#                   bits 16–19 = same but "ever happened since boot"
#   KYBER_RSS_KB  — KYBER's resident memory in KB (should be stable, not climbing)
#   KYBER_THREADS — total thread count for kyber_core.py (watch for creep)
#   KYBER_CPU     — KYBER's CPU% at the moment of the snapshot (ps rolling avg)
#   BT_RSS_KB     — bluetoothd's resident memory (climbs if D-Bus objects leak)
#   BT_CPU        — bluetoothd's CPU% (elevated if BlueZ is doing heavy radio work)
#   ARECORD_COUNT — number of live arecord processes (normally exactly 1)
#   RECONNECT     — "YES" if a reconnect log line appeared in the last INTERVAL seconds
#
# HOW TO RUN:
#   chmod +x ~/kyber/kyber_monitor.sh
#   nohup ~/kyber/kyber_monitor.sh &
#   echo $! > ~/kyber_monitor.pid    # save the PID so you can kill it later
#
# HOW TO WATCH LIVE (formatted):
#   tail -f ~/kyber/kyber_monitor.log | column -t -s '|'
#
# HOW TO STOP:
#   kill $(cat ~/kyber_monitor.pid)
#
# HOW TO CORRELATE WITH KYBER'S OWN LOGS:
#   When you see a temperature spike or thread count jump, note the timestamp
#   and run: journalctl -u kyber.service --since "2025-01-01 HH:MM:SS" --until "..."
#   Reconnect events show as "[BLE]: Silent disconnect" or "[BLE]: Connection lost"
# ─────────────────────────────────────────────────────────────────────────────

LOG=~/kyber/kyber_monitor.log
INTERVAL=15   # seconds between snapshots; increase to 30 if you want to run for days

# ── Startup ──────────────────────────────────────────────────────────────────
echo "# KYBER Monitor started $(date)" | tee -a "$LOG"
echo "# Logging every ${INTERVAL}s to $LOG" | tee -a "$LOG"
echo "# TIMESTAMP         | TEMP | THROTTLE | PID  | RSS_KB | THREADS | CPU% | BT_RSS | BT_CPU | AREC | RECONN" | tee -a "$LOG"
echo "# $(printf '%.0s-' {1..70})" >> "$LOG"

# ── Main loop ─────────────────────────────────────────────────────────────────
while true; do

    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    # ── Temperature ──────────────────────────────────────────────────────────
    RAW_TEMP=$(vcgencmd measure_temp 2>/dev/null)
    TEMP=$(echo "$RAW_TEMP" | grep -oP '[\d.]+' | head -1)
    [ -z "$TEMP" ] && TEMP="n/a"

    # ── Throttle state ───────────────────────────────────────────────────────
    RAW_THROTTLE=$(vcgencmd get_throttled 2>/dev/null)
    THROTTLE=$(echo "$RAW_THROTTLE" | grep -oP '0x[0-9a-fA-F]+' | head -1)
    if [ "$THROTTLE" = "0x0" ] || [ "$THROTTLE" = "0x00000" ] || [ "$THROTTLE" = "0x00000000" ]; then
        THROTTLE_FLAG="ok"
    elif [ -z "$THROTTLE" ]; then
        THROTTLE_FLAG="n/a"
    else
        THROTTLE_FLAG="!$THROTTLE"
    fi

    # ── kyber_core.py process ────────────────────────────────────────────────
    KYBER_PID=$(pgrep -f "kyber_core.py" 2>/dev/null | head -1)
    if [ -n "$KYBER_PID" ]; then
        # Single ps call for all three fields; trim leading whitespace
        read KYBER_RSS KYBER_THREADS KYBER_CPU < <(ps -o rss=,nlwp=,%cpu= -p "$KYBER_PID" 2>/dev/null | awk '{print $1, $2, $3}')
        KYBER_RSS=${KYBER_RSS:-"?"}
        KYBER_THREADS=${KYBER_THREADS:-"?"}
        KYBER_CPU=${KYBER_CPU:-"?"}
    else
        KYBER_PID="dead"
        KYBER_RSS="-"
        KYBER_THREADS="-"
        KYBER_CPU="-"
    fi

    # ── bluetoothd ───────────────────────────────────────────────────────────
    BT_PID=$(pgrep -x bluetoothd 2>/dev/null | head -1)
    if [ -n "$BT_PID" ]; then
        read BT_RSS BT_CPU < <(ps -o rss=,%cpu= -p "$BT_PID" 2>/dev/null | awk '{print $1, $2}')
        BT_RSS=${BT_RSS:-"?"}
        BT_CPU=${BT_CPU:-"?"}
    else
        BT_RSS="-"
        BT_CPU="-"
    fi

    # ── arecord subprocess count ─────────────────────────────────────────────
    # Normally exactly 1 (one recording chunk at a time).
    # Anything higher means previous invocations are piling up.
    ARECORD_COUNT=$(pgrep -c -x arecord 2>/dev/null || echo "0")

    # ── Reconnect event in the last INTERVAL seconds ─────────────────────────
    # "YES" if kyber.service logged a reconnect attempt since the last snapshot.
    # Makes it easy to line up temperature jumps with disconnect events.
    SINCE=$(date -d "@$(($(date +%s) - INTERVAL))" '+%Y-%m-%d %H:%M:%S' 2>/dev/null)
    if [ -n "$SINCE" ]; then
        RECONNECT_HIT=$(journalctl -u kyber.service --since "$SINCE" --no-pager -q 2>/dev/null \
            | grep -c "\[BLE\].*[Rr]econnect\|\[RECONNECT\]" 2>/dev/null || echo 0)
        [ "$RECONNECT_HIT" -gt 0 ] && RECONNECT="YES(${RECONNECT_HIT})" || RECONNECT="no"
    else
        RECONNECT="n/a"
    fi

    # ── Log the line ─────────────────────────────────────────────────────────
    LINE="$TIMESTAMP | $TEMP | $THROTTLE_FLAG | $KYBER_PID | $KYBER_RSS | $KYBER_THREADS | $KYBER_CPU | $BT_RSS | $BT_CPU | $ARECORD_COUNT | $RECONNECT"
    echo "$LINE" >> "$LOG"

    # ── Console alert on high temp or throttle ───────────────────────────────
    # Only prints if something looks wrong, so you can leave this running in
    # a terminal without it scrolling constantly.
    if [ "$THROTTLE_FLAG" != "ok" ] && [ "$THROTTLE_FLAG" != "n/a" ]; then
        echo "⚠  THROTTLING DETECTED at $TIMESTAMP — $THROTTLE_FLAG (temp: ${TEMP}°C)"
    elif (( $(echo "$TEMP >= 70" | bc -l 2>/dev/null || echo 0) )); then
        echo "🌡  HIGH TEMP: ${TEMP}°C at $TIMESTAMP (throttle: $THROTTLE_FLAG)"
    fi

    sleep "$INTERVAL"
done
