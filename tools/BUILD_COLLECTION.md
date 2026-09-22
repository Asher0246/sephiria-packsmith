# 构筑分享与接收服务

首次打开 Web 工具询问授权；关闭弹窗视为拒绝。授权保存在本机
`SephiriaPacksmith/sharing.json`，不随浏览器端口变化。设置中可撤销。
旧 JSONL 记录不会补传；未授权时不写本机 JSONL 记录、不创建上传样本、不发上传请求。
游戏内整理也遵循本机授权，但首次授权需在 Web 工具完成。

上传到 `https://asher0627.site/packsmith/v1/builds`。输入、输出使用白名单；
实例编号替换成样本内编号，不包含原始游戏实例号、自由文本错误、名称和路径。
包含规则/求解器指纹、动态石板规则、目标分项、布局和耗时。
求解前布局来自此前读取游戏的快照，并非应用前实时读取。
应用记录只上传成败、交换/旋转次数和回滚标记。

上传使用受信任 TLS，禁止重定向。后台 SQLite 队列最多 50 条、每条最多
2 MB，满时淘汰最旧项。失败自动退避重试，最长间隔 5 分钟；每次网络操作
超时 5 秒。关闭后清空队列；已发送和当时正在发送的数据无法撤回。
上传确认使用 sampleId 去重。独立求解保留独立样本，构筑层面的规范化去重
留给训练预处理，避免丢失不同参数的求解实验。

## 部署与运维

服务：`packsmith-collector.service`，独立系统用户，仅监听 127.0.0.1:4081。
Caddy 为 /packsmith/* 转发，其余路径保持原接口服务。
健康检查：`https://asher0627.site/packsmith/health`。
SQLite：`/var/lib/packsmith-collector/builds.sqlite3`，不经 HTTP 暴露。
数据库不保存 IP；未配置 HTTP 访问日志，但操作系统/云平台可能有网络日志。

通过 SSH 查看服务：`sudo systemctl status packsmith-collector`。
暂停接收：`sudo systemctl stop packsmith-collector`，客户端保留队列并重试。
部署文件位于 `/opt/packsmith-collector`；Caddy 变更前会保存备份。
重新部署：上传 build_collector.py 和 deploy_collector.sh 至同一目录后，
运行 `sudo sh deploy_collector.sh`。服务端只需要 Python 和独立环境中的 gunicorn。

接收器限制单条大小、每进程每分钟 120 次请求，以及数据库 2 GB / 20 万条；
达到存储上限返回 503，不覆盖已收集样本。当前单 worker，四线程。
这是无需账号的公开投稿接口；大小/速率限制不是身份鉴别。样本可能由第三方
伪造，因此入库一律标为 validation=pending，不能直接作为可信训练标签。
训练前需用归档规则独立验证、重算分数，并筛除污染和重复样本。

## 规则归档和测试

每次发布前执行 `python -m tools.archive_training_rules`，将生成的 ZIP
保存到服务器 `/var/lib/packsmith-collector/rules/`。保留旧版本，不覆盖旧规则。
数据样本中的 catalogHash / solverHash 对应归档文件名。

`python -m tools.verify_build_upload` 会实际上传一个人工构造的 30 格样本，
并验证重复投递。打印 syntheticSampleId，管理员应将该样本标成 synthetic_test，
从训练数据中排除。普通单元测试不会联网。

导出数据应通过 SSH 使用 SQLite 的 backup API 创建一致性快照，再下载快照；
不要在写入过程中直接复制数据库文件。尚未提供公开浏览、账号或管理后台。
