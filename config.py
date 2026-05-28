import json
import os

CONFIG_FILE = os.getenv("CONFIG_FILE", "channel_config.json")


def load_channel_config() -> dict[int, dict[int, dict]]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        raw: dict[str, dict[str, dict]] = json.load(f)
    return {
        int(guild_id): {int(ch_id): info for ch_id, info in channels.items()}
        for guild_id, channels in raw.items()
    }


def save_channel_config(config: dict[int, dict[int, dict]]) -> None:
    serialisable = {
        str(guild_id): {str(ch_id): info for ch_id, info in channels.items()}
        for guild_id, channels in config.items()
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(serialisable, f, ensure_ascii=False, indent=2)
