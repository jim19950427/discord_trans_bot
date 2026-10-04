import hashlib
import json
import os
import threading
import time

CONFIG_FILE = os.getenv("CONFIG_FILE", "channel_config.json")

# Digest of the last payload written per path, for skip_if_unchanged.
_last_digest: dict[str, str] = {}
_digest_lock = threading.Lock()


def atomic_write_text(path: str, text: str) -> None:
    """Write text so readers (and a crash/os._exit mid-write) only ever see the
    complete old file or the complete new one: write a sibling temp file,
    fsync it, then os.replace() it over the target. Only use this for files in
    a directory mount — replacing a single-file bind mount swaps the inode and
    the container would keep seeing the old file."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str, data, *, skip_if_unchanged: bool = False, **dump_kwargs
) -> bool:
    """Atomically write data as JSON. With skip_if_unchanged, the write is
    skipped when the serialized payload equals what this process last wrote
    to the same path (and the file still exists). Returns True if written."""
    dump_kwargs.setdefault("ensure_ascii", False)
    payload = json.dumps(data, **dump_kwargs)
    key = os.path.abspath(path)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    if skip_if_unchanged:
        with _digest_lock:
            if _last_digest.get(key) == digest and os.path.exists(key):
                return False
    atomic_write_text(key, payload)
    if skip_if_unchanged:
        with _digest_lock:
            _last_digest[key] = digest
    return True


def quarantine_corrupt(path: str) -> None:
    """Move an unreadable data file aside instead of letting the next save
    silently overwrite it, so the content can still be inspected/recovered."""
    try:
        os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    except OSError:
        pass


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
    atomic_write_json(CONFIG_FILE, serialisable, indent=2)
