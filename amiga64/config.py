"""The config file, the precedence rules and the first-run prompts.

docs/WAVE17_BRIEF.md "Rulings in force" (W17-K):
  device address  --host, AMIGA64_HOST, U64_HOST, then the config file;
  password        U64_PASSWORD or the config file, never the command line;
  ADF             --adf, the config, the single ./adf/*.adf, then a prompt.

The file is ``$XDG_CONFIG_HOME/amiga64/config.toml`` (default
``~/.config/amiga64/config.toml``), read with tomllib and written by the
small writer below (tables of strings and integers), mode 0600.  Run
reports (the device holds' config_before/after.json) and the staged files
list live under ``$XDG_STATE_HOME/amiga64`` (default
``~/.local/state/amiga64``).  AMIGA64_CONFIG (the file) and
AMIGA64_STATE_DIR override both (a clean run without touching XDG_CONFIG_HOME,
which VICE also reads).
"""

from __future__ import annotations

import glob
import os
import sys
import tomllib

HEADER = ("# amiga64 configuration (written by `amiga64`; edit freely).\n"
          "# [device] host, and optionally password, kind (U64E/C64U), mhz, volume\n"
          "# [disk] adf: your Workbench 3.1 ADF   [input] mouse_port: 0, 1 or 2\n"
          "# [[staged]] host, to, as: the files `amiga64 adf add` puts on the session disk\n")
ADF_DIR = "adf"                         # the default ADF directory, relative to the cwd


class ConfigError(RuntimeError):
    """A plain message for the user (the CLI prints it and exits 1)."""


def config_path() -> str:
    if os.environ.get("AMIGA64_CONFIG"):
        return os.path.abspath(os.path.expanduser(os.environ["AMIGA64_CONFIG"]))
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "amiga64", "config.toml")


def state_dir() -> str:
    if os.environ.get("AMIGA64_STATE_DIR"):
        return os.path.abspath(os.path.expanduser(os.environ["AMIGA64_STATE_DIR"]))
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"),
                                                            ".local", "state")
    return os.path.join(base, "amiga64")


def load(path: str | None = None) -> dict:
    path = path or config_path()
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None


def _toml_value(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise ConfigError(f"config values are strings or integers, not {v!r}")
    if isinstance(v, int):
        return str(v)
    out = ['"']
    for c in v:
        if c in '"\\':
            out.append("\\" + c)
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return "".join(out) + '"'


def dumps(data: dict) -> str:
    """Tables of strings/integers, and arrays of such tables ([[name]],
    e.g. the files `adf add` staged)."""
    lines = [HEADER]
    for table in sorted(data):
        items = data[table]
        rows = items if isinstance(items, list) else [items]
        if not rows or not all(isinstance(r, dict) for r in rows):
            if isinstance(items, list) and not items:
                continue                # an empty array of tables: nothing to write
            raise ConfigError(f"config: [{table}] must be a table or a list of tables")
        for r in rows:
            lines.append(f"\n[[{table}]]\n" if isinstance(items, list) else f"\n[{table}]\n")
            for k in sorted(r):
                if r[k] is None:
                    continue
                lines.append(f"{k} = {_toml_value(r[k])}\n")
    return "".join(lines)


def save(data: dict, path: str | None = None) -> str:
    path = path or config_path()
    text = dumps(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def get(data: dict, table: str, key: str, default=None):
    v = (data.get(table) or {}).get(key)
    return default if v in (None, "") else v


def put(data: dict, table: str, key: str, value) -> dict:
    data.setdefault(table, {})[key] = value
    return data


def resolve_host(flag: str | None, data: dict) -> tuple[str | None, str | None]:
    """(host, where it came from) or (None, None)."""
    for v, src in ((flag, "--host"), (os.environ.get("AMIGA64_HOST"), "AMIGA64_HOST"),
                   (os.environ.get("U64_HOST"), "U64_HOST"),
                   (get(data, "device", "host"), "config")):
        if v and v.strip():
            return v.strip(), src
    return None, None


def resolve_password(data: dict) -> str | None:
    return os.environ.get("U64_PASSWORD") or get(data, "device", "password")


def adf_candidates(cwd: str) -> list[str]:
    return sorted((p for p in glob.glob(os.path.join(cwd, ADF_DIR, "*"))
                   if p.lower().endswith(".adf") and os.path.isfile(p)),
                  key=lambda p: os.path.basename(p).lower())


def resolve_adf(flag: str | None, data: dict, cwd: str) -> tuple[str | None, str | None, list[str]]:
    """(path, source, []) or, with several ./adf/*.adf and nothing else,
    (None, None, candidates); (None, None, []) when there is none.  --adf is
    returned even when it does not exist (the checks then say so); a config
    path that no longer exists falls through to ./adf/."""
    if flag:
        return os.path.abspath(os.path.expanduser(flag)), "--adf", []
    c = get(data, "disk", "adf")
    if c and os.path.isfile(os.path.expanduser(c)):
        return os.path.abspath(os.path.expanduser(c)), "config", []
    cands = adf_candidates(cwd)
    if len(cands) == 1:
        return cands[0], "./adf/", []
    return None, None, cands


class Prompter:
    """The first-run questions.  Without a terminal nothing is asked: the
    error names the flag that answers the question instead."""

    def __init__(self, stdin=None, stdout=None, tty: bool | None = None, yes: bool = False):
        self.i = stdin or sys.stdin
        self.o = stdout or sys.stdout
        self.tty = self.i.isatty() if tty is None else tty
        self.yes = yes

    def _ask(self, q: str, flag: str) -> str:
        if not self.tty:
            raise ConfigError(f"{q.rstrip(': ')}? (no terminal to ask on: give {flag})")
        self.o.write(q)
        self.o.flush()
        line = self.i.readline()
        if not line:
            raise ConfigError(f"no answer (end of input): give {flag}")
        return line.strip()

    def ask_host(self) -> str:
        while True:
            a = self._ask("Address of your Ultimate (IP or hostname): ", "--host or AMIGA64_HOST")
            if a:
                return a

    def ask_adf(self, cwd: str, cands: list[str]) -> str:
        if cands:
            self.o.write("Workbench 3.1 ADFs in ./adf/:\n")
            for n, c in enumerate(cands, 1):
                self.o.write(f"  {n}. {os.path.basename(c)}\n")
            while True:
                a = self._ask(f"Which one [1-{len(cands)}]: ", "--adf PATH")
                if a.isdigit() and 1 <= int(a) <= len(cands):
                    return cands[int(a) - 1]
        while True:
            a = self._ask("Path to your Workbench 3.1 ADF (or a directory) [./adf/]: ",
                          "--adf PATH")
            p = os.path.abspath(os.path.join(cwd, os.path.expanduser(a or ADF_DIR)))
            if os.path.isdir(p):
                found = adf_candidates(os.path.dirname(p)) if os.path.basename(p) == ADF_DIR \
                    else sorted(x for x in glob.glob(os.path.join(p, "*.[aA][dD][fF]")))
                if len(found) == 1:
                    return found[0]
                if found:
                    return self.ask_adf(cwd, found)
                self.o.write(f"  no .adf file in {p}\n")
                continue
            return p

    def confirm(self, q: str) -> bool:
        if self.yes:
            return True
        a = self._ask(f"{q} [Y/n] ", "--yes").lower()
        return a in ("", "y", "yes")
