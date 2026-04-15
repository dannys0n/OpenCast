#!/usr/bin/env python3

import argparse
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import prompt_queue_v3 as queue_v3
from tts_client import build_config as build_tts_config


REPO_ROOT = SCRIPT_DIR.parents[3]


DEFAULT_COLOR_TEXT = (
    "Everything is slowing down here. The map is stretched thin, the spacing is awkward, "
    "and nobody wants to give away the round with one bad peek. This is the kind of silence "
    "that usually breaks all at once."
)
DEFAULT_EVENT_TEXT = "Niko finds the opener."
DEFAULT_FOLLOWUP_TEXT = "That cracks A wide open."
DEFAULT_SECOND_EVENT_TEXT = "M0NESY doubles back."
DEFAULT_SECOND_FOLLOWUP_TEXT = "Now the rotate is late."


def stamp():
    return time.monotonic()


def format_delta(started_at, event_time):
    if event_time is None:
        return "-"
    return f"{event_time - started_at:0.3f}s"


def wait_for_tts_health(tts_config):
    request = urllib.request.Request(
        f"{tts_config.api_base}/health",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            if response.status != 200:
                raise RuntimeError(f"TTS health returned HTTP {response.status}")
    except urllib.error.URLError as error:
        raise RuntimeError(f"TTS server is not ready at {tts_config.api_base}: {error}") from error


def build_items(payload_sequence, event_text, followup_text):
    items = [
        queue_v3.build_queue_item(
            commentary=event_text,
            caster="caster0",
            prompt_style="play_by_play_event",
            tag="event",
            payload_sequence=payload_sequence,
            source="manual_test",
        )
    ]
    if followup_text.strip():
        items.append(
            queue_v3.build_queue_item(
                commentary=followup_text,
                caster="caster1",
                prompt_style="play_by_play_follow_up",
                tag="followup",
                payload_sequence=payload_sequence,
                source="manual_test",
            )
        )
    return items


def describe_record(prefix, items, dropped_items, interrupted_current):
    print(prefix)
    print("  queued:")
    for item in items:
        print(f"    - [{item['tag']}] {item['commentary']}")
    if dropped_items:
        print("  dropped queued non-events:")
        for item in dropped_items:
            print(f"    - [{item['tag']}] {item['commentary']}")
    if interrupted_current:
        print(f"  interrupted current: [{interrupted_current['tag']}] {interrupted_current['commentary']}")


def mark_dropped_items(items, dropped_items):
    dropped_ids = {item["id"] for item in dropped_items}
    for item in items:
        if item["id"] in dropped_ids:
            item["dropped"] = True
            item["done_at"] = stamp()


def make_instrumented_playback(telemetry):
    def instrumented_play_tts_prompt_interruptibly(tts_config, tts_prompt, interrupt_event):
        current_item = queue_v3.CURRENT_PLAYBACK or {}
        item_id = current_item.get("id")
        entry = telemetry.setdefault(
            item_id,
            {
                "id": item_id,
                "tag": current_item.get("tag"),
                "commentary": current_item.get("commentary"),
            },
        )
        entry["playback_function_entered_at"] = stamp()

        player = None
        thread = None
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="gsi_tts_v3_instr_")
        result = {"done": False, "ok": False}
        first_audio_buffered = False
        interrupted = False

        def close_player_immediately():
            nonlocal player
            if player is None:
                return
            if player.stdin is not None:
                try:
                    player.stdin.close()
                except Exception:
                    pass
                player.stdin = None
            if player.poll() is None:
                try:
                    player.kill()
                except Exception:
                    pass
                try:
                    player.wait(timeout=0.5)
                except Exception:
                    pass

        def finish_fetch_and_cleanup(fetch_thread, temp_dir):
            try:
                if fetch_thread is not None:
                    fetch_thread.join()
            finally:
                temp_dir.cleanup()

        try:
            buffer_path = Path(temp_dir_obj.name) / "audio.pcm"
            buffer_path.touch()

            def fetch_wrapper():
                entry["tts_fetch_started_at"] = stamp()
                queue_v3.fetch_tts_audio_to_file(tts_config, tts_prompt, buffer_path, result, interrupt_event)
                entry["tts_fetch_finished_at"] = stamp()

            thread = threading.Thread(target=fetch_wrapper, daemon=True)
            thread.start()

            player = queue_v3.open_play_process(
                tts_config.sample_rate,
                speed=float(tts_prompt.get("speed") or 1.0),
            )
            entry["play_process_started_at"] = stamp()
            if player.stdin is None:
                raise RuntimeError("failed to open stdin for SoX play")

            offset = 0
            while True:
                if interrupt_event.is_set():
                    entry["playback_stopped_at"] = stamp()
                    entry["playback_result"] = "interrupted"
                    interrupted = True
                    return {"interrupted": True}

                size = buffer_path.stat().st_size if buffer_path.exists() else 0
                if size > offset:
                    if not first_audio_buffered:
                        first_audio_buffered = True
                        entry["first_audio_buffered_at"] = stamp()
                    with buffer_path.open("rb") as handle:
                        handle.seek(offset)
                        while True:
                            if interrupt_event.is_set():
                                entry["playback_stopped_at"] = stamp()
                                entry["playback_result"] = "interrupted"
                                interrupted = True
                                return {"interrupted": True}
                            chunk = handle.read(min(16384, size - offset))
                            if not chunk:
                                break
                            player.stdin.write(chunk)
                            player.stdin.flush()
                            if "first_audio_written_to_player_at" not in entry:
                                entry["first_audio_written_to_player_at"] = stamp()
                            offset += len(chunk)
                    continue

                if result.get("done"):
                    if not result.get("ok"):
                        raise RuntimeError(result.get("error") or "TTS request failed")
                    break

                time.sleep(0.01)

            player.stdin.close()
            player.stdin = None
            return_code = player.wait()
            entry["playback_stopped_at"] = stamp()
            if return_code != 0:
                raise RuntimeError(f"SoX play exited with status {return_code}")
            entry["playback_result"] = "played"
            return {"interrupted": False}
        finally:
            close_player_immediately()
            entry.setdefault("playback_stopped_at", stamp())
            if interrupted:
                cleanup_thread = threading.Thread(
                    target=finish_fetch_and_cleanup,
                    args=(thread, temp_dir_obj),
                    daemon=True,
                    name="gsi-v3-tts-instr-cleanup",
                )
                cleanup_thread.start()
            else:
                finish_fetch_and_cleanup(thread, temp_dir_obj)

    return instrumented_play_tts_prompt_interruptibly


