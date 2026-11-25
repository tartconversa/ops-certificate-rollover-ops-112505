"""用 materials/ 的演练资料演示一次完整轮换演练的值班视角。

依次展示：开局状态、关键时刻的保障告警、可安全下发的动作、
故障与恢复、以及事后按客户群回溯切换的里程碑差距。
"""
import tempfile
from pathlib import Path

from rotation import Engine, audit_group

ROOT = Path(__file__).resolve().parent


def read_jsonl(name):
    import json
    with (ROOT / "materials" / f"{name}.jsonl").open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def build(db):
    engine = Engine.open(db)
    for row in read_jsonl("chains"):
        engine.configure_chain(row["chain"], row["valid_from"], row["expires"], since=0)
    for row in read_jsonl("entrances"):
        engine.configure_entrance(row["entrance"], row["site"], row["fault_zone"],
                                  row["max_sessions"], row["allow_groups"],
                                  row["opening_chain"], row["opening_generation"],
                                  since=0)
    for row in read_jsonl("cohorts"):
        engine.configure_cohort(row["group"], row["site"], row["minimum_reconnect"],
                                row["min_zones"], row["session_cap_min"],
                                row["latched_chains"], since=0)
    engine.ingest_sessions(read_jsonl("sessions"))
    engine.ingest_receipts(read_jsonl("operations"))
    return engine


def show(engine, title, t):
    print(f"\n{'=' * 68}\n【t={t}】{title}\n{'=' * 68}")
    st = engine.status(t)
    for site, rep in st["sites"].items():
        joint = "联合可行" if rep["joint_feasible"] else "联合不可行"
        print(f"站点 {site}（空闲 {rep['total_free']}/需求 {rep['total_minimum']}，{joint}）")
        for group, c in rep["cohorts"].items():
            mark = {"合规": "✓", "不足": "△", "无合规路径": "✗"}[c["state"]]
            print(f"  {mark} {group:<6} 保证 {c['guaranteed']:>3}/{c['minimum']:<3}"
                  f" 故障域 {c['zones_covered']}/{c['zones_needed']}")
    for u in st["unguaranteed"]:
        if u["state"] == "无合规路径":
            print(f"  !! {u['group']}@{u['site']} 下一次重连没保障；"
                  f"可维持：" +
                  ("，".join(f"{m['session_set']}({m['count']}路至t={m['ends_at']})"
                             for m in u["maintainable"]) or "无存续连接"))


def main():
    db = Path(tempfile.mkdtemp()) / "demo.db"
    engine = build(db)
    print("轮换演练回放：old 链 t=54 硬到期，n3 在 46–60 离线，"
          "legal 信任版本三度切换")

    show(engine, "开局：各群靠开局观察保障", 5)
    show(engine, "n1 已提交排空，n2 新代次已生效但尚无探针（容量空洞）", 27)
    show(engine, "n3 离线、n4 已切 renewed：media@north 无合规路径", 50)
    show(engine, "old 链到期：s1 退出，n3 仍离线", 55)
    show(engine, "n3 恢复、legal 已转 v3：n2 证据失效待补探针", 61)

    print(f"\n{'=' * 68}\n【t=61】当前可下发的安全动作（节选）\n{'=' * 68}")
    for a in engine.plan(61):
        if a["kind"] == "probe":
            print(f"  [探针] {a['group']}→{a['entrance']} 代次{a['generation']}：{a['why']}")
        elif a["kind"] == "drain-ready":
            print(f"  [可排空] {a['entrance']} 代次{a['generation']}")
        elif a["kind"] == "degraded":
            print(f"  [告警] {a['group']}@{a['site']}：{'；'.join(a['problems'])}")

    print(f"\n{'=' * 68}\n【回溯】legal@north 在 n4 的代次切换（10→12）\n{'=' * 68}")
    for r in audit_group(engine, "legal"):
        if r["entrance"] != "n4":
            continue
        m = r["milestones"]
        print(f"  发布准备 t={m['publish_ready']} → 允许接流 t={m['allow_traffic']}"
              f" → 可开始排空 {m['drain_allowed'] or '（无安全点）'}"
              f" → 实际结束 t={m['actual_end']}")
        print(f"  实际替换 t={m['replaced_at']}；安全窗口 {r['safe_windows']}")
        for f in r["flags"]:
            print(f"  偏差：{f}")
    engine.close()
    print(f"\n状态库保留在 {db}，可用 python -m rotation --db {db} status 继续查看")


if __name__ == "__main__":
    main()
