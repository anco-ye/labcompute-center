# 部署配置说明

所有真实配置放在仓库外或 `.local/`，不要提交账号、密码摘要、会话密钥、令牌、CA/SSH 私钥、DPAPI 密文、SQLite、工作目录和结果。

`deploy/init_config.py` 生成私有 `server.json` 与 `users.json`，默认 TLS、安全 Cookie、仅本机监听、无启用节点。首次运行 `server.py --config ...` 创建数据库。新节点的 token_hash 用于已注册节点的身份校验；原始 token 只交付给对应节点，CA 必须匹配中心域名和节点连接地址。

协作服务以另一个配置文件指定：

```json
{
  "source_root": "/absolute/path/to/application-source",
  "data_root": "/absolute/private/collaboration-data",
  "bind": "127.0.0.1",
  "port": 8447,
  "full_edit": false
}
```

配合 `collaboration_service.py --help` 查看入口参数。source_root 必须只包含平台应用源代码，不能包含私有配置、密钥或科研工作目录。开启 full_edit 会提升所有启用账户的权限，只有明确选择全员可信协作模式时才开启。源码编辑使用 CAS、语法检查、备份与事件记录；静态资源 live 与后端 published_pending_activation 是不同状态。

新建中心与维护既有中心是不同流程。不要直接对忙碌主机执行安装脚本、清空队列或替换租约。保留原运行任务的 PID/创建时间、节点、资源、输入、参数、租约与传输偏移。Web 独立切换必须使用对应门禁，并验证计算与桥身份保持；不能将其当作可以任意重启 Agent/Broker/桥的许可。

独立结果网关、节点下载服务和 Linux SSH 连接器分别使用各自私有配置，均须配置可信 CA、原节点身份和租约。客户端是否直连成功与结果是否存在分开判断，转发 fallback 保留原下载鉴权与 Range 协议。

本仓库不提供现成生产环境或许可证；Windows 计划任务、JobObject/独立账号、Linux cgroup/systemd/命名空间及 GPU/商业运行环境必须在你的部署节点上完成原生验收后启用。
