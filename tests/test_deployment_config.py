from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_libretranslate_is_internal_pinned_and_persistent():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["libretranslate"]
    assert service["image"] == "libretranslate/libretranslate:v1.9.6"
    assert "ports" not in service
    assert service["environment"]["LT_LOAD_ONLY"] == "en,es,fr,ja,ko,pl,ru,th,zt"
    assert str(service["environment"]["LT_THREADS"]) == "2"
    assert "libretranslate_models:/home/libretranslate/.local:rw" in service["volumes"]
    assert "healthcheck" in service
    assert "libretranslate_models" in compose["volumes"]


def test_provider_module_is_mounted_deployed_and_watched():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    mounts = compose["services"]["discord-trans-bot"]["volumes"]
    assert "./translation_providers.py:/app/translation_providers.py:ro" in mounts
    deploy = (ROOT / "deploy.sh").read_text()
    watcher = (ROOT / "bot.py").read_text()
    assert "translation_providers.py" in deploy
    assert "translation_providers.py" in watcher


def test_runtime_dependency_uses_requests_not_deep_translator():
    requirements = (ROOT / "requirements.txt").read_text().lower()
    assert "requests>=" in requirements
    assert "deep-translator" not in requirements