def print_timeline(title, item, telemetry, started_at):
    entry = telemetry.get(item["id"], {})
    print(f"{title} [{item['tag']}] {item['commentary']}")
    print(f"  queued:                 {format_delta(started_at, item.get('queued_at'))}")
    print(f"  playback fn entered:    {format_delta(started_at, entry.get('playback_function_entered_at'))}")
    print(f"  tts fetch started:      {format_delta(started_at, entry.get('tts_fetch_started_at'))}")
    print(f"  play proc started:      {format_delta(started_at, entry.get('play_process_started_at'))}")
    print(f"  first audio buffered:   {format_delta(started_at, entry.get('first_audio_buffered_at'))}")
    print(f"  first audio to player:  {format_delta(started_at, entry.get('first_audio_written_to_player_at'))}")
    print(f"  interrupt requested:    {format_delta(started_at, item.get('interrupt_requested_at'))}")
    print(f"  playback stopped:       {format_delta(started_at, entry.get('playback_stopped_at'))}")
    print(f"  done_event set:         {format_delta(started_at, item.get('done_at'))}")
    if item.get("interrupt_requested_at") is not None and entry.get("playback_stopped_at") is not None:
        print(f"  stop after interrupt:   {entry['playback_stopped_at'] - item['interrupt_requested_at']:.3f}s")
    print(f"  result:                 {entry.get('playback_result', '-')}")


