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
