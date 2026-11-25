"""轮换演练引擎。

职责：
1. 回放配置与动作回执，推导任一演练时刻的状态（事件溯源，重启后续演）。
2. 校验三类证据：文件同步 sync、入口生效 served（核对签发链）、
   重连探针 probe（核对签发链与方向）。连通证据只对相同
   （入口代次, 客户端信任版本, 链）生效；迟到成功不给已替换代次放行。
3. 核算各客户群在各自站点的最低可重连容量与故障域覆盖
   （会话占用与重连容量共用入口限额；排空/离线/链到期不计新会话容量）。
4. 规划可安全下发的下一步动作，并在无合规重连路径时指出
   哪些连接能维持、哪个群下一次重连没保障。
"""
from __future__ import annotations

from collections import defaultdict

from .model import (DRAIN, OFFLINE, OPENING_TRUST_VERSION, PROBE, RESTORE,
                    SERVED, SYNC, TRUST, Chain, CohortCfg, EntranceCfg,
                    SessionSet)
from .store import CONFIG_CHAIN, CONFIG_COHORT, CONFIG_ENTRANCE, Store

INF = 10 ** 12


class GenState:
    """一个入口代次的证据与生命周期。"""
    __slots__ = ("chain", "synced_at", "served_at", "draining_from", "replaced_at")

    def __init__(self, chain, synced_at=None, served_at=None):
        self.chain = chain
        self.synced_at = synced_at      # sync 回执：发布准备
        self.served_at = served_at      # served 回执：允许接流
        self.draining_from = None       # drain 回执：排空提议提交时刻
        self.replaced_at = None         # 被更新代次替换的时刻


class _Dinic:
    """最大流。残量网络在多次 maxflow 调用间保留，可增容后续算。"""

    def __init__(self):
        self.adj = defaultdict(list)

    def add(self, u, v, cap):
        self.adj[u].append([v, cap, len(self.adj[v])])
        self.adj[v].append([u, 0, len(self.adj[u]) - 1])
        return self.adj[u][-1]

    def maxflow(self, s, t):
        total = 0
        while True:
            level = {s: 0}
            queue = [s]
            for u in queue:
                for e in self.adj[u]:
                    if e[1] > 0 and e[0] not in level:
                        level[e[0]] = level[u] + 1
                        queue.append(e[0])
            if t not in level:
                return total
            it = defaultdict(int)

            def dfs(u, f):
                if u == t:
                    return f
                edges = self.adj[u]
                while it[u] < len(edges):
                    e = edges[it[u]]
                    if e[1] > 0 and level.get(e[0], -1) == level[u] + 1:
                        d = dfs(e[0], min(f, e[1]))
                        if d > 0:
                            e[1] -= d
                            self.adj[e[0]][e[2]][1] += d
                            return d
                    it[u] += 1
                return 0

            while True:
                f = dfs(s, INF)
                if f <= 0:
                    break
                total += f


