# dayz-hive-script

Cross-server player loot sync hiving for DayZ. Reads and writes `players.db` SQLite files directly — no networking required, no extra dependencies beyond the Python standard library. This allows players to join a server collect loot, adventure, etc. and then switch to another hive server and maintain their loot, while keeping coords specific to a given server.

To see a working example you can checkout these servers which are hived together using this script:
```
Not so friendly in cherno... | VNLA+ | HIVED | AI | HELIS | discord.gg/C9Rm73V5HM
Not so friendly in vorkuta... | VNLA+ | HIVED | AI | HELIS | discord.gg/C9Rm73V5HM
```

---

## How it works

DayZ stores each player's state in a single binary blob inside `players.db`. The script reads that blob from every server, determines which copy is the most recently changed, and propagates the loot payload to all other servers. Position, death state, and first-time spawns are each handled separately.

### DB schema

```
Table: Players
  Id    INTEGER  primary key
  Alive INTEGER  1 = alive, 0 = dead
  UID   TEXT     Steam ID
  Data  BLOB     binary character blob
```

### Binary blob layout

```
Bytes  0– 1   02 00           fixed header / version
Bytes  2– 3   ?? ??           heading / orientation (unknown)
Bytes  4– 7   float BADC      Position X
Bytes  8–11   float BADC      Position Z
Bytes 12–15   float BADC      Position Y
Bytes 16+     opaque          character model, stats, inventory  ← loot payload
```

Positions are encoded as **BADC mid-big-endian floats** (byte order `[B, A, D, C]` on disk, decoded as big-endian `[A, B, C, D]`).

### Sync logic

| Situation | What happens |
|---|---|
| Player exists on both servers, loot changed | Loot payload (bytes 16+) updated; bytes 0–15 kept exactly as the local server wrote them |
| Player exists on both servers, nothing changed | Skipped — no write |
| Player exists on server A but has never joined server B | Inserted into server B's DB with loot from A and a random spawn position for B's map |
| Player died on any server | `Alive` set to 0, loot payload cleared, position reset to a random spawn point **on every server** including the one they died on |

### Change detection

`players.db` has no timestamp column. Changes are detected by storing a SHA-256 hash of bytes 16+ (the loot payload) and the last-seen `Alive` value in a small JSON state file after each sync cycle. A player is considered changed if either the loot hash or the alive state differs from the previous cycle.

### Position precision

When updating an existing player, bytes 0–15 are **never recalculated or re-encoded** — they are left completely untouched from what the local game server last wrote. Only bytes 16+ are replaced. This avoids any floating-point rounding drift that would otherwise shift the player slightly and cause falling or clipping on re-login.

---

## Requirements

- Python 3.10+
- No third-party packages — stdlib only (`sqlite3`, `struct`, `hashlib`, `json`, `argparse`)

---

## Setup

### 1. Clone or download

```bash
git clone https://github.com/yourname/dayz-loot-sync.git
cd dayz-loot-sync
```

### 2. Edit the config block

Open `dayz_loot_sync.py` and fill in the `CONFIG` section at the top:

```python
CONFIG = {
    "servers": [
        {
            "id":  "server1",           # short label for logs
            "map": "chernarus",         # must match a key in spawn_points
            "db":  "/path/to/server1/storage_1/players.db",
        },
        {
            "id":  "server2",
            "map": "livonia",
            "db":  "/path/to/server2/storage_1/players.db",
        },
    ],

    "spawn_points": {
        "chernarus": [
            (2669.0, 2488.0, 2.0),
            # add more...
        ],
        "livonia": [
            (4500.0, 4600.0, 2.0),
        ],
    },

    "state_dir": "/tmp/dayz_sync_state",   # where the hash state file lives
}
```

I recomend running this in a bash script with somthing like this
```
for i in 0 30; do
  python /path/hive_players.py >> /path/logs/hive_log.txt &
  sleep 30
done
```

And then run that bash script as a cron job with this
```
* * * * * /path/run_hive.sh
```

### 3. Verify your coordinates

Before running a full sync, confirm the position decoder is reading sensible values from your DB:

```bash
python dayz_loot_sync.py --dump /path/to/players.db
```

Output:

```
UID                  Alive         PosX         PosZ         PosY
────────────────────────────────────────────────────────────────────
76561198012345678        1     10366.12     5432.87         2.00
76561198087654321        0      3651.00     2375.00         2.00
```

If the coordinates look wildly off (negative millions, NaN, etc.) your blob offsets may differ — see [Adjusting blob offsets](#adjusting-blob-offsets) below.

---

## Usage

```bash
# Run once and exit
python dayz_loot_sync.py

# Run every 60 seconds (recommended for a live cluster)
python dayz_loot_sync.py --watch 60

# Preview what would change without writing anything
python dayz_loot_sync.py --dry-run

# Decode and print player positions from a single DB
python dayz_loot_sync.py --dump /path/to/players.db
```

---

## WAL mode and concurrent access

The script opens every DB with `PRAGMA journal_mode=WAL`. WAL (Write-Ahead Log) is SQLite's concurrent-access mode — the game server and the sync script can both have the DB open simultaneously without blocking each other. The `-wal` and `-shm` files that appear next to your `.db` are normal bookkeeping files that SQLite manages automatically.

Write transactions use `BEGIN IMMEDIATE` so the script waits for the game server to finish writing (up to 5 seconds) rather than failing immediately if there is lock contention.

---

## Backups

Before every sync cycle the script copies each `players.db` to `players.db.bak` in the same directory. These are simple single-file copies and are overwritten each cycle — they are a last-resort safety net, not a full backup solution. For production use, set up a proper periodic backup of the `.db` files separately.

---

## Adjusting blob offsets

If `--dump` shows garbage coordinates, the position bytes in your server's blob may be at different offsets. The offsets used (`4–7`, `8–11`, `12–15`) are based on community research and confirmed against vanilla DayZ servers. Modded servers or future game updates may shift them.

To investigate, you can dump the raw hex of a known player's blob and cross-reference with their in-game position:

```python
import sqlite3
conn = sqlite3.connect("players.db")
row = conn.execute("SELECT Data FROM Players WHERE UID = '76561198012345678'").fetchone()
print(bytes(row[0]).hex())
```

Once you identify the correct offsets, update `decode_badc_float` call sites in `read_position` and `patch_position` accordingly.

---

## State file

The sync state is stored at `state_dir/sync_state.json` (default `/tmp/dayz_sync_state/sync_state.json`). It records the last-synced loot hash, alive state, and server visit history per Steam ID. If you want to force a full re-sync of all players, delete this file and run the script — every player will be treated as changed.

---

