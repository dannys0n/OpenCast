import os
from pathlib import Path


def load_local_env_values():
    env_path = Path(__file__).with_name(".env")
    values = {}

    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")

    return values


LOCAL_ENV = load_local_env_values()


def set_dual_default(name, value):
    if name not in os.environ and name not in LOCAL_ENV:
        os.environ[name] = value


os.environ["VOICE_SELECTION_MODE"] = "dual"
set_dual_default("VOICE_NAME", "scrawny_e0")
set_dual_default("SECONDARY_VOICE_NAME", "june")
set_dual_default("DUAL_VOICE_HEURISTIC", "casting_roles")
set_dual_default("SECONDARY_VOICE_PROBABILITY", "0.5")

from cs2_gsi_listener_mono_commentator import main


if __name__ == "__main__":
    main()