class Engine:
    """轮换演练控制程序。所有查询默认作用于当前演练时钟，也可回溯任意时刻。"""

    def __init__(self, store):
        self.store = store
        self._replay()

    @classmethod
    def open(cls, db_path):
        return cls(Store(db_path))

    def close(self):
        self.store.close()

    # ------------------------------------------------------------------
    # 录入：配置、回执、会话回报
    # ------------------------------------------------------------------
    def configure_chain(self, name, valid_from, expires, since=None):
        self.store.put_config(CONFIG_CHAIN, name, self._since(since), {
            "chain": name, "valid_from": int(valid_from), "expires": int(expires)})
        self._replay()

    def configure_entrance(self, name, site, zone, max_sessions, allow_groups,
                           opening_chain, opening_generation, since=None):
        self.store.put_config(CONFIG_ENTRANCE, name, self._since(since), {
            "entrance": name, "site": site, "fault_zone": zone,
            "max_sessions": int(max_sessions), "allow_groups": list(allow_groups),
            "opening_chain": opening_chain,
            "opening_generation": int(opening_generation)})
        self._replay()

    def configure_cohort(self, group, site, minimum_reconnect, min_zones,
                         session_cap, latched, since=None):
        self.store.put_config(CONFIG_COHORT, f"{group}|{site}", self._since(since), {
            "group": group, "site": site,
            "minimum_reconnect": int(minimum_reconnect), "min_zones": int(min_zones),
            "session_cap_min": int(session_cap), "latched_chains": list(latched)})
        self._replay()

    def _since(self, since):
        return self.clock if since is None else int(since)

    def ingest_receipts(self, rows):
        """持续接收动作回执；完全相同的行去重。返回统计。"""
        new = dup = 0
        for row in rows:
            if "at" not in row or "kind" not in row:
                raise ValueError(f"回执缺少 at/kind：{row!r}")
            if self.store.add_receipt(row):
                new += 1
            else:
                dup += 1
        self._replay()
        return {"new": new, "duplicates": dup}

    def ingest_sessions(self, rows):
        """录入/更新会话回报。end_notice_min 可随后补到。"""
        accepted, rejected = [], []
        for row in rows:
            ss = SessionSet(
                session_set=row["session_set"], group=row["group"],
                entrance=row["entrance"], chain=row["chain"],
                begin=int(row["begin_min"]),
                notice=(None if row.get("end_notice_min") is None
                        else int(row["end_notice_min"])),
                count=int(row["count"]))
            ent = self._latest(self.ent_hist.get(ss.entrance, []), ss.begin)
            if ent is None:
                rejected.append((row, "未知入口"))
                continue
            if self._cohort_cfg(ss.group, ent.site, ss.begin) is None:
                rejected.append((row, "该群在此站点无登记"))
                continue
            self.store.upsert_session(row)
            accepted.append(ss)
        self._replay()
        return {"accepted": len(accepted), "rejected": rejected}

    # ------------------------------------------------------------------
    # 回放
    # ------------------------------------------------------------------
    def _replay(self):
        self.chain_hist = defaultdict(list)
        self.ent_hist = defaultdict(list)
        self.cohort_hist = defaultdict(list)
        for kind, name, since, p in self.store.configs():
            if kind == CONFIG_CHAIN:
                self.chain_hist[name].append(
                    (since, Chain(p["chain"], p["valid_from"], p["expires"])))
            elif kind == CONFIG_ENTRANCE:
                self.ent_hist[name].append((since, EntranceCfg(
                    p["entrance"], p["site"], p["fault_zone"], p["max_sessions"],
                    tuple(p["allow_groups"]), p["opening_chain"],
                    p["opening_generation"])))
            elif kind == CONFIG_COHORT:
                self.cohort_hist[(p["group"], p["site"])].append((since, CohortCfg(
                    p["group"], p["site"], p["minimum_reconnect"], p["min_zones"],
                    p["session_cap_min"], tuple(p["latched_chains"]))))

        self.generations = {name: {} for name in self.ent_hist}
        self.served_log = defaultdict(list)
        self.offline_log = defaultdict(list)
        self.trust_log = defaultdict(list)
        self.probes = defaultdict(list)   # (ent, gen, chain, group, tv) -> [(at, ok)]
        self.rejected = []
        self.sessions = {}
        for row in self.store.sessions():
            ss = SessionSet(row["session_set"], row["group"], row["entrance"],
                            row["chain"], row["begin_min"],
                            row.get("end_notice_min"), row["count"])
            self.sessions[ss.session_set] = ss
        for name, hist in self.ent_hist.items():
            opening = hist[0][1]
            self.generations[name][opening.opening_generation] = GenState(
                opening.opening_chain, served_at=0)
            self.served_log[name] = [(0, opening.opening_generation)]
        for key, hist in self.cohort_hist.items():
            self.trust_log[key] = [(OPENING_TRUST_VERSION, 0, tuple(hist[0][1].latched))]

        self.clock = 0
        for at, _seq, kind, payload in self.store.receipts():
            self.clock = max(self.clock, at)
            self._apply(at, kind, payload)

    def _apply(self, at, kind, p):
        handler = {SYNC: self._apply_sync, SERVED: self._apply_served,
                   PROBE: self._apply_probe, TRUST: self._apply_trust,
                   DRAIN: self._apply_drain, OFFLINE: self._apply_offline,
                   RESTORE: self._apply_restore}.get(kind)
        if handler is not None:
            handler(at, p)

    def _reject(self, at, kind, p, reason):
        self.rejected.append({"at": at, "kind": kind, "payload": p, "reason": reason})

    def _apply_sync(self, at, p):
        ent, gen, chain = p.get("entrance"), p.get("generation"), p.get("value")
        if ent not in self.ent_hist:
            return self._reject(at, SYNC, p, "未知入口")
        gens, cur = self.generations[ent], self._current_gen_at(ent, at)
        if gen in gens:
            g = gens[gen]
            if g.chain != chain:
                return self._reject(at, SYNC, p, f"签发链与代次{gen}已登记的 {g.chain} 不符")
            if gen < cur:
                return self._reject(at, SYNC, p, "代次已被替换，同步回执迟到")
            g.synced_at = at if g.synced_at is None else min(g.synced_at, at)
        else:
            if gen < cur:
                return self._reject(at, SYNC, p, "代次已被替换，同步回执迟到")
            gens[gen] = GenState(chain, synced_at=at)

    def _apply_served(self, at, p):
        ent, gen, chain = p.get("entrance"), p.get("generation"), p.get("value")
        if ent not in self.ent_hist:
            return self._reject(at, SERVED, p, "未知入口")
        gens, cur = self.generations[ent], self._current_gen_at(ent, at)
        if gen < cur:
            return self._reject(at, SERVED, p, "迟到回执：代次已被替换")
        if gen in gens and gens[gen].chain != chain:
            return self._reject(at, SERVED, p,
                                f"签发链与代次{gen}已登记的 {gens[gen].chain} 不符")
        if gen == cur:
            g = gens[gen]
            if g.served_at is None:
                g.served_at = at
            return
        # 更新代次生效：替换当前代次，旧代次会话按回报或业务时限自然排空
        g = gens.setdefault(gen, GenState(chain))
        g.served_at = at
        gens[cur].replaced_at = at
        self.served_log[ent].append((at, gen))

    def _apply_probe(self, at, p):
        ent, gen, group = p.get("entrance"), p.get("generation"), p.get("group")
        chain, _, status = str(p.get("value", "")).partition(":")
        if status not in ("ok", "failed"):
            return self._reject(at, PROBE, p, "探针状态无法识别")
        if ent not in self.ent_hist:
            return self._reject(at, PROBE, p, "未知入口")
        ecfg = self._latest(self.ent_hist[ent], at)
        key = (group, ecfg.site)
        if key not in self.cohort_hist:
            return self._reject(at, PROBE, p, "方向不符：该群在此站点无登记")
        if group not in ecfg.allow_groups:
            return self._reject(at, PROBE, p, "方向不符：该群无此入口路由权限")
        gens, cur = self.generations[ent], self._current_gen_at(ent, at)
        if gen not in gens or gen > cur or gens[gen].served_at is None:
            return self._reject(at, PROBE, p, "代次尚未生效，探针不计")
        if gen < cur:
            return self._reject(at, PROBE, p, "代次已被替换，探针迟到不予放行")
        g = gens[gen]
        if g.chain != chain:
            return self._reject(at, PROBE, p, f"签发链不符：代次{gen}使用 {g.chain}")
        tv, trusted = self._trust_at(key, at)
        if chain not in trusted and status == "ok":
            return self._reject(at, PROBE, p, "探针成功但链不在当时信任集合，证据矛盾")
        self.probes[(ent, gen, chain, group, tv)].append((at, status == "ok"))

    def _apply_trust(self, at, p):
        group, site, version = p.get("group"), p.get("site"), p.get("generation")
        chains = p.get("value") or []
        key = (group, site)
        if key not in self.cohort_hist:
            return self._reject(at, TRUST, p, "未知客户群")
        cur_version, _ = self._trust_at(key, at)
        if version is None or version <= cur_version:
            return self._reject(at, TRUST, p, "信任版本回退或重复")
        self.trust_log[key].append((version, at, tuple(chains)))

    def _apply_drain(self, at, p):
        ent, gen = p.get("entrance"), p.get("generation")
        if ent not in self.ent_hist:
            return self._reject(at, DRAIN, p, "未知入口")
        gens, cur = self.generations[ent], self._current_gen_at(ent, at)
        if gen != cur or gens[gen].served_at is None:
            return self._reject(at, DRAIN, p, "只能排空当前生效代次")
        g = gens[gen]
        if g.draining_from is None:
            g.draining_from = at

    def _apply_offline(self, at, p):
        ent = p.get("entrance")
        if ent not in self.ent_hist:
            return self._reject(at, OFFLINE, p, "未知入口")
        self.offline_log[ent].append((at, OFFLINE))

    def _apply_restore(self, at, p):
        ent = p.get("entrance")
        if ent not in self.ent_hist:
            return self._reject(at, RESTORE, p, "未知入口")
        self.offline_log[ent].append((at, RESTORE))

    # ------------------------------------------------------------------
    # 时刻状态查询
    # ------------------------------------------------------------------
    @staticmethod
    def _latest(hist, t):
        out = None
        for since, cfg in hist:
            if since <= t:
                out = cfg
            else:
                break
        return out

    def chain_at(self, name, t):
        return self._latest(self.chain_hist.get(name, []), t)

    def entrance_at(self, name, t):
        return self._latest(self.ent_hist.get(name, []), t)

    def _cohort_cfg(self, group, site, t):
        return self._latest(self.cohort_hist.get((group, site), []), t)

    def cohorts_at(self, t):
        return [cfg for hist in self.cohort_hist.values()
                if (cfg := self._latest(hist, t)) is not None]

    def _current_gen_at(self, ent, t):
        gen = None
        for at, g in self.served_log.get(ent, ()):
            if at <= t:
                gen = g
            else:
                break
        return gen

    def _offline_at(self, ent, t):
        state = False
        for at, kind in self.offline_log.get(ent, ()):
            if at > t:
                break
            state = kind == OFFLINE
        return state

    def _trust_at(self, key, t):
        version, chains = None, ()
        for v, at, ch in self.trust_log.get(key, ()):
            if at <= t:
                version, chains = v, ch
            else:
                break
        return version, chains

    # ------------------------------------------------------------------
    # 会话占用与容量
    # ------------------------------------------------------------------
    def session_end(self, ss):
        """存续会话的扣除时刻：可信终止回报或业务会话时限，先到为准。"""
        ent = self._latest(self.ent_hist.get(ss.entrance, []), ss.begin)
        cohort = self._cohort_cfg(ss.group, ent.site, ss.begin) if ent else None
        limit = ss.begin + (cohort.session_cap if cohort else 0)
        return min(ss.notice, limit) if ss.notice is not None else limit

    def occupancy(self, ent, t):
        return sum(ss.count for ss in self.sessions.values()
                   if ss.entrance == ent and ss.begin <= t < self.session_end(ss))

    def free_at(self, ent, t):
        cfg = self.entrance_at(ent, t)
        return cfg.max_sessions - self.occupancy(ent, t)

    # ------------------------------------------------------------------
    # 连通证据与合规路径
    # ------------------------------------------------------------------
    def _evidence(self, ent, gen, chain, group, tv, t):
        """返回 (是否有效, 来源说明)。只认相同代次/信任版本/链的证据。"""
        latest = None
        for at, ok in self.probes.get((ent, gen, chain, group, tv), ()):
            if at <= t:
                latest = (ok, at)
            else:
                break
        if latest is not None:
            ok, at = latest
            return ok, f"探针@{at}" + ("" if ok else "（失败）")
        opening = self.ent_hist[ent][0][1]
        if gen == opening.opening_generation and tv == OPENING_TRUST_VERSION:
            return True, "开局观察"
        return False, "无证据"

    def path_valid(self, group, site, ent, t):
        """该群此刻能否经此入口合规重连。返回 (是否可行, 原因)。"""
        ecfg = self.entrance_at(ent, t)
        if ecfg is None or ecfg.site != site:
            return False, "入口不在站点"
        if group not in ecfg.allow_groups:
            return False, "无路由权限"
        gen = self._current_gen_at(ent, t)
        g = self.generations[ent][gen]
        if self._offline_at(ent, t):
            return False, "入口离线"
        if g.draining_from is not None and g.draining_from <= t:
            return False, "代次排空中，不接新会话"
        chain = self.chain_at(g.chain, t)
        if chain is None:
            return False, f"签发链 {g.chain} 未配置"
        if not chain.valid_at(t):
            return False, f"签发链 {g.chain} 未生效或已到期"
        tv, trusted = self._trust_at((group, site), t)
        if g.chain not in trusted:
            return False, f"链 {g.chain} 不在信任版本 v{tv}"
        ok, _ = self._evidence(ent, gen, g.chain, group, tv, t)
        if not ok:
            return False, f"无有效连通证据（代次{gen}/v{tv}/{g.chain}）"
        return True, "ok"

    def _path_detail(self, group, site, ent, t):
        gen = self._current_gen_at(ent, t)
        g = self.generations[ent][gen]
        tv, _ = self._trust_at((group, site), t)
        _, source = self._evidence(ent, gen, g.chain, group, tv, t)
        return {"entrance": ent, "generation": gen, "chain": g.chain,
                "free": self.free_at(ent, t), "evidence": source}

    # ------------------------------------------------------------------
    # 站点级容量与故障域覆盖核算（最大流）
    # ------------------------------------------------------------------
    def _network(self, cohorts, valid_pairs, ent_free, demands, only=None):
        d = _Dinic()
        src, sink = "S", "T"
        gedge = {}
        for c in cohorts:
            gedge[c.group] = d.add(src, ("g", c.group), demands.get(c.group, 0))
        for group, ent in valid_pairs:
            if only and group == only[0] and ent not in only[1]:
                continue
            d.add(("g", group), ("e", ent), INF)
        for ent, free in ent_free.items():
            d.add(("e", ent), sink, free)
        return d, gedge, src, sink

    def site_analysis(self, site, t, exclude=None, extra=None):
        """核算站点内各群的可重连容量与故障域覆盖。

        exclude=(入口, 代次)：模拟该代次排空（不再接新会话）后的情形。
        extra={群: 量}：模拟该群加速切换、额外占用的迁移余量。
        """
        cohorts = [c for c in self.cohorts_at(t) if c.site == site]
        ent_names = [n for n in self.ent_hist
                     if (ec := self.entrance_at(n, t)) is not None and ec.site == site]
        valid_pairs, ent_free, paths = [], {}, defaultdict(list)
        for c in cohorts:
            for n in ent_names:
                if exclude and exclude == (n, self._current_gen_at(n, t)):
                    continue
                ok, _ = self.path_valid(c.group, site, n, t)
                if ok:
                    valid_pairs.append((c.group, n))
                    ent_free[n] = self.free_at(n, t)
                    paths[c.group].append(self._path_detail(c.group, site, n, t))

        demands = {c.group: c.minimum_reconnect + (extra or {}).get(c.group, 0)
                   for c in cohorts}
        d, _, src, sink = self._network(cohorts, valid_pairs, ent_free, demands)
        joint_flow = d.maxflow(src, sink)
        joint_feasible = joint_flow == sum(demands.values())

        report = {"site": site, "at": t, "joint_feasible": joint_feasible,
                  "total_free": sum(ent_free.values()),
                  "total_minimum": sum(demands.values()), "cohorts": {}}
        for c in cohorts:
            others = {o.group: demands[o.group] for o in cohorts
                      if o.group != c.group}
            # 保证容量：先满足其他群最低量，剩余可分给本群的部分
            d1, ge1, src, sink = self._network(
                cohorts, valid_pairs, ent_free, {**others, c.group: 0})
            d1.maxflow(src, sink)
            ge1[c.group][1] = INF
            guaranteed = d1.maxflow(src, sink)
            standalone = sum(ent_free[n] for (g, n) in valid_pairs if g == c.group)
            # 故障域：只有实际能分到重连容量（其他群最低量先保留）才计入
            zone_cap = {}
            my_ents = defaultdict(set)
            for g, n in valid_pairs:
                if g == c.group:
                    my_ents[self.entrance_at(n, t).zone].add(n)
            for zone, ents in my_ents.items():
                dz, gez, src, sink = self._network(
                    cohorts, valid_pairs, ent_free,
                    {**others, c.group: 0}, only=(c.group, ents))
                dz.maxflow(src, sink)
                gez[c.group][1] = INF
                got = dz.maxflow(src, sink)
                if got > 0:
                    zone_cap[zone] = got
            need, covered = c.min_zones, len(zone_cap)
            problems = []
            if not paths[c.group]:
                problems.append("无合规重连路径")
            if guaranteed < c.minimum_reconnect:
                problems.append(f"保证重连容量 {guaranteed} < 最低要求 {c.minimum_reconnect}")
            if covered < need:
                problems.append(f"故障域覆盖 {covered} < 下限 {need}")
            report["cohorts"][c.group] = {
                "minimum": c.minimum_reconnect, "guaranteed": guaranteed,
                "standalone": standalone, "zones_covered": covered,
                "zones_needed": need, "zone_capacity": zone_cap,
                "paths": paths[c.group], "problems": problems,
                "state": "合规" if not problems else
                         ("无合规路径" if not paths[c.group] else "不足")}
        return report

    def _site_ok(self, site, t, exclude=None):
        rep = self.site_analysis(site, t, exclude=exclude)
        return all(not c["problems"] for c in rep["cohorts"].values())

    def event_times(self):
        times = {0, self.clock}
        for at, _, _, _ in self.store.receipts():
            times.add(at)
        for ss in self.sessions.values():
            times.add(ss.begin)
            times.add(self.session_end(ss))
        for hist in self.chain_hist.values():
            for _, ch in hist:
                times.add(ch.valid_from)
                times.add(ch.expires)
        return sorted(times)

    # ------------------------------------------------------------------
    # 排空安全性
    # ------------------------------------------------------------------
    def _gen_horizon(self, ent, gen, t):
        ends = [self.session_end(ss) for ss in self.sessions.values()
                if ss.entrance == ent and ss.chain == self.generations[ent][gen].chain
                and self.session_end(ss) > t]
        return max(ends) if ends else t

    def drain_windows(self, ent, gen, t0=None):
        """在 [t0, 代次会话结束] 上排空一直安全的窗口列表，及会话结束时刻。"""
        t0 = self.clock if t0 is None else t0
        site = self.entrance_at(ent, t0).site
        end = self._gen_horizon(ent, gen, t0)
        times = [x for x in self.event_times() if t0 <= x <= end]
        if t0 not in times:
            times.insert(0, t0)
        windows, start, prev = [], None, None
        for x in times:
            ok = self._site_ok(site, x, exclude=(ent, gen))
            if ok and start is None:
                start = x
            elif not ok and start is not None:
                windows.append((start, prev))
                start = None
            prev = x
        if start is not None:
            windows.append((start, end))
        return windows, end

    def drain_safe_from(self, ent, gen, t0=None):
        """从何时起排空该代次一直安全到其会话结束。返回 (安全起点或None, 受阻原因)。"""
        t0 = self.clock if t0 is None else t0
        site = self.entrance_at(ent, t0).site
        windows, end = self.drain_windows(ent, gen, t0)
        for start, wend in windows:
            if wend >= end:
                return start, []
        times = [x for x in self.event_times() if t0 <= x <= end]
        unsafe = [x for x in times if not self._site_ok(site, x, exclude=(ent, gen))]
        at = unsafe[-1] if unsafe else t0
        return None, self._blockers(site, at, exclude=(ent, gen))

    def _blockers(self, site, t, exclude=None):
        rep = self.site_analysis(site, t, exclude=exclude)
        return [{"group": g, "problems": c["problems"]}
                for g, c in rep["cohorts"].items() if c["problems"]]

    # ------------------------------------------------------------------
    # 值班员视图
    # ------------------------------------------------------------------
    def maintainable(self, group, site, t):
        """无合规路径时仍能维持的既有连接（到正常结束或业务上限）。"""
        out = []
        for ss in self.sessions.values():
            ent = self.entrance_at(ss.entrance, t)
            if ss.group != group or ent is None or ent.site != site:
                continue
            end = self.session_end(ss)
            if ss.begin <= t < end:
                chain = self.chain_at(ss.chain, t)
                out.append({"session_set": ss.session_set, "entrance": ss.entrance,
                            "chain": ss.chain, "count": ss.count, "ends_at": end,
                            "chain_expires": chain.expires if chain else None})
        return out

    def status(self, t=None):
        t = self.clock if t is None else t
        sites = sorted({c.site for c in self.cohorts_at(t)} |
                       {self.entrance_at(n, t).site for n in self.ent_hist
                        if self.entrance_at(n, t)})
        analyses = {s: self.site_analysis(s, t) for s in sites}
        unguaranteed = []
        for s, rep in analyses.items():
            for g, c in rep["cohorts"].items():
                if c["problems"]:
                    unguaranteed.append({
                        "group": g, "site": s, "state": c["state"],
                        "problems": c["problems"],
                        "maintainable": self.maintainable(g, s, t)})
        pending = []
        for name, gens in self.generations.items():
            cur = self._current_gen_at(name, t)
            for gen, g in sorted(gens.items()):
                if gen <= cur:
                    continue
                # 代次只在有回执（同步/生效）到达后才为控制程序所知
                synced = g.synced_at is not None and g.synced_at <= t
                served = g.served_at is not None and g.served_at <= t
                if synced or served:
                    pending.append({"entrance": name, "generation": gen,
                                    "chain": g.chain,
                                    "state": "已同步待生效" if synced else "未知"})
        return {"at": t, "sites": analyses, "unguaranteed": unguaranteed,
                "pending": pending,
                "rejected": [r for r in self.rejected if r["at"] <= t]}

    def plan(self, t=None):
        """可安全下发的下一步动作，以及受阻动作的原因。"""
        t = self.clock if t is None else t
        actions = []
        for site in sorted({c.site for c in self.cohorts_at(t)}):
            rep = self.site_analysis(site, t)
            for group, c in rep["cohorts"].items():
                key = (group, site)
                tv, trusted = self._trust_at(key, t)
                if c["problems"]:
                    actions.append({
                        "kind": "unguaranteed" if not c["paths"] else "degraded",
                        "group": group, "site": site, "problems": c["problems"],
                        "maintainable": self.maintainable(group, site, t)})
                for n in sorted(self.ent_hist):
                    ecfg = self.entrance_at(n, t)
                    if ecfg is None or ecfg.site != site or group not in ecfg.allow_groups:
                        continue
                    gen = self._current_gen_at(n, t)
                    g = self.generations[n][gen]
                    if self._offline_at(n, t) or (
                            g.draining_from is not None and g.draining_from <= t):
                        continue
                    chain = self.chain_at(g.chain, t)
                    if chain is None or not chain.valid_at(t):
                        continue
                    if g.chain not in trusted:
                        actions.append({
                            "kind": "trust-gap", "group": group, "site": site,
                            "entrance": n, "generation": gen, "chain": g.chain,
                            "why": f"链 {g.chain} 不在 {group} 信任版本 v{tv}"})
                        continue
                    ok, _ = self._evidence(n, gen, g.chain, group, tv, t)
                    if not ok:
                        actions.append({
                            "kind": "probe", "group": group, "site": site,
                            "entrance": n, "generation": gen, "chain": g.chain,
                            "why": f"代次{gen}已生效且链可信，"
                                   f"缺少信任版本 v{tv} 下的探针证据"})
            for n in sorted(self.ent_hist):
                ecfg = self.entrance_at(n, t)
                if ecfg is None or ecfg.site != site:
                    continue
                gen = self._current_gen_at(n, t)
                g = self.generations[n][gen]
                draining = g.draining_from is not None and g.draining_from <= t
                if draining or self._offline_at(n, t):
                    continue
                safe_from, blockers = self.drain_safe_from(n, gen, t)
                if safe_from is not None and safe_from <= t:
                    actions.append({"kind": "drain-ready", "entrance": n,
                                    "generation": gen, "site": site,
                                    "why": "排空后各群最低容量与覆盖仍满足"})
                else:
                    windows, _ = self.drain_windows(n, gen, t)
                    actions.append({"kind": "drain-blocked", "entrance": n,
                                    "generation": gen, "site": site,
                                    "safe_from": safe_from, "windows": windows,
                                    "blockers": blockers})
                chain = self.chain_at(g.chain, t)
                if chain and chain.expires > t:
                    users = [gr for gr, c in rep["cohorts"].items()
                             if any(p["entrance"] == n for p in c["paths"])]
                    if users:
                        actions.append({"kind": "chain-expiry", "entrance": n,
                                        "generation": gen, "chain": g.chain,
                                        "expires": chain.expires, "groups": users,
                                        "why": f"链 {g.chain} 于 {chain.expires} 到期，"
                                               f"到期后禁止新握手"})
        return actions

    def check_shift(self, group, site, extra, t=None):
        """假设某群加速切换、额外占用 extra 路重连容量，其余群最低量是否仍满足。"""
        t = self.clock if t is None else t
        rep = self.site_analysis(site, t, extra={group: int(extra)})
        return {"joint_feasible": rep["joint_feasible"],
                "total_free": rep["total_free"],
                "total_minimum": rep["total_minimum"],
                "cohorts": {g: {"guaranteed": c["guaranteed"],
                                "minimum": c["minimum"],
                                "zones_covered": c["zones_covered"],
                                "zones_needed": c["zones_needed"]}
                            for g, c in rep["cohorts"].items()}}

    def group_view(self, group, site, t=None):
        """单群视图：每条入口为什么算/不算，信任版本，可维持连接。"""
        t = self.clock if t is None else t
        tv, trusted = self._trust_at((group, site), t)
        rows = []
        for n in sorted(self.ent_hist):
            ecfg = self.entrance_at(n, t)
            if ecfg is None or ecfg.site != site:
                continue
            ok, reason = self.path_valid(group, site, n, t)
            row = {"entrance": n, "ok": ok, "reason": reason,
                   "generation": self._current_gen_at(n, t),
                   "free": self.free_at(n, t),
                   "offline": self._offline_at(n, t)}
            rows.append(row)
        rep = self.site_analysis(site, t)
        return {"group": group, "site": site, "at": t, "trust_version": tv,
                "trusted": sorted(trusted), "entrances": rows,
                "cohort": rep["cohorts"].get(group),
                "maintainable": self.maintainable(group, site, t)}
