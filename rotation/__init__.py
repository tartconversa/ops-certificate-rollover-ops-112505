"""长连接业务的证书轮换演练控制程序。

按客户群推进轮换：校验三类证据（文件同步 sync、入口生效 served、重连探针 probe），
核算各站点最低可重连容量与故障域覆盖，规划可安全下发的下一步动作，
并支持按客户群回溯每一次切换的里程碑差距。
"""
from .engine import Engine
from .audit import audit_group

__all__ = ["Engine", "audit_group"]
