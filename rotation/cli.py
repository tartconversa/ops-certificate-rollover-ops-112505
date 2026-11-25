"""值班员命令行：录入配置与回执、查看状态、规划下一步、回溯切换。"""
from __future__ import annotations

import argparse
import json
import sys

from .audit import audit_group
from .engine import Engine


def _read_jsonl(path):
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                yield json.loads(line)


def _print(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load_materials(engine, directory):
    """从资料目录录入 entrances/cohorts/chains（since=0）。"""
    import pathlib
    root = pathlib.Path(directory)
    for row in _read_jsonl(root / "chains.jsonl"):
        engine.configure_chain(row["chain"], row["valid_from"], row["expires"], since=0)
    for row in _read_jsonl(root / "entrances.jsonl"):
        engine.configure_entrance(row["entrance"], row["site"], row["fault_zone"],
                                  row["max_sessions"], row["allow_groups"],
                                  row["opening_chain"], row["opening_generation"],
                                  since=0)
    for row in _read_jsonl(root / "cohorts.jsonl"):
        engine.configure_cohort(row["group"], row["site"], row["minimum_reconnect"],
                                row["min_zones"], row["session_cap_min"],
                                row["latched_chains"], since=0)


def cmd_init(args):
    engine = Engine.open(args.db)
    if args.materials:
        _load_materials(engine, args.materials)
    if args.config:
        with open(args.config, encoding="utf-8") as stream:
            conf = json.load(stream)
        for row in conf.get("chains", []):
            engine.configure_chain(row["chain"], row["valid_from"], row["expires"],
                                   since=row.get("since", 0))
        for row in conf.get("entrances", []):
            engine.configure_entrance(row["entrance"], row["site"], row["fault_zone"],
                                      row["max_sessions"], row["allow_groups"],
                                      row["opening_chain"], row["opening_generation"],
                                      since=row.get("since", 0))
        for row in conf.get("cohorts", []):
            engine.configure_cohort(row["group"], row["site"],
                                    row["minimum_reconnect"], row["min_zones"],
                                    row["session_cap_min"], row["latched_chains"],
                                    since=row.get("since", 0))
    print(f"已初始化 {args.db}：链 {len(engine.chain_hist)}，"
          f"入口 {len(engine.ent_hist)}，客户群 {len(engine.cohort_hist)}")
    engine.close()


def cmd_configure(args):
    engine = Engine.open(args.db)
    if args.what == "chain":
        engine.configure_chain(args.name, args.valid_from, args.expires,
                               since=args.since)
    elif args.what == "entrance":
        engine.configure_entrance(args.name, args.site, args.zone, args.max_sessions,
                                  args.allow_groups.split(","), args.opening_chain,
                                  args.opening_generation, since=args.since)
    else:
        engine.configure_cohort(args.name, args.site, args.minimum_reconnect,
                                args.min_zones, args.session_cap,
                                args.latched.split(","), since=args.since)
    print(f"已录入 {args.what} {args.name}（since={engine.clock if args.since is None else args.since}）")
    engine.close()


def cmd_ingest(args):
    engine = Engine.open(args.db)
    if args.operations:
        print("回执：", engine.ingest_receipts(_read_jsonl(args.operations)))
    if args.sessions:
        result = engine.ingest_sessions(_read_jsonl(args.sessions))
        print(f"会话：接受 {result['accepted']}，拒绝 {len(result['rejected'])}")
        for row, reason in result["rejected"]:
            print(f"  拒绝 {row.get('session_set')}：{reason}")
    print(f"演练时钟推进到 t={engine.clock}")
    engine.close()


def cmd_receipt(args):
    engine = Engine.open(args.db)
    rows = [json.loads(args.json)] if args.json else list(_read_jsonl(args.file))
    print(engine.ingest_receipts(rows))
    print(f"演练时钟 t={engine.clock}")
    engine.close()


def _fmt_paths(paths):
    return [f"{p['entrance']} 代次{p['generation']} 链{p['chain']} "
            f"空闲{p['free']}（{p['evidence']}）" for p in paths]


def cmd_status(args):
    engine = Engine.open(args.db)
    st = engine.status(args.at)
    if args.json:
        return _print(st, True)
    print(f"演练时钟 t={st['at']}")
    for site, rep in st["sites"].items():
        joint = "联合可行" if rep["joint_feasible"] else "联合不可行"
        print(f"站点 {site}（空闲 {rep['total_free']} / 最低需求 "
              f"{rep['total_minimum']}，{joint}）")
        for group, c in rep["cohorts"].items():
            print(f"  {group}：{c['state']}  保证 {c['guaranteed']}/"
                  f"最低 {c['minimum']}，故障域 {c['zones_covered']}/"
                  f"{c['zones_needed']}")
            for line in _fmt_paths(c["paths"]):
                print(f"    路径 {line}")
            for p in c["problems"]:
                print(f"    问题：{p}")
    for u in st["unguaranteed"]:
        print(f"!! {u['group']}@{u['site']} {u['state']}：{'；'.join(u['problems'])}")
        for m in u["maintainable"]:
            print(f"   可维持连接 {m['session_set']} {m['entrance']}/{m['chain']} "
                  f"{m['count']} 路，维持到 t={m['ends_at']}"
                  f"（链到期 {m['chain_expires']}）")
    for p in st["pending"]:
        print(f".. 待证据 {p['entrance']} 代次{p['generation']} 链{p['chain']}：{p['state']}")
    if st["rejected"]:
        print(f"被拒回执 {len(st['rejected'])} 条：")
        for r in st["rejected"]:
            print(f"  t={r['at']} {r['kind']} {r['payload']}：{r['reason']}")
    engine.close()


def cmd_plan(args):
    engine = Engine.open(args.db)
    t = engine.clock if args.at is None else args.at
    actions = engine.plan(t)
    if args.json:
        return _print(actions, True)
    print(f"t={t} 下一步动作：")
    for a in actions:
        kind = a["kind"]
        if kind == "probe":
            print(f"  [探针] {a['group']}→{a['entrance']} 代次{a['generation']} "
                  f"链{a['chain']}：{a['why']}")
        elif kind == "drain-ready":
            print(f"  [可排空] {a['entrance']} 代次{a['generation']}：{a['why']}")
        elif kind == "drain-blocked":
            when = "本次演练内不可行" if a["safe_from"] is None \
                else f"安全起点 t={a['safe_from']}"
            wins = "，".join(f"[{x},{y}]" for x, y in a.get("windows", []))
            print(f"  [排空受阻] {a['entrance']} 代次{a['generation']}：{when}"
                  + (f"；安全窗口 {wins}" if wins else ""))
            for b in a["blockers"]:
                print(f"      受阻 {b['group']}：{'；'.join(b['problems'])}")
        elif kind == "trust-gap":
            print(f"  [信任缺口] {a['group']}@{a['site']} 不信任 {a['entrance']} "
                  f"代次{a['generation']} 的链 {a['chain']}：{a['why']}")
        elif kind == "chain-expiry":
            print(f"  [链到期] {a['entrance']} 代次{a['generation']} 链{a['chain']} "
                  f"t={a['expires']} 到期，影响 {','.join(a['groups'])}")
        elif kind in ("unguaranteed", "degraded"):
            print(f"  [告警] {a['group']}@{a['site']}：{'；'.join(a['problems'])}")
            for m in a["maintainable"]:
                print(f"      可维持 {m['session_set']} {m['entrance']} "
                      f"{m['count']} 路至 t={m['ends_at']}")
    engine.close()


def cmd_group(args):
    engine = Engine.open(args.db)
    view = engine.group_view(args.group, args.site, args.at)
    if args.json:
        return _print(view, True)
    print(f"{view['group']}@{view['site']} t={view['at']} "
          f"信任版本 v{view['trust_version']} {view['trusted']}")
    c = view["cohort"]
    if c:
        print(f"  状态 {c['state']}，保证 {c['guaranteed']}/{c['minimum']}，"
              f"故障域 {c['zones_covered']}/{c['zones_needed']} {c['zone_capacity']}")
    for e in view["entrances"]:
        mark = "✓" if e["ok"] else "✗"
        print(f"  {mark} {e['entrance']} 代次{e['generation']} 空闲{e['free']}"
              f"{'（离线）' if e['offline'] else ''}：{e['reason']}")
    for m in view["maintainable"]:
        print(f"  可维持 {m['session_set']} {m['entrance']}/{m['chain']} "
              f"{m['count']} 路至 t={m['ends_at']}")
    engine.close()


def cmd_audit(args):
    engine = Engine.open(args.db)
    rows = audit_group(engine, args.group, args.at)
    if args.entrance:
        rows = [r for r in rows if r["entrance"] == args.entrance]
    if args.json:
        return _print(rows, True)
    for r in rows:
        m = r["milestones"]
        print(f"{r['group']}@{r['site']} 入口 {r['entrance']} "
              f"代次{r['old_generation']}({r['old_chain']})"
              + (f" → 代次{r['new_generation']}" if r["new_generation"] else "（仅排空）"))
        labels = [("publish_ready", "发布准备"), ("allow_traffic", "允许接流"),
                  ("drain_allowed", "可开始排空"), ("drained_at", "提交排空"),
                  ("replaced_at", "实际替换"), ("actual_end", "实际结束")]
        print("   " + "，".join(f"{name} t={m[key]}" if m[key] is not None
                                else f"{name} —" for key, name in labels))
        if r["gaps"]:
            print("   差距：" + "，".join(f"{k}={v}分钟" for k, v in r["gaps"].items()))
        if r["safe_windows"]:
            wins = "，".join(f"[{a},{b}]" for a, b in r["safe_windows"])
            print(f"   安全排空窗口：{wins}")
        for f in r["flags"]:
            print(f"   偏差：{f}")
        for key, acts in r["safe_actions"].items():
            if acts:
                print(f"   {key} 时可做：")
                for a in acts:
                    print(f"     - {a['kind']} "
                          + " ".join(str(a.get(k, "")) for k in
                                     ("group", "entrance", "generation", "chain")))
    engine.close()


def cmd_check_drain(args):
    engine = Engine.open(args.db)
    t = engine.clock if args.at is None else args.at
    gen = engine._current_gen_at(args.entrance, t)
    safe_from, blockers = engine.drain_safe_from(args.entrance, gen, t)
    result = {"entrance": args.entrance, "generation": gen, "at": t,
              "safe_from": safe_from, "blockers": blockers}
    if args.json:
        return _print(result, True)
    if safe_from is not None and safe_from <= t:
        print(f"{args.entrance} 代次{gen} 现在可安全排空")
    elif safe_from is None:
        print(f"{args.entrance} 代次{gen} 在其会话结束前不存在安全排空点")
    else:
        print(f"{args.entrance} 代次{gen} 到 t={safe_from} 才可安全排空")
    for b in blockers:
        print(f"  受阻 {b['group']}：{'；'.join(b['problems'])}")
    engine.close()


def cmd_what_if(args):
    engine = Engine.open(args.db)
    result = engine.check_shift(args.group, args.site, args.extra, args.at)
    if args.json:
        return _print(result, True)
    ok = "可行" if result["joint_feasible"] else "不可行"
    print(f"{args.group}@{args.site} 加速切换额外占用 {args.extra} 路：{ok}")
    for g, c in result["cohorts"].items():
        print(f"  {g}：保证 {c['guaranteed']}/最低 {c['minimum']}，"
              f"故障域 {c['zones_covered']}/{c['zones_needed']}")
    engine.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="rotation", description=__doc__)
    parser.add_argument("--db", default="rotation.db", help="状态库路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化演练（链/入口/客户群）")
    p.add_argument("--materials", help="资料目录（chains/entrances/cohorts.jsonl）")
    p.add_argument("--config", help="JSON 配置文件")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("configure", help="录入或调整单项配置")
    p.add_argument("what", choices=["chain", "entrance", "cohort"])
    p.add_argument("name")
    p.add_argument("--since", type=int, default=None)
    p.add_argument("--valid-from", type=int, default=0)
    p.add_argument("--expires", type=int, default=0)
    p.add_argument("--site", default="")
    p.add_argument("--zone", default="")
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--allow-groups", default="")
    p.add_argument("--opening-chain", default="")
    p.add_argument("--opening-generation", type=int, default=0)
    p.add_argument("--minimum-reconnect", type=int, default=0)
    p.add_argument("--min-zones", type=int, default=0)
    p.add_argument("--session-cap", type=int, default=0)
    p.add_argument("--latched", default="")
    p.set_defaults(fn=cmd_configure)

    p = sub.add_parser("ingest", help="接收动作回执与会话回报（可反复执行）")
    p.add_argument("--operations", help="operations.jsonl")
    p.add_argument("--sessions", help="sessions.jsonl")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("receipt", help="接收单条回执")
    p.add_argument("--json", help="单行 JSON")
    p.add_argument("--file", help="JSONL 文件")
    p.set_defaults(fn=cmd_receipt)

    p = sub.add_parser("status", help="各群重连保障状态")
    p.add_argument("--at", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plan", help="可安全下发的下一步动作")
    p.add_argument("--at", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("group", help="单群视图")
    p.add_argument("group")
    p.add_argument("--site", required=True)
    p.add_argument("--at", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_group)

    p = sub.add_parser("audit", help="按客户群回溯切换")
    p.add_argument("--group", required=True)
    p.add_argument("--entrance", default=None)
    p.add_argument("--at", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("check-drain", help="某入口当前代次能否安全排空")
    p.add_argument("--entrance", required=True)
    p.add_argument("--at", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check_drain)

    p = sub.add_parser("what-if", help="某群加速切换是否挤占他群迁移余量")
    p.add_argument("--group", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--extra", type=int, required=True)
    p.add_argument("--at", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_what_if)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
