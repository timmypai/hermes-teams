#!/opt/hermes/.venv/bin/python
"""Install devflow into the Hermes container (run as the hermes user).

Idempotent. Configures the 7 profiles (default = Hermes) from roles.json:
SOUL, profile description, skills (team + mattpocock + pstack allowlist),
config keys, profile .env, developer Codex launcher, and removes the
superseded seven-bot skills/cron (moved to a timestamped backup).
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ruamel.yaml import YAML

os.umask(0o022)

DATA = Path("/opt/data")
ROOT = DATA / "devflow"
ROLES = json.loads((ROOT / "roles.json").read_text())
VENDOR = ROOT / "vendor" / "mattpocock-skills"
MP_REPO = "https://github.com/mattpocock/skills"
STAMP = time.strftime("%Y%m%dT%H%M%S")
BACKUP = DATA / "devflow-backup" / STAMP
HERMES = shutil.which("hermes") or "/opt/hermes/bin/hermes"
MARK_BEGIN, MARK_END = "<!-- devflow:begin -->", "<!-- devflow:end -->"
log_lines: list[str] = []


def log(msg: str) -> None:
    print(msg)
    log_lines.append(msg)


def home_of(role: str) -> Path:
    return DATA if role == "default" else DATA / "profiles" / role


def sh(cmd: list[str], *, home: Path | None = None, check: bool = True, env_extra: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN")}
    if home is not None:
        env["HERMES_HOME"] = str(home)
    env.update(env_extra or {})
    p = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if check and p.returncode != 0:
        sys.exit(f"FAILED {' '.join(cmd)}\n{p.stdout}\n{p.stderr}")
    return p.stdout


def backup(path: Path) -> None:
    if not path.exists():
        return
    dest = BACKUP / path.relative_to(DATA)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(path, dest)


def retire(path: Path) -> None:
    """Move a superseded file/dir into the backup."""
    dest = BACKUP / "retired" / path.relative_to(DATA)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    log(f"retired {path}")


# --- skills -----------------------------------------------------------------

def vendor_mattpocock() -> dict[str, Path]:
    if (VENDOR / ".git").exists():
        sh(["git", "-C", str(VENDOR), "pull", "-q", "--ff-only"], check=False)
    else:
        VENDOR.parent.mkdir(parents=True, exist_ok=True)
        sh(["git", "clone", "-q", "--depth", "1", MP_REPO, str(VENDOR)])
    rev = sh(["git", "-C", str(VENDOR), "rev-parse", "--short", "HEAD"]).strip()
    log(f"mattpocock/skills @ {rev}")
    found: dict[str, Path] = {}
    for skill_md in sorted((VENDOR / "skills").glob("*/*/SKILL.md")):
        found.setdefault(skill_md.parent.name, skill_md.parent)
    return found


def pstack_names() -> list[str]:
    d = DATA / "plugins" / "pstack" / "skills"
    return sorted(p.name for p in d.iterdir() if (p / "SKILL.md").exists())


def build_role_dir(role: str, spec: dict, mp: dict[str, Path]) -> Path:
    d = ROOT / "roles" / role
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for name in spec["team"]:
        (d / name).symlink_to(ROOT / "skills" / name)
    for name in spec["mattpocock"]:
        if name not in mp:
            sys.exit(f"mattpocock skill {name!r} not found in {VENDOR}")
        (d / name).symlink_to(mp[name])
    return d


def retire_obsolete_skills() -> None:
    patterns = ROLES["obsolete_skills"]["root"]
    for skill_md in list((DATA / "skills").rglob("SKILL.md")):
        skill = skill_md.parent
        if skill.exists() and any(fnmatch.fnmatch(skill.name, pat) for pat in patterns):
            retire(skill)
    for prof in sorted((DATA / "profiles").iterdir()):
        sk = prof / "skills"
        if not sk.is_dir():
            continue
        for child in sorted(sk.iterdir()):
            if any(fnmatch.fnmatch(child.name, pat) for pat in ROLES["obsolete_skills"]["profiles"]):
                retire(child)
    for snap in [DATA / ".skills_prompt_snapshot.json", *DATA.glob("profiles/*/.skills_prompt_snapshot.json")]:
        if snap.exists():
            snap.unlink()


# --- per-profile ----------------------------------------------------------------

def base_persona() -> str:
    text = (DATA / "SOUL.md").read_text() if (DATA / "SOUL.md").exists() else ""
    first = text.split("\n", 1)[0].strip()
    return first if first.startswith("You are Hermes Agent") else (
        "You are Hermes Agent, built by Nous Research. Be direct and concise.")


def write_soul(role: str, home: Path, persona: str) -> None:
    body = (ROOT / "souls" / f"{role}.md").read_text().strip()
    (home / "SOUL.md").write_text(f"{persona}\n\n{MARK_BEGIN}\n{body}\n{MARK_END}\n")


def write_profile_yaml(home: Path, spec: dict) -> None:
    path = home / "profile.yaml"
    yaml = YAML()
    yaml.width = 4096
    data = yaml.load(path.read_text()) if path.exists() else {}
    data = data or {}
    data["description"] = spec["description"]
    data["description_auto"] = False
    with path.open("w") as fh:
        yaml.dump(data, fh)


def config_set(home: Path, key: str, value: str) -> None:
    sh([HERMES, "config", "set", key, value], home=home)


MISSING = object()


def raw_config(home: Path) -> dict:
    path = home / "config.yaml"
    return (YAML(typ="safe").load(path.read_text()) if path.exists() else None) or {}


def config_value(cfg: dict, key: str):
    node = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def as_yaml_value(value: str):
    """roles.json values are CLI strings; JSON-decode them to compare with config.yaml."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def enabled_plugins(cfg: dict) -> list[str]:
    return [str(x) for x in ((cfg.get("plugins") or {}).get("enabled") or [])]


