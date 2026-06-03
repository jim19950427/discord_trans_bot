import json
import os

GLOSSARY_FILE = os.getenv("GLOSSARY_FILE", "/data/glossary.json")
SUBSTITUTIONS_FILE = os.getenv("SUBSTITUTIONS_FILE", "/data/substitutions.json")


def load_glossary() -> dict:
    """Returns {str(guild_id): {source_term: {target_lang: translation}}}"""
    if not os.path.exists(GLOSSARY_FILE):
        return {}
    try:
        with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_glossary(data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(GLOSSARY_FILE)), exist_ok=True)
    with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_glossary(guild_id: int, glossary_data: dict) -> dict:
    return glossary_data.get(str(guild_id), {})


def load_substitutions() -> dict:
    """Returns {str(guild_id): {source_term: replacement}}"""
    if not os.path.exists(SUBSTITUTIONS_FILE):
        return {}
    try:
        with open(SUBSTITUTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_substitutions(data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(SUBSTITUTIONS_FILE)), exist_ok=True)
    with open(SUBSTITUTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_substitutions(guild_id: int, sub_data: dict) -> dict:
    return sub_data.get(str(guild_id), {})


USER_LANGS_FILE = os.getenv("USER_LANGS_FILE", "/data/user_langs.json")


def load_user_langs() -> dict:
    """Returns {str(user_id): [lang_code, ...]}"""
    if not os.path.exists(USER_LANGS_FILE):
        return {}
    try:
        with open(USER_LANGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_langs(data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(USER_LANGS_FILE)), exist_ok=True)
    with open(USER_LANGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


CLUSTERS_FILE = os.getenv("CLUSTERS_FILE", "/data/msg_clusters.json")
THREAD_CLUSTERS_FILE = os.getenv("THREAD_CLUSTERS_FILE", "/data/thread_clusters.json")
CHANNEL_PINS_FILE = os.getenv("CHANNEL_PINS_FILE", "/data/channel_pins.json")


def _int_key_dict(d: dict) -> dict:
    return {int(k): v for k, v in d.items()}


def save_clusters(clusters: dict) -> None:
    serialized: dict = {}
    for msg_id, cluster in clusters.items():
        entry: dict = {
            "channels":    {str(k): v for k, v in cluster["channels"].items()},
            "contents":    {str(k): v for k, v in cluster["contents"].items()},
            "author":      cluster.get("author", ""),
            "avatar_url":  cluster.get("avatar_url", ""),
            "source_ch":   cluster["source_ch"],
            "source_lang": cluster["source_lang"],
        }
        for opt_key in ("thread_channels", "prefixes", "att_names", "att_urls"):
            if opt_key in cluster:
                entry[opt_key] = {str(k): v for k, v in cluster[opt_key].items()}
        if "embed_count" in cluster:
            entry["embed_count"] = cluster["embed_count"]
        serialized[str(msg_id)] = entry
    os.makedirs(os.path.dirname(os.path.abspath(CLUSTERS_FILE)), exist_ok=True)
    with open(CLUSTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(serialized, f, ensure_ascii=False)


def load_clusters() -> dict:
    if not os.path.exists(CLUSTERS_FILE):
        return {}
    try:
        with open(CLUSTERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result: dict = {}
        for msg_id_str, entry in raw.items():
            cluster: dict = {
                "channels":    _int_key_dict(entry["channels"]),
                "contents":    _int_key_dict(entry["contents"]),
                "author":      entry.get("author", ""),
                "avatar_url":  entry.get("avatar_url", ""),
                "source_ch":   int(entry["source_ch"]),
                "source_lang": entry["source_lang"],
            }
            for opt_key in ("thread_channels", "prefixes", "att_names", "att_urls"):
                if opt_key in entry:
                    cluster[opt_key] = _int_key_dict(entry[opt_key])
            if "embed_count" in entry:
                cluster["embed_count"] = entry["embed_count"]
            result[int(msg_id_str)] = cluster
        return result
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return {}


def save_thread_clusters(thread_clusters: dict) -> None:
    serialized = {
        str(tid): {str(k): v for k, v in mapping.items()}
        for tid, mapping in thread_clusters.items()
    }
    os.makedirs(os.path.dirname(os.path.abspath(THREAD_CLUSTERS_FILE)), exist_ok=True)
    with open(THREAD_CLUSTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(serialized, f)


def load_thread_clusters() -> dict:
    if not os.path.exists(THREAD_CLUSTERS_FILE):
        return {}
    try:
        with open(THREAD_CLUSTERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(tid): {int(k): v for k, v in mapping.items()} for tid, mapping in raw.items()}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def save_channel_pins(channel_pins: dict) -> None:
    serialized = {str(ch_id): list(pins) for ch_id, pins in channel_pins.items()}
    os.makedirs(os.path.dirname(os.path.abspath(CHANNEL_PINS_FILE)), exist_ok=True)
    with open(CHANNEL_PINS_FILE, "w", encoding="utf-8") as f:
        json.dump(serialized, f)


def load_channel_pins() -> dict:
    if not os.path.exists(CHANNEL_PINS_FILE):
        return {}
    try:
        with open(CHANNEL_PINS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(ch_id): set(pins) for ch_id, pins in raw.items()}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
