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
