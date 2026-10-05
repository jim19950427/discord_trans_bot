"""Run deploy.sh end to end against a fake `ssh` that executes the remote
command locally, with DEPLOY_DEST pointing at a temp directory."""
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CODE_FILES = ["bot.py", "translator.py", "translation_providers.py", "config.py", "glossary.py"]

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX and a non-root user (read-only files must reject writes)",
)

FAKE_SSH = """#!/bin/bash
while [[ "$1" == -* ]]; do shift 2; done
shift   # host
if [ $# -eq 1 ]; then exec bash -c "$1"; else exec "$@"; fi
"""


@pytest.fixture
def nas(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    dest = tmp_path / "dest"
    (dest / "data").mkdir(parents=True)
    (dest / "data" / "status.json").write_text('{"last_start": "old"}')
    for name in CODE_FILES:
        (dest / name).write_text(f"OLD {name}\n")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DEPLOY_DEST=str(dest),
        DEPLOY_POLL_INTERVAL="0.2",
    )
    return dest, env


def run_deploy(env):
    return subprocess.run(
        ["bash", str(ROOT / "deploy.sh")], env=env, capture_output=True, text=True, timeout=60
    )


def test_successful_deploy_replaces_files_in_place_and_cleans_up(nas):
    dest, env = nas
    inode_before = (dest / "bot.py").stat().st_ino

    def fake_restart():
        time.sleep(0.5)
        (dest / "data" / "status.json").write_text('{"last_start": "new"}')

    thread = threading.Thread(target=fake_restart)
    thread.start()
    result = run_deploy(env)
    thread.join()

    assert result.returncode == 0, result.stdout + result.stderr
    for name in CODE_FILES:
        assert (dest / name).read_bytes() == (ROOT / name).read_bytes()
    assert (dest / "bot.py").stat().st_ino == inode_before  # bind-mount safe
    assert not list(dest.glob("*.new")) and not list(dest.glob("*.bak"))


def test_failure_midway_restores_every_file(nas):
    dest, env = nas
    # translator.py rejects writes, so the swap fails after bot.py was replaced.
    (dest / "translator.py").chmod(stat.S_IRUSR | stat.S_IRGRP)

    result = run_deploy(env)

    assert result.returncode != 0
    assert "已用備份還原" in result.stdout
    for name in CODE_FILES:
        assert (dest / name).read_text() == f"OLD {name}\n", name
    assert not list(dest.glob("*.bak"))
    assert (dest / "bot.py.new").exists()  # staged files kept for inspection


def test_failed_restore_keeps_backups_and_reports_distinct_error(nas, tmp_path):
    dest, env = nas
    # Fake `cat`: the swap of translator.py writes a partial file then fails
    # (disk full), and restoring translator.py from its backup fails too.
    fake_bin = Path(env["PATH"].split(":")[0])
    real_cat = subprocess.run(["which", "cat"], capture_output=True, text=True).stdout.strip()
    fake_cat = fake_bin / "cat"
    fake_cat.write_text(f"""#!/bin/bash
case "$1" in
  *translator.py.new) printf PARTIAL; exit 1 ;;
  *translator.py.bak) exit 1 ;;
esac
exec {real_cat} "$@"
""")
    fake_cat.chmod(0o755)

    result = run_deploy(env)

    assert result.returncode != 0
    assert "還原也失敗" in result.stdout
    assert (dest / "translator.py.bak").read_text() == "OLD translator.py\n"
    # The failed restore truncated the file first, so the damage is real and
    # the .bak above is the only good copy.
    assert (dest / "translator.py").read_text() != "OLD translator.py\n"
    # Everything that could be restored was.
    for name in ("bot.py", "translation_providers.py", "config.py", "glossary.py"):
        assert (dest / name).read_text() == f"OLD {name}\n", name
    # Every backup is kept so nothing is lost.
    for name in CODE_FILES:
        assert (dest / f"{name}.bak").exists(), name
