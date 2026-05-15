#!/usr/bin/env python3

import argparse
import hashlib
import json
import logging
import random
import shutil
import sqlite3
import struct
import time
from pathlib import Path

CONFIG = {
    "servers": [
        {
            "id":  "server1",
            "map": "chernarus",
            "db":  "<PATH TO SERVER 'players.db'>",
        },
        {
            "id":  "server2",
            "map": "namalsk",
            "db":  "PATH TO SERVER 'players.db'",
        },
        # Add more servers here…
    ],

    # Fresh spawn coordinates per map.
    "spawn_points": {
        "chernarus": [
            (8104.845215, 6.381680, 3147.029297),
            (11830.325195, 6.086939, 3451.525146),
            (13449.270508, 4.035523, 6486.302734),
            (12958.542969, 5.978739, 9815.685547)
        ],
        "livonia": [
            (4500.0, 4600.0, 2.0),
            (7200.0, 3100.0, 2.0),
            (5800.0, 8900.0, 2.0),
        ],
        "namalsk": [
            (4483.514160, 12.689652, 11084.105469),
            (9073.796875, 8.935995, 10154.644531),
            (6114.770020, 16.094164, 10441.400391)
        ],
        "_default": [
            (3000.0, 3000.0, 2.0),
        ],
    },

    # Where to store the sync state file (visit history + blob hashes)
    "state_dir": "/tmp/dayz_sync_state",
}

MIN_BLOB_SIZE = 16

# ──────────────────────────────────────────────────────────────────────────────
# BINARY HELPERS  –  BADC (Mid-Big-Endian) float codec
# ──────────────────────────────────────────────────────────────────────────────
def decode_badc_float(data: bytes, offset: int) -> float:
    """
    Decode a 4-byte BADC (Mid-Big-Endian) float from `data` at `offset`.

    The bytes on disk are stored as [B, A, D, C].
    Rearranged to standard big-endian [A, B, C, D] for struct.unpack.
    """
    b = data[offset: offset + 4]
    abcd = bytes([b[1], b[0], b[3], b[2]])
    return struct.unpack(">f", abcd)[0]


def encode_badc_float(value: float) -> bytes:
    """Encode a float as 4 bytes in BADC (Mid-Big-Endian) order."""
    abcd = struct.pack(">f", value)          # standard big-endian [A, B, C, D]
    return bytes([abcd[1], abcd[0], abcd[3], abcd[2]])   # → [B, A, D, C]


def read_position(blob: bytes) -> tuple[float, float, float] | None:
    """
    Decode (pos_x, pos_z, pos_y) from the blob as Python floats.
    Used only for logging/spawn assignment — never for round-tripping back
    into a blob (use copy_position_bytes for that).
    """
    if len(blob) < MIN_BLOB_SIZE:
        return None
    x = decode_badc_float(blob,  4)
    z = decode_badc_float(blob,  8)
    y = decode_badc_float(blob, 12)
    return x, z, y


def splice_loot(local_blob: bytes, src_blob: bytes) -> bytes:
    """
    Return a blob that keeps bytes 0-15 from local_blob completely intact
    (header, orientation, AND position) and replaces only bytes 16+ with
    the loot payload from src_blob.

    This is the correct approach for an existing player: we never touch
    the first 16 bytes written by the local game server, so there is zero
    risk of any position or heading drift on re-login.
    """
    if len(local_blob) < MIN_BLOB_SIZE:
        raise ValueError("local blob too short")
    return local_blob[:16] + src_blob[16:]


def patch_position(blob: bytes, x: float, z: float, y: float) -> bytes:
    """
    Encode (x, z, y) as BADC floats into blob bytes 4-15.
    Only used when setting brand-new coords (new-player spawn), never
    for preserving an existing position.
    """
    ba = bytearray(blob)
    ba[4:8]   = encode_badc_float(x)
    ba[8:12]  = encode_badc_float(z)
    ba[12:16] = encode_badc_float(y)
    return bytes(ba)