def set_env(home: Path, values: dict[str, str]) -> None:
    path = home / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


def gh_login(gh_dir: str) -> str:
    return sh(["gh", "api", "user", "--jq", ".login"], env_extra={"GH_CONFIG_DIR": gh_dir}).strip()


def configure_role(role: str, spec: dict, mp: dict[str, Path], pstack: list[str], persona: str,
                   logins: dict[str, str]) -> None:
    home = home_of(role)
    if not home.exists():
        sys.exit(f"profile {role} missing at {home}; create it with `hermes profile create {role}` first")
    for f in ("SOUL.md", "config.yaml", "profile.yaml"):
        backup(home / f)
    role_dir = build_role_dir(role, spec, mp)
    write_soul(role, home, persona)
    write_profile_yaml(home, spec)

    keep = set(spec["pstack"]) - set(ROLES["pstack_always_off"])
    disabled = [f"pstack:{n}" for n in pstack if n not in keep]
    settings = {
        "skills.external_dirs": json.dumps([str(role_dir)]),
        "skills.auto_load": json.dumps(spec["auto_load"]),
        "skills.disabled": json.dumps(disabled),
    }
    wanted = ["pstack", *spec.get("plugins", [])]
    for name in wanted:
        link = home / "plugins" / name
        if not (link.exists() or link.is_symlink()):
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(DATA / "plugins" / name)
    cfg = raw_config(home)
    names = enabled_plugins(cfg)
    if any(n not in names for n in wanted):
        settings["plugins.enabled"] = json.dumps([*names, *(n for n in wanted if n not in names)])
    if spec.get("worker", True):
        settings.update(ROLES["worker_config"])
        settings.update(ROLES["kanban_other"])
    else:
        settings.update(ROLES["kanban_dispatcher"])
    settings.update(spec.get("config", {}))
    # Each `hermes config` call costs ~3 s of CLI startup; only call it for keys that differ.
    # UPPER_SNAKE keys live in .env, not config.yaml, so they are always re-set.
    changed = [k for k, v in settings.items() if config_value(cfg, k) != as_yaml_value(v)]
    for key in changed:
        config_set(home, key, settings[key])
    unset = [k for k in [*ROLES["kanban_unset"], *spec.get("unset", [])] if config_value(cfg, k) is not MISSING]
    for key in unset:
        sh([HERMES, "config", "unset", key], home=home, check=False)

    login = logins[spec["gh"]]
    env = {
        "GH_CONFIG_DIR": ROLES["gh"][spec["gh"]],
        "GIT_AUTHOR_NAME": spec["git_name"], "GIT_COMMITTER_NAME": spec["git_name"],
        "GIT_AUTHOR_EMAIL": f"{login}@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": f"{login}@users.noreply.github.com",
    }
    env.update(ROLES["common_env"])
    if spec.get("worker", True):
        env.update(ROLES["worker_env"])
    backup(home / ".env")
    set_env(home, env)
    log(f"configured {role}: {len(spec['team'])} team + {len(spec['mattpocock'])} mattpocock + {len(keep)} pstack skills; "
        f"config {len(changed)} set / {len(unset)} unset")


def retire_obsolete_cron() -> None:
    for role in ROLES["roles"]:
        home = home_of(role)
        jobs = home / "cron" / "jobs.json"
        if not jobs.exists():
            continue
        data = json.loads(jobs.read_text())
        items = data.get("jobs", data) if isinstance(data, dict) else data
        for job in list(items):
            if job.get("name") in ROLES["obsolete_cron_names"]:
                backup(jobs)
                sh([HERMES, "cron", "remove", job["id"]], home=home, check=False)
                log(f"removed cron {job['name']} ({role})")


def install_developer_codex() -> None:
    codex_home = DATA / "profiles" / "developer" / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    backup(codex_home / "AGENTS.md")
    shutil.copy2(ROOT / "codex" / "AGENTS.md", codex_home / "AGENTS.md")
    for f in ("codex-developer", "devflow"):
        (ROOT / "bin" / f).chmod(0o755)
    link = DATA / ".local" / "bin" / "devflow"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(ROOT / "bin" / "devflow")
    # Retire only named skills: the old seven-bot contract, plus Codex copies whose rules ub-mode
    # now owns (TDD, root causes, behavior tests). Everything else in $CODEX_HOME/skills stays.
    names = ROLES["roles"]["developer"]["codex_skills"]
    skills = codex_home / "skills"
    skills.mkdir(exist_ok=True)
    for pat in ROLES["obsolete_skills"]["codex"]:
        for path in sorted(skills.glob(pat)):
            retire(path)
    # Copied, not symlinked: Codex scans $CODEX_HOME/skills itself.
    for name in names:
        dest = skills / name
        if dest.exists():
            backup(dest)
            shutil.rmtree(dest)
        shutil.copytree(ROOT / "skills" / name, dest)
    log(f"developer Codex skills: {', '.join(names)}")


def main() -> None:
    BACKUP.mkdir(parents=True, exist_ok=True)
    for d in ("state", "runner/requests", "runner/status"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    mp = vendor_mattpocock()
    pstack = pstack_names()
    persona = base_persona()
    logins = {name: gh_login(path) for name, path in ROLES["gh"].items()}
    log(f"gh builder={logins['builder']} reviewer={logins['reviewer']}")
    retire_obsolete_skills()
    for role, spec in ROLES["roles"].items():
        configure_role(role, spec, mp, pstack, persona, logins)
    retire_obsolete_cron()
    install_developer_codex()
    (BACKUP / "install.log").write_text("\n".join(log_lines) + "\n")
    print(f"devflow installed; backup + log in {BACKUP}")


if __name__ == "__main__":
    main()
