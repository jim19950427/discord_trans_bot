import json
import os

from config import atomic_write_json, quarantine_corrupt

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
    atomic_write_json(GLOSSARY_FILE, data, indent=2)


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
    atomic_write_json(SUBSTITUTIONS_FILE, data, indent=2)


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
    atomic_write_json(USER_LANGS_FILE, data, indent=2)


CLUSTERS_FILE = os.getenv("CLUSTERS_FILE", "/data/msg_clusters.json")
THREAD_CLUSTERS_FILE = os.getenv("THREAD_CLUSTERS_FILE", "/data/thread_clusters.json")
CHANNEL_PINS_FILE = os.getenv("CHANNEL_PINS_FILE", "/data/channel_pins.json")


def _int_key_dict(d: dict) -> dict:
    return {int(k): v for k, v in d.items()}


CLUSTERS_FORMAT_VERSION = 2


def _serialize_cluster(cluster: dict) -> dict:
    entry: dict = {
        "channels":    {str(k): v for k, v in cluster["channels"].items()},
        "contents":    {str(k): v for k, v in cluster["contents"].items()},
        "author":      cluster.get("author", ""),
        "avatar_url":  cluster.get("avatar_url", ""),
        "source_ch":   cluster["source_ch"],
        "source_lang": cluster["source_lang"],
        "raw_forward": cluster.get("raw_forward", False),
    }
    for opt_key in ("thread_channels", "prefixes", "att_names", "att_urls", "extra_parts"):
        if opt_key in cluster:
            entry[opt_key] = {str(k): v for k, v in cluster[opt_key].items()}
    if "embed_count" in cluster:
        entry["embed_count"] = cluster["embed_count"]
    return entry


def serialize_clusters(clusters: dict) -> dict:
    """Snapshot clusters into a JSON-ready structure, one entry per distinct
    cluster with the message ids ("keys") that point at it. Run this on the
    event-loop thread: clusters are shared mutable dicts, and serializing them
    from a worker thread while handlers edit them can raise mid-iteration."""
    entries: list[dict] = []
    by_identity: dict[int, dict] = {}
    for msg_id, cluster in clusters.items():
        entry = by_identity.get(id(cluster))
        if entry is None:
            entry = _serialize_cluster(cluster)
            entry["keys"] = []
            by_identity[id(cluster)] = entry
            entries.append(entry)
        entry["keys"].append(msg_id)
    return {"version": CLUSTERS_FORMAT_VERSION, "clusters": entries}


def write_clusters(serialized: dict) -> None:
    atomic_write_json(CLUSTERS_FILE, serialized, skip_if_unchanged=True)


def save_clusters(clusters: dict) -> None:
    write_clusters(serialize_clusters(clusters))


def _cluster_from_entry(entry: dict) -> dict:
    cluster: dict = {
        "channels":    _int_key_dict(entry["channels"]),
        "contents":    _int_key_dict(entry["contents"]),
        "author":      entry.get("author", ""),
        "avatar_url":  entry.get("avatar_url", ""),
        "source_ch":   int(entry["source_ch"]),
        "source_lang": entry["source_lang"],
        "raw_forward": entry.get("raw_forward", False),
    }
    for opt_key in ("thread_channels", "prefixes", "att_names", "att_urls", "extra_parts"):
        if opt_key in entry:
            cluster[opt_key] = _int_key_dict(entry[opt_key])
    if "embed_count" in entry:
        cluster["embed_count"] = entry["embed_count"]
    return cluster


def _clusters_from_raw(raw: dict) -> dict:
    result: dict = {}
    if "version" in raw:
        if raw["version"] != CLUSTERS_FORMAT_VERSION:
            raise ValueError(f"unsupported clusters format {raw['version']!r}")
        for entry in raw["clusters"]:
            cluster = _cluster_from_entry(entry)
            for msg_id in entry["keys"]:
                result[int(msg_id)] = cluster
        return result
    # Legacy format: one full copy of the cluster per message id. Copies that
    # are identical were one shared dict before the save, so re-share them —
    # otherwise an edit through one channel's copy never reaches the others.
    shared: dict[str, dict] = {}
    for msg_id_str, entry in raw.items():
        signature = json.dumps(entry, sort_keys=True)
        cluster = shared.get(signature)
        if cluster is None:
            cluster = shared[signature] = _cluster_from_entry(entry)
        result[int(msg_id_str)] = cluster
    return result


def load_clusters() -> dict:
    """Returns {msg_id: cluster}; every id of one forwarded message maps to the
    same dict object, as in the live bot."""
    if not os.path.exists(CLUSTERS_FILE):
        return {}
    try:
        with open(CLUSTERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _clusters_from_raw(raw)
    except OSError:
        return {}
    except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError):
        quarantine_corrupt(CLUSTERS_FILE)
        return {}


def serialize_thread_clusters(thread_clusters: dict) -> dict:
    return {
        str(tid): {str(k): v for k, v in mapping.items()}
        for tid, mapping in thread_clusters.items()
    }


def write_thread_clusters(serialized: dict) -> None:
    atomic_write_json(THREAD_CLUSTERS_FILE, serialized, skip_if_unchanged=True)


def save_thread_clusters(thread_clusters: dict) -> None:
    write_thread_clusters(serialize_thread_clusters(thread_clusters))


def load_thread_clusters() -> dict:
    if not os.path.exists(THREAD_CLUSTERS_FILE):
        return {}
    try:
        with open(THREAD_CLUSTERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(tid): {int(k): v for k, v in mapping.items()} for tid, mapping in raw.items()}
    except OSError:
        return {}
    except (json.JSONDecodeError, ValueError):
        quarantine_corrupt(THREAD_CLUSTERS_FILE)
        return {}


def serialize_channel_pins(channel_pins: dict) -> dict:
    return {str(ch_id): sorted(pins) for ch_id, pins in channel_pins.items()}


def write_channel_pins(serialized: dict) -> None:
    atomic_write_json(CHANNEL_PINS_FILE, serialized, skip_if_unchanged=True)


def save_channel_pins(channel_pins: dict) -> None:
    write_channel_pins(serialize_channel_pins(channel_pins))


def load_channel_pins() -> dict:
    if not os.path.exists(CHANNEL_PINS_FILE):
        return {}
    try:
        with open(CHANNEL_PINS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(ch_id): set(pins) for ch_id, pins in raw.items()}
    except OSError:
        return {}
    except (json.JSONDecodeError, ValueError):
        quarantine_corrupt(CHANNEL_PINS_FILE)
        return {}


PENDING_RETRIES_FILE = os.getenv("PENDING_RETRIES_FILE", "/data/pending_retries.json")


def save_pending_retries(entries: list[dict]) -> None:
    """Delayed translation retries that haven't run yet, so a restart/deploy
    inside the retry window doesn't drop them. Holds only ids and text — never
    webhook URLs (they embed auth tokens); those are resolved at run time."""
    atomic_write_json(PENDING_RETRIES_FILE, entries)


def load_pending_retries() -> list[dict]:
    if not os.path.exists(PENDING_RETRIES_FILE):
        return []
    try:
        with open(PENDING_RETRIES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except OSError:
        return []
    except ValueError:
        quarantine_corrupt(PENDING_RETRIES_FILE)
        return []
    if not isinstance(raw, list):
        return []
    required = {"text": str, "src": str, "dest": str, "guild_id": int,
                "ch_id": int, "msg_id": int, "due": (int, float)}
    return [
        entry for entry in raw
        if isinstance(entry, dict)
        and all(isinstance(entry.get(k), t) for k, t in required.items())
        and isinstance(entry.get("prefix", ""), str)
    ]