def loot_hash(blob: bytes) -> str:
    """SHA-256 of the loot payload (bytes 16+) used to detect changes."""
    return hashlib.sha256(blob[16:]).hexdigest() if len(blob) > 16 else ""


def pick_spawn(map_name: str) -> tuple[float, float, float]:
    pts = CONFIG["spawn_points"].get(map_name) or CONFIG["spawn_points"]["_default"]
    return random.choice(pts)


# ──────────────────────────────────────────────────────────────────────────────
# STATE FILE
# ──────────────────────────────────────────────────────────────────────────────

class SyncState:
    """
    Persists per-player metadata across runs:
        uid → {
            "last_hash":      "<sha256 of loot bytes at last sync>",
            "last_server":    "<server_id that held the newest data>",
            "visited":        ["server1", "server2", …]
        }
    """
    def __init__(self, state_dir: str):
        self._path = Path(state_dir) / "sync_state.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception as exc:
                print("State file corrupt, starting fresh: %s", exc)

    def save(self):
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self._path)

    def get(self, uid: str) -> dict:
        return self._data.get(uid, {})

    def set(self, uid: str, data: dict):
        self._data[uid] = data

    def visited(self, uid: str) -> set:
        return set(self.get(uid).get("visited", []))

    def record_visit(self, uid: str, server_id: str):
        entry = self._data.setdefault(uid, {})
        vs = set(entry.get("visited", []))
        vs.add(server_id)
        entry["visited"] = list(vs)

    def last_alive(self, uid: str) -> int | None:
        """Return the alive value from the last sync, or None if never synced."""
        v = self.get(uid).get("last_alive")
        return int(v) if v is not None else None


