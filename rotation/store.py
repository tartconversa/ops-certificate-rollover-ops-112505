"""SQLite 持久化。

配置（链/入口/客户群，按生效时刻 since 版本化）、动作回执、会话回报全部落库；
控制程序重启后重新回放即可延续存续会话与已生效入口。
"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS config(
  kind TEXT NOT NULL, name TEXT NOT NULL, since INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(kind, name, since));
CREATE TABLE IF NOT EXISTS receipts(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(at, kind, payload));
CREATE TABLE IF NOT EXISTS sessions(
  session_set TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""

CONFIG_CHAIN = "chain"
CONFIG_ENTRANCE = "entrance"
CONFIG_COHORT = "cohort"


def _canonical(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ---------- 配置（since 为生效时刻，同一键可多次录入） ----------
    def put_config(self, kind: str, name: str, since: int, payload: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO config(kind, name, since, payload) VALUES(?,?,?,?)",
            (kind, name, since, _canonical(payload)))
        self.db.commit()

    def configs(self):
        sql = "SELECT kind, name, since, payload FROM config ORDER BY kind, name, since"
        for kind, name, since, payload in self.db.execute(sql):
            yield kind, name, since, json.loads(payload)

    # ---------- 动作回执（按 (at, 到达序) 回放；完全相同的行去重） ----------
    def add_receipt(self, row: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO receipts(at, kind, payload) VALUES(?,?,?)",
            (int(row["at"]), row["kind"], _canonical(row)))
        self.db.commit()
        return cur.rowcount > 0

    def receipts(self):
        for at, seq, kind, payload in self.db.execute(
                "SELECT at, seq, kind, payload FROM receipts ORDER BY at, seq"):
            yield at, seq, kind, json.loads(payload)

    # ---------- 会话回报（end_notice 可随后补到） ----------
    def upsert_session(self, row: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO sessions(session_set, payload) VALUES(?,?)",
            (row["session_set"], _canonical(row)))
        self.db.commit()

    def sessions(self):
        for payload, in self.db.execute("SELECT payload FROM sessions"):
            yield json.loads(payload)
