"""File Room: a one-window Lethal Company save archive.

The archive is a single SQLite database. The same file runs on Windows, macOS,
and Linux. PyInstaller can pack it into one executable per system.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.padding import PKCS7

__version__ = "1.0.0"
GITHUB_REPO = "fake-game-developers/file-room"
PASSWORD = b"lcslime14a5"
DAY_SECONDS = 1080
PLANETS = (
    "Experimentation",
    "Assurance",
    "Vow",
    "Offense",
    "March",
    "Rend",
    "Dine",
    "Titan",
)


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent


def archive_folder() -> Path:
    documents = Path.home() / "Documents"
    if documents.is_dir():
        return documents / "LethalCompanySaveArchive"
    return Path.home() / ".local" / "share" / "FileRoom"


def default_save_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return home / "AppData" / "LocalLow" / "ZeekerssRBLX" / "Lethal Company"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "ZeekerssRBLX" / "Lethal Company"
    return home / ".config" / "unity3d" / "ZeekerssRBLX" / "Lethal Company"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def file_iso(path: Path) -> str | None:
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def decrypt_save(data: bytes) -> str:
    iv, encrypted = data[:16], data[16:]
    key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=iv, iterations=100).derive(PASSWORD)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8-sig")


def encrypt_save(plain: str, iv: bytes) -> bytes:
    key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=iv, iterations=100).derive(PASSWORD)
    padder = PKCS7(128).padder()
    padded = padder.update(plain.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + encryptor.update(padded) + encryptor.finalize()


def blank_stats() -> dict:
    return {
        "empty": True,
        "missing": True,
        "unreadable": False,
        "credits": None,
        "quota": None,
        "fulfilled": None,
        "quotasPassed": None,
        "daysSpent": None,
        "daysLeft": None,
        "deaths": None,
        "scrap": None,
        "planetId": None,
        "planetName": None,
        "version": None,
    }


def read_stats(data: bytes) -> dict:
    try:
        parsed = json.loads(decrypt_save(data))
    except Exception:
        stats = blank_stats()
        stats.update(empty=False, missing=False, unreadable=True)
        return stats

    def number(key: str) -> int | None:
        item = parsed.get(key)
        if not isinstance(item, dict):
            return None
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    credits = number("GroupCredits")
    planet_id = number("CurrentPlanetID")
    deadline = number("DeadlineTime")
    return {
        "empty": credits is None,
        "missing": False,
        "unreadable": False,
        "credits": credits,
        "quota": number("ProfitQuota"),
        "fulfilled": number("QuotaFulfilled"),
        "quotasPassed": number("QuotasPassed"),
        "daysSpent": number("Stats_DaysSpent"),
        "daysLeft": None if deadline is None else round(deadline / DAY_SECONDS),
        "deaths": number("Stats_Deaths"),
        "scrap": number("Stats_ValueCollected"),
        "planetId": planet_id,
        "planetName": PLANETS[planet_id] if planet_id is not None and 0 <= planet_id < len(PLANETS) else None,
        "version": number("FileGameVers"),
    }


def is_slot_file(name: str, slot: int) -> bool:
    return re.search(rf"SaveFile{slot}(?!\d)", name) is not None


def walk_slot_files(root: Path, slot: int) -> list[str]:
    if not root.exists():
        return []
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name in {"Player.log", "Player-prev.log"}:
                continue
            if is_slot_file(name, slot):
                found.append((Path(dirpath) / name).relative_to(root).as_posix())
    found.sort()
    return found


def tokenise(rel: str, slot: int) -> str:
    return re.sub(rf"SaveFile{slot}(?!\d)", "SaveFileSLOT", rel)


def detokenise(rel: str, slot: int) -> str:
    return rel.replace("SaveFileSLOT", f"SaveFile{slot}")


def safe_join(root: Path, rel: str) -> Path:
    dest = root
    for part in rel.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise RuntimeError("Refusing to touch a path outside the save folder.")
        dest = dest / part
    return dest


def clean_alias(value: str) -> str | None:
    text = " ".join(value.split())
    if len(text) > 48:
        raise RuntimeError("Alias must be 48 characters or fewer.")
    return text or None


def clean_notes(value: str) -> str:
    text = value.replace("\r\n", "\n").strip()
    if len(text) > 400:
        raise RuntimeError("Notes must be 400 characters or fewer.")
    return text


def game_running() -> bool:
    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            output = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Lethal Company.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                creationflags=flags,
            )
            return "lethal company.exe" in output.stdout.lower()
        return subprocess.run(["pgrep", "-f", "Lethal Company"], capture_output=True).returncode == 0
    except OSError:
        return False


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Archive:
    def __init__(self, database_path: Path, save_dir: Path):
        self.database_path = database_path
        self.folder = database_path.parent
        self.save_dir = save_dir
        self.folder.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(database_path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON").fetchone()
        self.conn.execute("PRAGMA journal_mode = DELETE").fetchone()
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                alias TEXT NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_slot INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blobs (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                rel_path TEXT NOT NULL,
                content BLOB NOT NULL,
                PRIMARY KEY (record_id, rel_path)
            );
            """
        )
        self.lock = threading.Lock()

    def state(self) -> dict:
        return {
            "saveDir": str(self.save_dir),
            "libraryDir": str(self.folder),
            "databasePath": str(self.database_path),
            "saveDirMissing": not self.save_dir.exists(),
            "version": __version__,
            "gameRunning": game_running(),
            "slots": [self.describe_slot(slot) for slot in (1, 2, 3)],
            "archives": [self.describe_record(row[0]) for row in self.conn.execute("SELECT id FROM records ORDER BY created_at DESC")],
        }

    def archive_slot(self, slot: int, alias: str, notes: str) -> dict:
        self._require_closed()
        self._require_slot(slot)
        if not self.save_dir.exists():
            raise RuntimeError("The Lethal Company save folder was not found.")
        files = walk_slot_files(self.save_dir, slot)
        if not any(Path(rel).name == f"LCSaveFile{slot}" for rel in files):
            raise RuntimeError(f"File {slot} has no save to archive.")
        record_id = str(uuid.uuid4())
        stored_alias = clean_alias(alias) or f"File {slot}"
        stored_notes = clean_notes(notes)
        with self.conn:
            self.conn.execute(
                "INSERT INTO records (id, alias, notes, created_at, source_slot) VALUES (?, ?, ?, ?, ?)",
                (record_id, stored_alias, stored_notes, now_iso(), slot),
            )
            for rel in files:
                payload = safe_join(self.save_dir, rel).read_bytes()
                self.conn.execute(
                    "INSERT INTO blobs (record_id, rel_path, content) VALUES (?, ?, ?)",
                    (record_id, tokenise(rel, slot), payload),
                )
        return self.describe_record(record_id)

    def update_record(self, record_id: str, alias: str, notes: str) -> dict:
        self._require_id(record_id)
        stored_alias = clean_alias(alias)
        if not stored_alias:
            raise RuntimeError("An alias is required.")
        with self.conn:
            changed = self.conn.execute(
                "UPDATE records SET alias = ?, notes = ? WHERE id = ?",
                (stored_alias, clean_notes(notes), record_id),
            ).rowcount
        if changed == 0:
            raise RuntimeError("That record is no longer in the archive.")
        return self.describe_record(record_id)

    def delete_record(self, record_id: str) -> dict:
        self._require_id(record_id)
        with self.conn:
            changed = self.conn.execute("DELETE FROM records WHERE id = ?", (record_id,)).rowcount
        if changed == 0:
            raise RuntimeError("That record is no longer in the archive.")
        return {"ok": True}

    def restore(self, record_id: str, slot: int) -> dict:
        self._require_closed()
        self._require_id(record_id)
        self._require_slot(slot)
        if not self.save_dir.exists():
            raise RuntimeError("The Lethal Company save folder was not found.")
        row = self.conn.execute("SELECT id FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise RuntimeError("That record is no longer in the archive.")
        blobs = self.conn.execute("SELECT rel_path, content FROM blobs WHERE record_id = ?", (record_id,)).fetchall()
        if not blobs:
            raise RuntimeError("The archived save is missing from the database.")
        for rel in walk_slot_files(self.save_dir, slot):
            safe_join(self.save_dir, rel).unlink(missing_ok=True)
        for rel, content in blobs:
            dest = safe_join(self.save_dir, detokenise(rel, slot))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return self.describe_slot(slot)

    def reveal_folder(self) -> dict:
        open_folder(self.folder)
        return {"libraryDir": str(self.folder)}

    def describe_record(self, record_id: str) -> dict:
        row = self.conn.execute(
            "SELECT id, alias, notes, created_at, source_slot FROM records WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("That record is no longer in the archive.")
        paths = [item[0] for item in self.conn.execute("SELECT rel_path FROM blobs WHERE record_id = ?", (record_id,))]
        main = next((rel for rel in paths if rel == "LCSaveFileSLOT" or rel.endswith("/LCSaveFileSLOT")), None)
        if main is None:
            stats = blank_stats()
        else:
            blob = self.conn.execute(
                "SELECT content FROM blobs WHERE record_id = ? AND rel_path = ?",
                (record_id, main),
            ).fetchone()
            stats = read_stats(blob[0]) if blob else blank_stats()
        return {
            "id": row[0],
            "alias": row[1],
            "notes": row[2],
            "createdAt": row[3],
            "sourceSlot": row[4],
            "stats": stats,
            "missing": main is None,
        }

    def describe_slot(self, slot: int) -> dict:
        files = walk_slot_files(self.save_dir, slot) if self.save_dir.exists() else []
        main = next((rel for rel in files if Path(rel).name == f"LCSaveFile{slot}"), None)
        if main is None:
            stats = blank_stats()
            modified = None
        else:
            path = safe_join(self.save_dir, main)
            stats = read_stats(path.read_bytes())
            modified = file_iso(path)
        total = 0
        for rel in files:
            try:
                total += safe_join(self.save_dir, rel).stat().st_size
            except OSError:
                pass
        occupied = main is not None and not stats["empty"] and not stats["missing"]
        return {
            "slot": slot,
            "files": files,
            "stats": stats,
            "bytes": total,
            "modifiedAt": modified,
            "occupied": occupied,
        }

    def dispatch(self, method: str, url: str, payload: dict | None) -> dict:
        payload = payload or {}
        try:
            with self.lock:
                data = self._route(method, url, payload)
            return {"ok": True, "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _route(self, method: str, url: str, payload: dict) -> dict:
        slot = int(payload.get("slot") or 0)
        if method == "GET" and url == "/api/state":
            return self.state()
        if method == "POST" and url == "/api/archive":
            return self.archive_slot(slot, str(payload.get("alias") or ""), str(payload.get("notes") or ""))
        if method == "POST" and url == "/api/restore":
            return self.restore(str(payload.get("id") or ""), slot)
        if method == "POST" and url == "/api/open-library":
            return self.reveal_folder()
        if method == "GET" and url == "/api/update":
            return check_for_update()
        if method == "POST" and url == "/api/update":
            return install_update()
        if method == "POST" and url == "/api/open-url":
            open_release_url(str(payload.get("url") or ""))
            return {"ok": True}
        prefix = "/api/archive/"
        if url.startswith(prefix):
            record_id = url[len(prefix) :]
            if method == "PATCH":
                return self.update_record(record_id, str(payload.get("alias") or ""), str(payload.get("notes") or ""))
            if method == "DELETE":
                return self.delete_record(record_id)
        raise RuntimeError("Not found.")

    def _require_slot(self, slot: int) -> None:
        if slot not in (1, 2, 3):
            raise RuntimeError("Pick file 1, 2, or 3.")

    def _require_id(self, record_id: str) -> None:
        try:
            parsed = uuid.UUID(record_id)
        except ValueError as exc:
            raise RuntimeError("Unknown record.") from exc
        if str(parsed) != record_id:
            raise RuntimeError("Unknown record.")

    def _require_closed(self) -> None:
        if game_running():
            raise RuntimeError("Lethal Company is open. Close it first, or it will write the old save back over this change.")


def page() -> str:
    root = app_root() / "public"
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8").replace("</script>", "<\\/script>")
    return html.replace(
        '<link rel="stylesheet" href="/styles.css" />',
        f"<style>\n{css}\n</style>",
    ).replace(
        '<script src="/app.js"></script>',
        f"<script>\n{js}\n</script>",
    )


def parse_version(value: str) -> tuple[int, ...]:
    numbers = []
    for piece in value.strip().lstrip("v").split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        numbers.append(int(digits or "0"))
    return tuple(numbers)


def platform_asset() -> str | None:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "FileRoom-windows-x64.exe"
    if sys.platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "FileRoom-macos-arm64"
        if machine in {"x86_64", "amd64"}:
            return "FileRoom-macos-x64"
        return None
    if machine in {"x86_64", "amd64"}:
        return "FileRoom-linux-x64"
    return None


def github_json(url: str) -> dict | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "FileRoom", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError("GitHub did not answer the update check.") from exc
    except OSError as exc:
        raise RuntimeError("Could not reach GitHub to check for an update.") from exc


def check_for_update() -> dict:
    current = __version__
    asset = platform_asset()
    release = github_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
    if release is None:
        return {
            "current": current,
            "latest": None,
            "updateAvailable": False,
            "canInstall": False,
            "releaseUrl": f"https://github.com/{GITHUB_REPO}/releases",
            "downloadUrl": None,
            "message": f"No release has been published yet. This copy is {current}.",
        }
    latest = str(release.get("tag_name") or "").lstrip("v")
    release_url = str(release.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases")
    newer = bool(latest) and parse_version(latest) > parse_version(current)
    download_url = None
    if asset:
        for item in release.get("assets") or []:
            if item.get("name") == asset:
                download_url = item.get("browser_download_url")
                break
    if not newer:
        message = f"{current} is the latest release."
    elif not download_url:
        message = f"{latest} is available, but there is no build for this system yet."
    elif not getattr(sys, "frozen", False):
        message = f"{latest} is available. This copy is running from source, so use the release page to install it."
    else:
        message = f"{latest} is available."
    return {
        "current": current,
        "latest": latest or None,
        "updateAvailable": newer and bool(download_url),
        "canInstall": newer and bool(download_url) and bool(getattr(sys, "frozen", False)),
        "releaseUrl": release_url,
        "downloadUrl": download_url,
        "message": message,
    }


def download_file(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "FileRoom", "Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def spawn_replacer(current: Path, incoming: Path) -> None:
    if sys.platform == "win32":
        script = current.with_name("file-room-update.cmd")
        script.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    ":loop",
                    "timeout /t 1 /nobreak >nul",
                    f'move /Y "{incoming}" "{current}"',
                    "if errorlevel 1 goto loop",
                    f'start "" "{current}"',
                    'del "%~f0"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=0x00000008 | 0x00000200 | 0x08000000,
            close_fds=True,
        )
        return
    script = current.with_name("file-room-update.sh")
    script.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.4; done",
                f'mv -f "{incoming}" "{current}"',
                f'chmod +x "{current}"',
                f'"{current}" >/dev/null 2>&1 &',
                'rm -f "$0"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    subprocess.Popen(["sh", str(script)], start_new_session=True, close_fds=True)


def install_update() -> dict:
    info = check_for_update()
    if not info["canInstall"] or not info["downloadUrl"]:
        raise RuntimeError(info["message"])
    current = Path(sys.executable)
    incoming = current.with_name(current.name + ".new")
    download_file(str(info["downloadUrl"]), incoming)
    spawn_replacer(current, incoming)
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"restarting": True}


def open_release_url(url: str) -> None:
    allowed = f"https://github.com/{GITHUB_REPO}/"
    if not url.startswith(allowed):
        raise RuntimeError("That link is not a File Room release.")
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    command = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    subprocess.Popen(command)


def alert(text: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, text, "File Room", 0x10)
    else:
        print(text, file=sys.stderr)


def main() -> None:
    try:
        import webview
    except ImportError:
        alert("The desktop window library is missing. Install it with: python -m pip install pywebview")
        return
    store = Archive(archive_folder() / "archive.db", default_save_dir())
    webview.create_window(
        f"File Room {__version__}",
        html=page(),
        js_api=store,
        width=1180,
        height=820,
        min_size=(860, 640),
        easy_drag=False,
        text_select=True,
        background_color="#12100e",
    )
    webview.start(private_mode=True)


def self_test() -> None:
    root = Path(tempfile.mkdtemp(prefix="file-room-"))
    save_dir = root / "saves"
    (save_dir / "DawnLib").mkdir(parents=True)
    iv = bytes([7] * 16)
    plain = json.dumps(
        {
            "GroupCredits": {"__type": "int", "value": 60},
            "ProfitQuota": {"__type": "int", "value": 130},
            "QuotaFulfilled": {"__type": "int", "value": 10},
            "DeadlineTime": {"__type": "int", "value": 2160},
            "CurrentPlanetID": {"__type": "int", "value": 7},
            "Stats_DaysSpent": {"__type": "int", "value": 4},
            "Stats_Deaths": {"__type": "int", "value": 1},
            "QuotasPassed": {"__type": "int", "value": 2},
            "Stats_ValueCollected": {"__type": "int", "value": 80},
            "FileGameVers": {"__type": "int", "value": 10031},
        },
        separators=(",", ":"),
    )
    save = encrypt_save(plain, iv)
    (save_dir / "LCSaveFile1").write_bytes(save)
    (save_dir / "DawnLib" / "ContractLCSaveFile1").write_bytes(b"contract")
    (save_dir / "LCGeneralSaveData").write_bytes(b"settings")
    (save_dir / "LCSaveFile2.moddata").write_bytes(b"stale")
    archive = Archive(root / "archive.db", save_dir)
    record = archive.archive_slot(1, "Titan drill", "keep this")
    assert record["stats"]["credits"] == 60, record["stats"]
    assert record["stats"]["planetName"] == "Titan"
    assert record["stats"]["daysLeft"] == 2
    archive.restore(record["id"], 2)
    assert (save_dir / "LCSaveFile2").read_bytes() == save
    assert (save_dir / "DawnLib" / "ContractLCSaveFile2").read_bytes() == b"contract"
    assert not (save_dir / "LCSaveFile2.moddata").exists()
    assert (save_dir / "LCGeneralSaveData").read_bytes() == b"settings"
    assert (save_dir / "DawnLib" / "ContractLCSaveFile1").exists()
    archive.update_record(record["id"], "Renamed", "note")
    assert archive.state()["archives"][0]["alias"] == "Renamed"
    archive.delete_record(record["id"])
    assert archive.state()["archives"] == []
    assert parse_version("v1.2.10") == (1, 2, 10)
    assert parse_version("1.0.1") > parse_version("1.0.0")
    assert not (parse_version("1.0.0") > parse_version("1.0.0"))
    real = default_save_dir() / "LCSaveFile2"
    if real.exists() and real.stat().st_size > 64:
        live = read_stats(real.read_bytes())
        assert live["unreadable"] is False, live
        assert isinstance(live["credits"], int), live
    print("ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