# ──────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def open_db(path: str) -> sqlite3.Connection:
    """
    Open a players.db in WAL mode.

    WAL (Write-Ahead Log) is the correct mode for concurrent access:
    the game server and this script can both have the DB open at the
    same time.  Readers never block writers and writers never block
    readers — only one writer runs at a time, which SQLite enforces
    automatically.  The -wal and -shm files that appear alongside the
    .db are normal and expected; SQLite folds them back into the main
    file on a checkpoint.

    We set isolation_level=None so we drive transactions manually with
    BEGIN IMMEDIATE / COMMIT / ROLLBACK.  BEGIN IMMEDIATE acquires the
    write lock at the start of the transaction rather than mid-way,
    which avoids the "cannot upgrade a read lock" error when the game
    server is also writing.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None          # manual transaction control
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")  # wait up to 5 s for a lock
    conn.execute("PRAGMA synchronous=NORMAL;") # safe with WAL, faster than FULL
    return conn


def try_open_db(path: str, server_id: str, readonly: bool = False) -> sqlite3.Connection | None:
    if not Path(path).exists():
        print("[%s] DB not found: %s", server_id, path)
        return None
    try:
        return open_db(path)
    except sqlite3.Error as exc:
        print("[%s] Cannot open DB: %s", server_id, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 1 – collect canonical snapshots from all server DBs
# ──────────────────────────────────────────────────────────────────────────────

def collect(state: SyncState, dry_run: bool) -> dict:
    """
    Read every server DB and return the best (most recently changed) snapshot
    for each player.

    snapshot = {
        "uid":     steam_id string,
        "alive":   int (0 or 1),
        "blob":    bytes (full Data blob),
        "hash":    sha256 of loot bytes,
        "source":  server_id string,
        "changed": bool (loot bytes differ from last sync),
    }
    """
    canon: dict[str, dict] = {}   # uid → best snapshot

    for srv in CONFIG["servers"]:
        sid   = srv["id"]
        conn = try_open_db(srv["db"], sid)
        if conn is None:
            continue

        try:
            rows = conn.execute(
                "SELECT Id, Alive, UID, Data FROM Players"
            ).fetchall()
        except sqlite3.Error as exc:
            print("[%s] Read error: %s", sid, exc)
            conn.close()
            continue
        finally:
            conn.close()

        count = 0
        for row in rows:
            uid   = str(row["UID"])
            alive = int(row["Alive"] or 0)
            blob  = bytes(row["Data"]) if row["Data"] else b""

            if len(blob) < MIN_BLOB_SIZE:
                print("[%s] UID %s has blob too short (%d B), skipping",
                            sid, uid, len(blob))
                continue

            h          = loot_hash(blob)
            prev_h     = state.get(uid).get("last_hash", "")
            prev_alive = state.last_alive(uid)
            # Changed if loot bytes differ OR alive state changed.
            # Death sets Alive=0 without touching loot, so we must
            # check both independently.
            changed = (h != prev_h) or (prev_alive is not None and prev_alive != alive)

            if not dry_run:
                state.record_visit(uid, sid)

            prev = canon.get(uid)

            # Prefer: changed-data entry > unchanged entry.
            # Tie-break when both changed: keep the one already set (first-wins
            # per cycle) — add tie-breaking logic here if needed.
            if prev is None or (changed and not prev["changed"]):
                canon[uid] = {
                    "uid":     uid,
                    "alive":   alive,
                    "blob":    blob,
                    "hash":    h,
                    "source":  sid,
                    "changed": changed,
                }
            count += 1

        print("[%s] Read %d player(s)", sid, count)

    return canon


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 2 – apply canonical snapshots to every other server DB
# ──────────────────────────────────────────────────────────────────────────────

def apply(canon: dict, state: SyncState, dry_run: bool) -> None:
    for srv in CONFIG["servers"]:
        sid      = srv["id"]
        map_name = srv["map"]
        applied  = skipped = 0

        conn = try_open_db(srv["db"], sid)
        if conn is None:
            continue

        try:
            # BEGIN IMMEDIATE acquires a write lock up front so we don't
            # race with the game server upgrading a read lock mid-transaction,
            # which is the main cause of "database is locked" errors.
            conn.execute("BEGIN IMMEDIATE;")

            for uid, snap in canon.items():

                alive    = snap["alive"]
                src_blob = snap["blob"]

                # ── Fetch existing row on this server ──────────────────────
                local = conn.execute(
                    "SELECT Id, Alive, Data FROM Players WHERE UID = ?",
                    (uid,)
                ).fetchone()

                is_new_player = local is None
                is_source     = snap["source"] == sid

                # For a living player on their source server there is nothing
                # to update — the game server is the authority there.
                # For a dead player we still need to reset their position on
                # every server including the one they died on, so we only
                # skip living players on the source.
                if is_source and alive == 1:
                    continue

                # Skip if nothing changed and the player already exists here.
                if not snap["changed"] and not is_new_player:
                    skipped += 1
                    continue

                # ── Compose the new blob ──────────────────────────────────
                if alive == 0:
                    # Dead player: clear the loot payload and assign a fresh
                    # spawn position on THIS server's map — regardless of
                    # which server they died on and regardless of whether
                    # they have an existing record here.  This ensures they
                    # never respawn at their death coordinates on any server.
                    pos      = pick_spawn(map_name)
                    new_blob = patch_position(src_blob, *pos)
                    new_blob = new_blob[:16]   # truncate loot — game respawns fresh
                    print(
                        "[%s] UID %s dead (died on %s) → Alive=0, "
                        "position reset to spawn (%.4f, %.4f, %.4f)",
                        sid, uid, snap["source"], *pos,
                    )
                elif not is_new_player and local["Data"] and len(bytes(local["Data"])) >= MIN_BLOB_SIZE:
                    # Alive, existing player: keep ALL of bytes 0-15 from the
                    # local DB untouched and only replace the loot payload.
                    new_blob = splice_loot(bytes(local["Data"]), src_blob)
                else:
                    # Alive, first visit to this server: patch a spawn point
                    # into the source blob so they land somewhere sensible.
                    pos      = pick_spawn(map_name)
                    new_blob = patch_position(src_blob, *pos)
                    print(
                        "[%s] New player UID %s (from %s) → spawn (%.4f, %.4f, %.4f)",
                        sid, uid, snap["source"], *pos,
                    )

                if dry_run:
                    action = "INSERT" if not local else "UPDATE"
                    pos_str = "(%.1f, %.1f, %.1f)" % pos
                    print("[DRY-RUN][%s] %s UID %s  alive=%d  pos=%s",
                             sid, action, uid, alive, pos_str)
                    applied += 1
                    continue

                if local:
                    conn.execute(
                        "UPDATE Players SET Alive=?, Data=? WHERE UID=?",
                        (alive, new_blob, uid)
                    )
                else:
                    conn.execute(
                        "INSERT INTO Players (Alive, UID, Data) VALUES (?, ?, ?)",
                        (alive, uid, new_blob)
                    )

                applied += 1

            if not dry_run:
                conn.execute("COMMIT;")

        except sqlite3.Error as exc:
            print("[%s] Write error: %s", sid, exc)
            if not dry_run:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
        finally:
            conn.close()

        print("[%s] Applied: %d  |  Skipped (no change): %d",
                 sid, applied, skipped)


# ──────────────────────────────────────────────────────────────────────────────
# BACKUP
# ──────────────────────────────────────────────────────────────────────────────
def backup_all() -> None:
    for srv in CONFIG["servers"]:
        src = Path(srv["db"])
        if src.exists():
            dst = src.with_suffix(".db.bak")
            shutil.copy2(src, dst)
            print("Backed up: %s", src.name)


# ──────────────────────────────────────────────────────────────────────────────
# SYNC CYCLE
# ──────────────────────────────────────────────────────────────────────────────
def run_sync(dry_run: bool = False) -> None:
    print("══ Sync cycle started%s ══", "  [DRY-RUN]" if dry_run else "")
    t0 = time.monotonic()

    state = SyncState(CONFIG["state_dir"])

    if not dry_run:
        backup_all()

    canon = collect(state, dry_run)
    print("Canonical snapshots: %d unique player(s)", len(canon))

    apply(canon, state, dry_run)

    # Update state hashes after a successful (non-dry) cycle
    if not dry_run:
        for uid, snap in canon.items():
            entry = state.get(uid)
            entry["last_hash"]   = snap["hash"]
            entry["last_server"] = snap["source"]
            entry["last_alive"]  = snap["alive"]
            state.set(uid, entry)
        state.save()

    print("══ Done (%.2f s) ══", time.monotonic() - t0)


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY: dump a player's position from a DB (handy for debugging)
# ──────────────────────────────────────────────────────────────────────────────
def dump_positions(db_path: str) -> None:
    """Print every player's decoded position — useful for verifying the codec."""
    conn = open_db(db_path)
    rows = conn.execute("SELECT UID, Alive, Data FROM Players").fetchall()
    conn.close()
    print(f"{'UID':<20} {'Alive':>5}  {'PosX':>12}  {'PosZ':>12}  {'PosY':>12}")
    print("-" * 68)
    for row in rows:
        blob = bytes(row["Data"]) if row["Data"] else b""
        pos  = read_position(blob)
        if pos:
            print(f"{str(row['UID']):<20} {row['Alive']:>5}  "
                  f"{pos[0]:>12.2f}  {pos[1]:>12.2f}  {pos[2]:>12.2f}")
        else:
            print(f"{str(row['UID']):<20} {row['Alive']:>5}  (blob too short)")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="DayZ cross-server loot sync (local .db files, no networking)")
    parser.add_argument("--watch", metavar="SEC", type=int, default=0,
                        help="Keep running, re-syncing every N seconds")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change, write nothing")
    parser.add_argument("--dump", metavar="DB_PATH",
                        help="Decode and print all player positions in a DB then exit")
    args = parser.parse_args()

    if args.dump:
        dump_positions(args.dump)
        return

    if args.watch > 0:
        print("Watch mode: every %d s. Ctrl-C to stop.", args.watch)
        while True:
            try:
                run_sync(dry_run=args.dry_run)
            except Exception as exc:
                print("Unexpected error: %s", exc)
            time.sleep(args.watch)
    else:
        run_sync(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
