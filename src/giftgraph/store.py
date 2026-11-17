"""SQLite snapshots and transactional crawl checkpoints."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import CrawlConfig, CrawlStats, GiftPage, Peer


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                config TEXT NOT NULL, status TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
                node_limit_hit INTEGER NOT NULL DEFAULT 0, error TEXT,
                source TEXT NOT NULL DEFAULT 'telegram'
            );
            CREATE TABLE IF NOT EXISTS peers (
                key TEXT PRIMARY KEY, id TEXT NOT NULL, kind TEXT NOT NULL,
                name TEXT NOT NULL, username TEXT
            );
            CREATE TABLE IF NOT EXISTS run_nodes (
                run_id INTEGER NOT NULL REFERENCES runs(id), peer_key TEXT NOT NULL,
                depth INTEGER NOT NULL, state TEXT NOT NULL, offset TEXT NOT NULL DEFAULT '',
                gifts_seen INTEGER NOT NULL DEFAULT 0, error TEXT,
                PRIMARY KEY(run_id, peer_key)
            );
            CREATE TABLE IF NOT EXISTS gifts (
                key TEXT PRIMARY KEY, kind TEXT NOT NULL, catalog_id TEXT NOT NULL,
                title TEXT NOT NULL, slug TEXT, stars INTEGER, number INTEGER
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id INTEGER NOT NULL REFERENCES runs(id), key TEXT NOT NULL,
                gift_key TEXT NOT NULL, sender_key TEXT, recipient_key TEXT NOT NULL,
                date INTEGER NOT NULL, source_peer_key TEXT NOT NULL,
                kind TEXT NOT NULL, source TEXT NOT NULL, description TEXT,
                PRIMARY KEY(run_id, key)
            );
            CREATE TABLE IF NOT EXISTS ownerships (
                run_id INTEGER NOT NULL REFERENCES runs(id), gift_key TEXT NOT NULL,
                owner_key TEXT, source_peer_key TEXT NOT NULL, host_key TEXT,
                PRIMARY KEY(run_id, gift_key, source_peer_key)
            );
            CREATE TABLE IF NOT EXISTS pages (
                run_id INTEGER NOT NULL, peer_key TEXT NOT NULL, offset TEXT NOT NULL,
                observed_at TEXT NOT NULL, PRIMARY KEY(run_id, peer_key, offset)
            );
            CREATE INDEX IF NOT EXISTS queue_order ON run_nodes(run_id, state, depth);
            CREATE TABLE IF NOT EXISTS run_context (
                run_id INTEGER PRIMARY KEY REFERENCES runs(id), data TEXT NOT NULL
            );
        """)
        # Databases from earlier versions remain resumable without re-collecting
        # evidence. Unknown new fields deliberately stay NULL.
        with self.db:
            for table, additions in {
                "gifts": {"stars": "INTEGER", "number": "INTEGER"},
                "events": {"description": "TEXT"},
            }.items():
                existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column, column_type in additions.items():
                    if column not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _put_peer(self, peer: Peer) -> None:
        self.db.execute(
            """
            INSERT INTO peers VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name=CASE WHEN excluded.name='' THEN peers.name ELSE excluded.name END,
                username=COALESCE(excluded.username, peers.username)
        """,
            (peer.key, str(peer.id), peer.kind, peer.name, peer.username),
        )

    def create_run(
        self,
        seeds: list[Peer],
        config: CrawlConfig,
        source: str = "telegram",
        context: dict | None = None,
    ) -> int:
        seeds = list({p.key: p for p in seeds}.values())
        if not seeds:
            raise ValueError("No accessible seed users were found")
        if any(peer.kind != "user" for peer in seeds):
            raise ValueError("Graph seeds must be users")
        if len(seeds) > config.max_nodes:
            raise ValueError("Seed count exceeds --max-nodes; increase it or reduce --seed-limit")
        now = utc_now()
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO runs(started_at,updated_at,config,status,source) VALUES (?,?,?,?,?)",
                (now, now, json.dumps(asdict(config)), "paused", source),
            )
            run_id = cursor.lastrowid
            if context is not None:
                self.db.execute(
                    "INSERT INTO run_context VALUES (?,?)",
                    (run_id, json.dumps(context, ensure_ascii=False)),
                )
            for peer in seeds:
                self._put_peer(peer)
                self.db.execute(
                    "INSERT INTO run_nodes(run_id,peer_key,depth,state) VALUES (?,?,0,?)",
                    (run_id, peer.key, "boundary" if config.max_depth == 0 else "pending"),
                )
        return run_id

    def get_run(self, run_id: int | None = None) -> sqlite3.Row:
        if run_id is None:
            row = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Crawl run not found")
        return row

    def config(self, run_id: int) -> CrawlConfig:
        return CrawlConfig(**json.loads(self.get_run(run_id)["config"]))

    def set_status(self, run_id: int, status: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, utc_now(), run_id),
            )

    def next_node(self, run_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT n.*,p.id,p.kind,p.name,p.username FROM run_nodes n
            JOIN peers p ON p.key=n.peer_key
            WHERE n.run_id=? AND n.state='pending' ORDER BY n.depth,n.rowid LIMIT 1
        """,
            (run_id,),
        ).fetchone()

    def mark_unavailable(self, run_id: int, key: str, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE run_nodes SET state='unavailable',error=? WHERE run_id=? AND peer_key=?",
                (error, run_id, key),
            )

    def save_page(self, run_id: int, node: sqlite3.Row, page: GiftPage) -> None:
        """Commit evidence, discovered peers, and pagination cursor together."""
        config = self.config(run_id)
        key, offset = node["peer_key"], node["offset"]
        if page.item_count < 0:
            raise ValueError("Negative page item count")
        if page.next_offset and (
            page.next_offset == offset
            or self.db.execute(
                "SELECT 1 FROM pages WHERE run_id=? AND peer_key=? AND offset=?",
                (run_id, key, page.next_offset),
            ).fetchone()
        ):
            raise ValueError(f"Provider returned a pagination cycle for {key}")
        with self.db:
            # Replayed pages must not double-count observations or limits.
            if self.db.execute(
                "SELECT 1 FROM pages WHERE run_id=? AND peer_key=? AND offset=?",
                (run_id, key, offset),
            ).fetchone():
                return
            for peer in page.peers:
                self._put_peer(peer)
            for gift in page.gifts:
                self.db.execute(
                    """
                    INSERT INTO gifts(key,kind,catalog_id,title,slug,stars,number)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        kind=excluded.kind,catalog_id=excluded.catalog_id,title=excluded.title,
                        slug=excluded.slug,stars=excluded.stars,number=excluded.number
                    """,
                    (
                        gift.key,
                        gift.kind,
                        gift.catalog_id,
                        gift.title,
                        gift.slug,
                        gift.stars,
                        gift.number,
                    ),
                )
            node_count = self.db.execute(
                "SELECT COUNT(*) FROM run_nodes WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            for event in page.events:
                for peer_key in (event.sender_key, event.recipient_key):
                    if peer_key:
                        self._put_peer(Peer.from_key(peer_key))
                self.db.execute(
                    """INSERT OR IGNORE INTO events(
                        run_id,key,gift_key,sender_key,recipient_key,date,source_peer_key,
                        kind,source,description
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        event.key,
                        event.gift_key,
                        event.sender_key,
                        event.recipient_key,
                        event.date,
                        event.source_peer_key,
                        event.kind,
                        event.source,
                        event.description,
                    ),
                )
                # Current ownership must never imply a historical gift to this owner.
                if event.recipient_key != key or not event.sender_key:
                    continue
                if Peer.from_key(event.sender_key).kind != "user":
                    continue
                depth = node["depth"] + 1
                if depth > config.max_depth:
                    continue
                exists = self.db.execute(
                    "SELECT 1 FROM run_nodes WHERE run_id=? AND peer_key=?",
                    (run_id, event.sender_key),
                ).fetchone()
                if exists:
                    continue
                if node_count >= config.max_nodes:
                    self.db.execute("UPDATE runs SET node_limit_hit=1 WHERE id=?", (run_id,))
                    continue
                self.db.execute(
                    "INSERT INTO run_nodes(run_id,peer_key,depth,state) VALUES (?,?,?,?)",
                    (
                        run_id,
                        event.sender_key,
                        depth,
                        "boundary" if depth == config.max_depth else "pending",
                    ),
                )
                node_count += 1
            for ownership in page.ownerships:
                self.db.execute(
                    "INSERT OR REPLACE INTO ownerships VALUES (?,?,?,?,?)",
                    (
                        run_id,
                        ownership.gift_key,
                        ownership.owner_key,
                        ownership.source_peer_key,
                        ownership.host_key,
                    ),
                )
            seen = node["gifts_seen"] + page.item_count
            state = "done"
            if page.next_offset:
                state = "truncated" if seen >= config.max_gifts_per_profile else "pending"
            self.db.execute(
                """
                UPDATE run_nodes SET offset=?,state=?,gifts_seen=?
                WHERE run_id=? AND peer_key=?
            """,
                (page.next_offset or "", state, seen, run_id, key),
            )
            self.db.execute("INSERT INTO pages VALUES (?,?,?,?)", (run_id, key, offset, utc_now()))
            self.db.execute(
                "UPDATE runs SET pages=pages+1,updated_at=? WHERE id=?",
                (utc_now(), run_id),
            )

    def finish(self, run_id: int) -> None:
        partial = self.db.execute(
            """
            SELECT 1 FROM run_nodes WHERE run_id=? AND state IN ('truncated','unavailable') LIMIT 1
        """,
            (run_id,),
        ).fetchone()
        status = "partial" if partial or self.get_run(run_id)["node_limit_hit"] else "complete"
        self.set_status(run_id, status)

    def stats(self, run_id: int) -> CrawlStats:
        run = self.get_run(run_id)
        states = dict(
            self.db.execute(
                "SELECT state,COUNT(*) FROM run_nodes WHERE run_id=? GROUP BY state",
                (run_id,),
            ).fetchall()
        )
        events = self.db.execute(
            "SELECT COUNT(*) FROM events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        return CrawlStats(run_id, run["status"], sum(states.values()), run["pages"], events, states)

    def graph_data(self, run_id: int) -> dict:
        run = self.get_run(run_id)
        context = self.db.execute(
            "SELECT data FROM run_context WHERE run_id=?", (run_id,)
        ).fetchone()
        nodes = [
            dict(r)
            for r in self.db.execute(
                """
            SELECT p.key AS id,CASE WHEN p.kind='user' THEN p.id END AS user_id,
                COALESCE(NULLIF(p.name,''),p.username,p.key) AS label,
                p.kind,p.username,n.depth,n.state AS scan_state
            FROM run_nodes n JOIN peers p ON p.key=n.peer_key
            WHERE n.run_id=? ORDER BY n.depth,p.key
        """,
                (run_id,),
            )
        ]
        keys = {n["id"] for n in nodes}
        rows = self.db.execute(
            """
            SELECT e.key AS id,e.sender_key AS source,e.recipient_key AS target,
                substr(e.sender_key,instr(e.sender_key,':')+1) AS sender_id,
                substr(e.recipient_key,instr(e.recipient_key,':')+1) AS recipient_id,
                substr(e.gift_key,instr(e.gift_key,':')+1) AS gift_id,e.gift_key,
                g.title AS gift_title,g.slug AS gift_slug,g.number AS gift_number,g.stars,
                e.date,e.kind,
                e.source_peer_key
            FROM events e JOIN gifts g ON g.key=e.gift_key
            WHERE e.run_id=? ORDER BY e.date,e.key
        """,
            (run_id,),
        ).fetchall()
        edges = [dict(r) for r in rows if r["source"] in keys and r["target"] in keys]
        anonymous = [dict(r) for r in rows if r["source"] is None and r["target"] in keys]
        ownerships = [
            dict(r)
            for r in self.db.execute(
                "SELECT gift_key,owner_key,host_key,source_peer_key FROM ownerships WHERE run_id=?",
                (run_id,),
            )
        ]
        return {
            "metadata": {
                "run_id": run_id,
                "seed_context": json.loads(context[0]) if context else None,
                "status": run["status"],
                "source": run["source"],
                "started_at": run["started_at"],
                "updated_at": run["updated_at"],
                "config": json.loads(run["config"]),
                "node_limit_hit": bool(run["node_limit_hit"]),
                "pages": run["pages"],
                "observed_events": len(rows),
                "events_outside_graph": len(rows) - len(edges) - len(anonymous),
                "scope": "Visible incoming gifts; ownership is separate; not full gift history",
                "gift_scope": "regular_and_unique",
                "boundary_profiles_scanned": False,
                "price_semantics": (
                    "stars is the observed regular gift catalog price, not a proven historical "
                    "payment; unknown prices and unique gift prices are null"
                ),
                "date_semantics": "Unix timestamp in seconds, UTC",
            },
            "nodes": nodes,
            "edges": edges,
            "anonymous_events": anonymous,
            "ownerships": ownerships,
        }

    def gift_descriptions(self, run_id: int) -> dict[str, str]:
        """Return opt-in gift notes by event ID, keeping repeated gifts distinct."""
        self.get_run(run_id)
        return {
            row["key"]: row["description"]
            for row in self.db.execute(
                """SELECT key,description FROM events
                WHERE run_id=? AND description IS NOT NULL AND description<>'' ORDER BY key""",
                (run_id,),
            )
        }