def main():
    parser = argparse.ArgumentParser(
        description="Live harness for testing v3 TTS queue ordering and non-event cutoff."
    )
    parser.add_argument("--color-head-start", type=float, default=1.4, help="Seconds to let the initial non-event line play before firing event 1.")
    parser.add_argument("--event-gap", type=float, default=0.5, help="Seconds between event 1 enqueue and event 2 enqueue.")
    parser.add_argument("--color-text", default=DEFAULT_COLOR_TEXT)
    parser.add_argument("--event-text", default=DEFAULT_EVENT_TEXT)
    parser.add_argument("--followup-text", default=DEFAULT_FOLLOWUP_TEXT)
    parser.add_argument("--second-event-text", default=DEFAULT_SECOND_EVENT_TEXT)
    parser.add_argument("--second-followup-text", default=DEFAULT_SECOND_FOLLOWUP_TEXT)
    args = parser.parse_args()

    tts_config = build_tts_config(REPO_ROOT)
    wait_for_tts_health(tts_config)

    started_at = stamp()
    telemetry = {}
    original_playback = queue_v3.play_tts_prompt_interruptibly
    queue_v3.play_tts_prompt_interruptibly = make_instrumented_playback(telemetry)

    queue_v3.reset_prompt_runtime_state()
    queue_v3.ensure_queue_worker(REPO_ROOT)

    try:
        print("Starting live v3 queue cutoff test.")
        print(f"TTS API: {tts_config.api_base}")
        print(f"Queue state file: {queue_v3.PROMPT_QUEUE_STATE_PATH}")
        print()

        color_item = queue_v3.build_queue_item(
            commentary=args.color_text,
            caster="caster1",
            prompt_style="idle_color",
            tag="idle",
            payload_sequence=1,
            source="manual_test",
        )
        color_item["queued_at"] = stamp()
        queue_v3.enqueue_prompt_items([color_item], REPO_ROOT)
        print("Step 1: queued long non-event idle line.")
        print(f"  - [{color_item['tag']}] {color_item['commentary']}")

        time.sleep(max(args.color_head_start, 0.0))

        event1_items = build_items(2, args.event_text, args.followup_text)
        interrupt_requested_at = stamp()
        dropped_items, interrupted_current = queue_v3.prepare_queue_for_event_trigger()
        if interrupted_current and interrupted_current["id"] == color_item["id"]:
            color_item["interrupt_requested_at"] = interrupt_requested_at
        mark_dropped_items([color_item, *event1_items], dropped_items)
        for item in event1_items:
            item["queued_at"] = stamp()
        queue_v3.enqueue_prompt_items(event1_items, REPO_ROOT)
        describe_record("Step 2: event 1 generated; non-events cleared and event 1 queued.", event1_items, dropped_items, interrupted_current)

        time.sleep(max(args.event_gap, 0.0))

        event2_items = build_items(3, args.second_event_text, args.second_followup_text)
        interrupt_requested_at_2 = stamp()
        dropped_items_2, interrupted_current_2 = queue_v3.prepare_queue_for_event_trigger()
        if interrupted_current_2 is not None:
            if interrupted_current_2["id"] == color_item["id"]:
                color_item["interrupt_requested_at"] = interrupt_requested_at_2
            for item in event1_items + event2_items:
                if item["id"] == interrupted_current_2["id"]:
                    item["interrupt_requested_at"] = interrupt_requested_at_2
                    break
        mark_dropped_items([color_item, *event1_items, *event2_items], dropped_items_2)
        for item in event2_items:
            item["queued_at"] = stamp()
        queue_v3.enqueue_prompt_items(event2_items, REPO_ROOT)
        describe_record("Step 3: event 2 generated while event 1 should already be speaking.", event2_items, dropped_items_2, interrupted_current_2)

        waited_items = [color_item, *event1_items, *event2_items]
        print()
        print("Waiting for queued items to finish...")
        for item in waited_items:
            if item.get("dropped"):
                print(f"  - [{item['tag']}] {item['commentary']} -> dropped")
                continue
            item["done_event"].wait()
            item["done_at"] = stamp()
            result = item.get("playback_result") or {}
            status = "interrupted" if result.get("interrupted") else "played"
            if result.get("failed"):
                status = "failed"
            print(f"  - [{item['tag']}] {item['commentary']} -> {status}")

        print()
        print("Timing summary")
        print("--------------")
        print_timeline("Color item", color_item, telemetry, started_at)
        for index, item in enumerate(event1_items, start=1):
            print_timeline(f"Event 1 item {index}", item, telemetry, started_at)
        for index, item in enumerate(event2_items, start=1):
            print_timeline(f"Event 2 item {index}", item, telemetry, started_at)

        if color_item.get("interrupt_requested_at") is not None:
            event1_entry = telemetry.get(event1_items[0]["id"], {})
            if event1_entry.get("first_audio_written_to_player_at") is not None:
                print()
                print(
                    "Key delay: cut request -> event 1 first audio = "
                    f"{event1_entry['first_audio_written_to_player_at'] - color_item['interrupt_requested_at']:.3f}s"
                )

        print()
        print("Done. Expected behavior:")
        print("  1. The first idle line starts speaking.")
        print("  2. Event 1 cuts off that idle line.")
        print("  3. Event 2 does not cut off event 1; it queues behind it.")
    finally:
        queue_v3.play_tts_prompt_interruptibly = original_playback


if __name__ == "__main__":
    main()
