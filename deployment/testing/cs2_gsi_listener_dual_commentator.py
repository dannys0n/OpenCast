import os


os.environ.setdefault("VOICE_SELECTION_MODE", "dual")
os.environ.setdefault("VOICE_NAME", "scotty")
os.environ.setdefault("SECONDARY_VOICE_NAME", "june")
os.environ.setdefault("DUAL_VOICE_HEURISTIC", "flip_flop")
os.environ.setdefault("SECONDARY_VOICE_PROBABILITY", "0.5")

from cs2_gsi_listener_mono_commentator import main


if __name__ == "__main__":
    main()
