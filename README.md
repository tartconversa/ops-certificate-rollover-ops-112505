# 长连接业务的证书轮换与可用性接力

轮换演练中的签发链、入口及信任状态均为合成标识，无证书或私钥。会话占用和重新接流容量共享同一入口限额。所有时间是同一演练时钟的分钟。

## 在本地查看资料

运行环境使用 Python 3.11.2 和标准库。启动资料浏览器无需安装额外包。

```sh
python materials.py
python materials.py entrances
python -m unittest discover -s tests -v
```

资料浏览器也接受下方的其他资料名。JSONL 使用 UTF-8 编码，每行是一个对象。表内的顺序对应来源记录，事件时间与接收时间的区别以资料说明为准。各入口可以在无外网的环境中运行。

资料字段和业务口径见[演练资料格式](materials/README.md)。

## 轮换演练控制程序（rotation）

`rotation` 是按客户群推进本地轮换演练的运维程序。它接收动作回执，
核对三类证据（文件同步 sync、入口生效 served、重连探针 probe），
核算各群在各自站点的最低可重连容量与故障域覆盖，并告诉值班员
哪里还有可安全重连的路径、何时可以下发下一步操作。

```sh
python -m rotation --db drill.db init --materials materials
python -m rotation --db drill.db ingest --operations materials/operations.jsonl \
                                       --sessions materials/sessions.jsonl
python -m rotation --db drill.db status          # 各群重连保障状态
python -m rotation --db drill.db status --at 50  # 回溯任意时刻
python -m rotation --db drill.db plan            # 可安全下发的下一步动作
python -m rotation --db drill.db group legal --site north
python -m rotation --db drill.db audit --group legal
python -m rotation --db drill.db check-drain --entrance n2
python -m rotation --db drill.db what-if --group assist --site north --extra 20
python demo.py                                    # 完整演练回放
```

### 证据与状态口径

- 连通证据只对相同（入口代次, 客户端信任版本, 链）生效；开局信任覆盖
  视作开局观察，之后生效的代次需要该代次的探针。
- served 回执核对签发链；probe 回执核对签发链与方向（群→入口路由）。
  迟到成功不给已替换的代次放行；被拒回执在 `status` 中可见。
- 同一站点没有证据的入口代次保持未知（`status` 的"待证据"列表）。
- 排空/离线入口不计新会话容量；存续会话维持到可信终止回报或业务时限。
- 证书链到期后禁止新握手，即使某些群因此低于期望容量。

### 容量与覆盖口径

- 会话占用与重连容量共用入口限额；同一新入口承接多组时统一核算。
- 某群的保证容量 = 先满足其他群最低量后实际可分得的部分（最大流）。
- 故障域只有实际能分到重连容量才计入覆盖。
- `what-if` 评估某群加速切换是否挤占其他群需要的迁移余量。

### 持续运行与回溯

- 状态库存于 SQLite（`--db`），控制程序重启后续演；`receipt`/`ingest`
  可持续接收回执，完全相同的回执自动去重。
- `configure chain|entrance|cohort` 可录入不同的到期日、信任覆盖和
  业务群（`--since` 指定生效时刻，默认取当前演练时钟）。
- `audit --group X` 回溯每一次切换：发布准备、允许接流、可开始排空、
  实际结束之间的差距，各里程碑时可做的安全动作，以及
  "实际替换早于安全点"这类执行偏差。

