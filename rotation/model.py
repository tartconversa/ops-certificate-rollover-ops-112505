"""轮换演练的领域模型与回执种类。"""
from __future__ import annotations

from dataclasses import dataclass

# 回执种类（operations.kind）
SYNC = "sync"        # 设备报证书文件已同步
SERVED = "served"    # 入口已对新连接生效（需核对签发链）
PROBE = "probe"      # 客户群重连探针（需核对签发链与方向）
TRUST = "trust"      # 客户群新的信任集合（generation 字段为信任版本）
DRAIN = "drain"      # 控制台已提交排空提议
OFFLINE = "offline"  # 入口不接新会话；既有会话按回报或业务时限判定
RESTORE = "restore"  # 入口恢复接新会话

OPENING_TRUST_VERSION = 1  # 开局信任版本（latched_chains）


@dataclass(frozen=True)
class Chain:
    """合成签发链。valid_from 含端点，expires 不含端点。"""
    name: str
    valid_from: int
    expires: int

    def valid_at(self, t: int) -> bool:
        return self.valid_from <= t < self.expires


@dataclass(frozen=True)
class EntranceCfg:
    """站点入口。max_sessions 为会话占用与重连容量共用的限额。"""
    name: str
    site: str
    zone: str
    max_sessions: int
    allow_groups: tuple
    opening_chain: str
    opening_generation: int


@dataclass(frozen=True)
class CohortCfg:
    """客户群在某个站点的信任与覆盖承诺。"""
    group: str
    site: str
    minimum_reconnect: int
    min_zones: int
    session_cap: int
    latched: tuple


@dataclass(frozen=True)
class SessionSet:
    """一批共用同一入口/链的存续会话。"""
    session_set: str
    group: str
    entrance: str
    chain: str
    begin: int
    notice: int | None  # 退出回报到达时刻；None 表示尚未收到可信终止
    count: int
