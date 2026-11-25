"""按客户群回溯某一次切换。

每一次代次更替（或只排空未更替）给出四个里程碑：
发布准备（sync）→ 允许接流（served + 该群探针/信任齐备）→ 可开始排空
（从此刻起排空旧代次到其会话结束都安全）→ 实际结束（旧代次会话全部扣除）。
并标出各里程碑之间的时间差、当时可做的安全动作，以及
"实际替换早于安全点"这类执行偏差。
"""
from __future__ import annotations


def audit_group(engine, group, t=None):
    """回溯该群经历的每一次代次切换，按入口与旧代次组织。"""
    t = engine.clock if t is None else t
    out = []
    for ent in sorted(engine.ent_hist):
        ecfg = engine.ent_hist[ent][0][1]
        site = ecfg.site
        if (group, site) not in engine.cohort_hist:
            continue
        involved = group in ecfg.allow_groups or any(
            ss.group == group and ss.entrance == ent for ss in engine.sessions.values())
        if not involved:
            continue
        log = engine.served_log[ent]
        for i, (served_at, gen) in enumerate(log):
            g = engine.generations[ent][gen]
            successor = log[i + 1][1] if i + 1 < len(log) else None
            if g.replaced_at is None and g.draining_from is None:
                continue  # 仍在正常生效，未发生切换
            my_sessions = [ss for ss in engine.sessions.values()
                           if ss.entrance == ent and ss.chain == g.chain]
            group_sessions = [ss for ss in my_sessions if ss.group == group]
            ends = [engine.session_end(ss) for ss in my_sessions]
            actual_end = max(ends) if ends else (
                g.replaced_at if g.replaced_at is not None else g.draining_from)
            group_end = (max(engine.session_end(ss) for ss in group_sessions)
                         if group_sessions else None)

            publish_ready = allow_traffic = None
            if successor is not None:
                ng = engine.generations[ent][successor]
                publish_ready = ng.synced_at
                stop = ng.replaced_at if ng.replaced_at is not None else t
                for x in engine.event_times():
                    if ng.served_at <= x <= stop and engine.path_valid(
                            group, site, ent, x)[0]:
                        allow_traffic = x
                        break

            windows, _ = engine.drain_windows(ent, gen, 0)
            drain_allowed = None
            for start, wend in windows:
                if wend >= actual_end:
                    drain_allowed = start
                    break

            milestones = {"publish_ready": publish_ready,
                          "allow_traffic": allow_traffic,
                          "drain_allowed": drain_allowed,
                          "drained_at": g.draining_from,
                          "replaced_at": g.replaced_at,
                          "actual_end": actual_end}
            gaps = {}
            seq = ["publish_ready", "allow_traffic", "drain_allowed", "actual_end"]
            for a, b in zip(seq, seq[1:]):
                if milestones[a] is not None and milestones[b] is not None:
                    gaps[f"{a}->{b}"] = milestones[b] - milestones[a]

            flags = []
            if g.replaced_at is not None and allow_traffic is not None \
                    and g.replaced_at < allow_traffic:
                flags.append(f"代次于 {g.replaced_at} 被替换，"
                             f"早于该群允许接流点 {allow_traffic}")
            if g.replaced_at is not None and drain_allowed is not None \
                    and g.replaced_at < drain_allowed:
                flags.append(f"代次在 {g.replaced_at} 被替换，"
                             f"早于可安全排空点 {drain_allowed}")
            if drain_allowed is None:
                flags.append(f"到实际结束 {actual_end} 为止，"
                             "不存在一直安全到结束的排空起点")
            if g.draining_from is not None and drain_allowed is not None \
                    and g.draining_from < drain_allowed:
                flags.append(f"排空提交于 {g.draining_from}，"
                             f"早于可安全排空点 {drain_allowed}")

            safe_actions = {}
            for label in ("publish_ready", "allow_traffic", "drain_allowed"):
                x = milestones[label]
                if x is None:
                    continue
                acts = [a for a in engine.plan(x)
                        if a.get("group") == group or a.get("entrance") == ent]
                safe_actions[f"at_{label}"] = acts

            out.append({
                "group": group, "site": site, "entrance": ent,
                "old_generation": gen, "old_chain": g.chain,
                "new_generation": successor,
                "milestones": milestones, "gaps": gaps,
                "safe_windows": windows, "flags": flags,
                "group_sessions_end": group_end,
                "safe_actions": safe_actions})
    out.sort(key=lambda r: (r["milestones"]["replaced_at"]
                            or r["milestones"]["drained_at"] or 0,
                            r["entrance"]))
    return out
