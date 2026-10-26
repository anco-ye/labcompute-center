# LabCompute Center

科研任务的上传、资源准入、唯一租约派发、隔离执行、原始错误记录与结果交付平台。支持 Windows 节点和通过 SSH 接入的 Linux 节点；Python、科学 Python、NEURON、GPU 与 COMSOL 等环境按节点实际发布能力匹配。

本仓库包含平台源代码、前端、测试、部署工具与任务协议。配置、账号、令牌、证书、科研输入与结果、真实运维记录不属于公开源码包，部署时自行创建。默认生成的中心仅监听 `127.0.0.1`，没有已启用计算节点。

## 本地验证

使用 Python 3.11 或更新版本：

```sh
python -m venv .venv
# macOS/Linux
. .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p 'test_*.py'
npm ci
npx playwright install chromium
npm test
```

`npm test` 包括真实 Chromium 的刷新展开状态、页面与对话框滚动位置测试。Python 回归使用临时数据库、模拟节点或隔离的本地任务，不连接实际科研中心。`tests/live_smoke.py` 是另行显式调用的测试中心集成脚本，会提交两个小任务，不在自动回归中运行。

## 创建独立中心

先为自己的域名或测试主机准备 TLS 证书、私钥及客户端可信 CA。不要使用本仓库以外某个既有实验室的证书或账号。

```sh
python deploy/init_config.py --root /absolute/private/labcompute-instance --username admin --certificate /absolute/tls/server.crt --private-key /absolute/tls/server.key
python server.py --config /absolute/private/labcompute-instance/config/server.json
```

初始化会交互读取管理员密码、随机生成会话密钥，不覆盖非空目录。Windows 同样支持绝对路径，例如 `D:/LabComputePrivate`。局域网或公网访问请在私有配置中明确设置 bind、域名、可信反向代理和相应 TLS；保持安全 Cookie 与证书校验。

## Windows 节点安装包

准备与 `requirements.txt` 匹配的 Windows/Python wheel 离线目录、自己的 CA 和已验证的 Python 安装路径：

```sh
python deploy/enroll.py --id win2 --python C:/Python311/python.exe --controller https://compute.example.org:8443 --ca /private/ca.crt --wheels /private/windows-wheels --output /private/new-win2-bundle
```

包中 `node-registration.json` 仅含令牌摘要；通过中心管理员的私有配置/接入流程登记节点，验证后再启用。包中的 `agent.json` 和 `install.json` 含新生成的节点与本机账号凭据，整个包必须私下交付，禁止提交 Git。全新节点用管理员 PowerShell 执行包中 `install.ps1 -ConfigPath install.json`；安装器遇到已有服务账号会拒绝覆盖。该流程不用于更新忙碌节点。`deploy/build_bundle.py --output /private/app.zip` 可单独构建完整应用归档。

Linux 连接器依赖 `requirements-ssh.txt`，主机根目录、已验证 SSH 身份和运行环境须独立配置。Linux 执行需要对应 systemd、cgroup 和文件命名空间隔离能力；COMSOL 等商业运行环境与许可证由部署方自行配置，不随代码分发。

## 协作与维护

- 任务协议：[任务发布规范](docs/任务发布规范.md)，资源规则：[通用调度策略](docs/通用调度策略.md)。
- 编辑与反馈：[Agent 协作接口](static/AGENT_COLLABORATION.md)，公开配置示例见 [部署配置说明](docs/DEPLOYMENT.md)。
- 源码与部署管理账户具有高权限；`full_edit` 的全员协作模式须在自己的私有配置中显式开启。普通用户、匿名和禁用账户按认证规则控制。
- 调度计划只提交请求节点可领取的一项；分配、领取、准备、原生计算、停止回执和交付分别记账。科学计算不设置墙钟终止上限，保留内存、提交、磁盘、许可证、取消/暂停和有界网络等待。
- 科学已开始的失败只能使用验证过的检查点或缺项补齐入口继续；退出 0 与结果可下载不代表科学结论已经验收。
- 更新需要分别确认源码、驻留进程和按需 RPC 的真实版本。旧 Agent/桥可能继续运行旧代码，不能因文件已覆盖就重启忙碌服务。

本次公开包审查、验证范围与仍需部署方验证的边界见 [AUDIT.md](docs/AUDIT.md)。
