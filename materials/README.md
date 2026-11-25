# 演练资料格式

轮换演练中的签发链、入口及信任状态均为合成标识，无证书或私钥。会话占用和重新接流容量共享同一入口限额。所有时间是同一演练时钟的分钟。

## `entrances` 站点入口和当前证书

max_sessions 为共用同时会话数，fault_zone 表示可共同故障的范围。opening_chain 为初始接新链，opening_generation 是它在该入口的代次。allow_groups 为有路由权限的客户群。

字段为 `entrance`、`site`、`fault_zone`、`max_sessions`、`opening_chain`、`opening_generation`、`allow_groups`。

## `cohorts` 客户端群的信任与覆盖承诺

minimum_reconnect 为需能接新会话的余量，min_zones 为具备该能力的独立故障域下限。latched_chains 为初始信任链。session_cap_min 是正常会话最长维持时长。

字段为 `group`、`site`、`minimum_reconnect`、`min_zones`、`latched_chains`、`session_cap_min`。

## `chains` 合成签发链生效与到期时间

valid_from 含端点，expires 不含端点。新的握手只能用有效链。资料以这些标识表达证书约束，无实际 TLS 私钥或证书材料。

字段为 `chain`、`valid_from`、`expires`。

## `operations` 控制动作、入口和信任证据

形式上可按时间投递。sync 表示文件同步，served 表示入口新握手实际生效，probe 记录指定 group 使用指定链的重连结果，trust 为该组新的信任集合，drain 表示控制台已提交排空提议。 probe 的 generation 是入口代次，value 依次为链与探针状态。信任版本以该组在探针发生时已生效的一组为准。资料里在初始链上已有的同站点信任覆盖视作开局观察，之后的 served 需要该代次探针。offline 表示入口不接新会话，既有业务会话按 sessions 回报或业务时限判定。

字段为 `at`、`kind`、`entrance`、`group`、`site`、`generation`、`value`。

## `sessions` 存续会话占用与逐步排空回报

begin_min 为会话开始时间。end_notice_min 为退出回报到达时刻。count 为使用同一入口/链会话数。只有收到可信终止或达到业务会话时限才从接续量扣除。

字段为 `session_set`、`group`、`entrance`、`chain`、`begin_min`、`end_notice_min`、`count`。
