# Shell 沙盒(Sandbox)

Code Wood 可以为 AI 执行的 `shell` 命令提供一个基于操作系统的隔离层,
限制命令对文件系统与网络的访问范围。设计参考了 OpenAI 的
[Building a Codex sandbox for Windows](https://openai.com/index/building-codex-windows-sandbox/)。

## 级别

| 级别 | 文件写 | 文件读 | 网络 |
| --- | --- | --- | --- |
| `read_only` | 禁止(包括工作区) | 取决于沙盒用户被授予的读取权限 | 禁止 |
| `workspace_write` | 仅当前工作区;`.git`/`.codewood` 等受保护目录除外 | 取决于沙盒用户被授予的读取权限 | 可开关 |
| `full_access` | 与当前用户一致 | 与当前用户一致 | 允许 |

默认级别为 `full_access`,即保持现有行为不变。

> **能读 ≠ 能写**。读取能力取决于沙盒用户身份被授予的权限;工作区外
> 即使可读也不可写。沙盒不是防数据泄漏系统,也不等同于虚拟机。

## Windows 实现

与 Codex 相同,Windows 沙盒不是单个"沙盒程序",而是多个 Windows 原生
安全机制的组合。命令执行采用 **runner 进程** 架构(参考 Codex 的
`window-sandbox-rs`):主进程用 `CreateProcessWithLogonW` 以沙盒用户身份
启动一个 **runner**,runner 再为真实命令派生 **受限令牌**(restricted
token)并用 `CreateProcessAsUserW` 启动之:

```text
主进程 ──CreateProcessWithLogonW──▶ runner(沙盒用户)
                                      │ CreateRestrictedToken
                                      │   restricted SIDs = [capability SID,
                                      │     用户SID, logon SID, Everyone]
                                      ▼ CreateProcessAsUserW
                                   真实命令(受限令牌)
```

runner 本身用**普通登录令牌**运行(故沙盒用户组对其可读即可);真实命令
才持有受限令牌,其文件访问检查只在受限 SID 集合上精确匹配。

1. **专用本地用户与组** `CodewoodSandOffline` / `CodewoodSandOnline`
   提供文件系统身份(Windows 本地账户名最长 20 字符,故省略 "Sandbox"
   中缀)。Offline 用户命中出站 BLOCK 防火墙规则,Online 用户不受限,
   因此网络开关不需要在运行期切换规则。两个账户均属于
   `CodewoodSandUsers` 本地组。
2. **Capability SID 白名单**(Codex 方案):预置时在
   `%LOCALAPPDATA%/<应用名>/sandbox/sandbox_cap_sid.json` 持久化两个随机
   `S-1-5-21-*` 能力 SID(`workspace` / `readonly`)。受限令牌把对应
   capability SID 加入 restricted SIDs,**写权限只通过该 SID 授予**——
   因此即使目录对 `Authenticated Users` 等组开放 `Modify`,受限命令
   依然无法写入工作区之外。工作区只把 `workspace` capability SID
   授 `Modify`(受保护子目录显式 `DENY`),`read_only` 级别对两个
   capability SID 都授写 `DENY`。
3. **工作区 ACL**:切换/打开/启动工作区时自动应用(见下);旧版本遗留的
   按用户授的 ACE 会被清除,避免失效权限堆积。应用过 ACL 的工作区根目录
   会记入共享状态目录的 `sandbox_acl_dirs.json`;重设沙盒时按清单逐个
   清理所有历史目录的 ACL,删除工作区时同步从清单移除。
4. **Windows Firewall**:对 Offline 用户建出站 BLOCK 规则,阻止 HTTP/TCP/
   UDP 等所有出站连接(应用层代理/环境变量方案被显式避免,因为它们可以
   被不遵守约定的程序绕过)。
5. **Job Object(`KILL_ON_JOB_CLOSE`)**:runner 把真实命令挂到 job 上;
   沙盒命令被中断或超时后,整棵进程树一起结束。
6. **DPAPI**:沙盒用户密码用 `CryptProtectData` 加密后存于
   `%LOCALAPPDATA%/<应用名>/sandbox/sandbox_secret.bin`。

> **共享状态目录**:沙盒的全部状态(密钥、预置标记、capability SID、
> 运行时目录、ACL 记录)都放在 `%LOCALAPPDATA%/<应用名>/sandbox/`,
> 与各 `config` 目录无关,同一台机器上的多个 Code Wood 实例共享同一套
> 沙盒用户与密码,重建用户不再互相影响。升级后首次运行预置会重新创建
> 用户并采用共享密钥(旧配置目录里的 sandbox 残留文件可手动删除)。

### runner 的启动方式

- **源码运行**:spawn 用当前解释器(`sys.executable`,可用
  `CODOWN_SANDBOX_RUNNER_PYTHON` 覆盖)以 `-c` 方式加载
  `cli/core/sandbox/windows_runner.py`。预置时会为沙盒用户授予
  解释器目录的读取权限(见下)。
- **打包版本(PyInstaller)**:产物 `codewood\shell-runner.exe` 与主程序
  共享同一套 `_internal\` 运行时,由 `build/codewood.spec` 一并构建;
  spawn 直接以沙盒用户启动该 exe,不依赖任何 Python 解释器。

### 进程环境

沙盒命令的 `HOME` / `USERPROFILE` / `TEMP` / `APPDATA` 被重定向到
`%LOCALAPPDATA%/<应用名>/sandbox/home`、`sandbox/tmp`,由预置步骤创建
并授予两个沙盒用户写权限——沙盒工具(缓存、临时文件)不会尝试写真实
用户配置文件。

## 预置(一次性,需管理员)

创建用户与防火墙规则需要管理员权限。两种方式:

```text
codewood sandbox setup
```

在管理员终端运行;或打开 GUI **Settings → Security → Sandbox settings**,
点击 **Set up sandbox**(会弹出 UAC)。预置是幂等的,可重复运行。

```text
codewood sandbox status
```

查看当前预置状态。

设置页(Settings → Security)加载时会用 `LogonUserW` 校验两个沙盒用户
的密码是否与当前数据目录的密钥一致(60 秒缓存,避免重复失败登录触发
账户锁定);若用户存在但密码不一致,页面会提示并显示"设置沙盒"入口,
重跑预置即可修复。预置完成(标记文件被重写)后,页面会立即绕过缓存
重新校验一次,按钮与错误提示随即消失。

预置步骤:

1. 生成/读取 DPAPI 加密的随机密码。
2. 用 PowerShell `New-LocalUser` 创建 `CodewoodSandOffline` 与
   `CodewoodSandOnline`,并加入 `Users` 组;账户已存在时先
   `Remove-LocalUser` 删除再重建(跨数据目录的旧账户可能拒绝密码刷新,
   且密码必须与当前数据目录的密钥一致)。生成的密码固定包含大小写、
   数字与符号以满足密码复杂度策略;若被本地策略拒绝(复杂度/历史/
   最小长度),会自动换新密码重试并回写密钥文件。删除旧账户前,会先
   按账户名递归清理工作区目录树及沙盒运行时目录中关联旧用户的 ACL,
   并清除根目录上不可解析(失效)的本地 SID,避免重建用户后残留失效项。
3. 用 PowerShell `New-NetFirewallRule`(失败时回退 `netsh`)为 Offline
   用户建出站 BLOCK 规则。
4. 创建沙盒运行时目录(home/tmp/AppData)并授权。
5. 生成/读取 capability SID 并为运行时目录授权。
6. 源码运行时,为沙盒用户授予 Python 解释器目录读取权限(打包版跳过;
   失败不阻塞预置,属尽力而为)。
7. 按当前级别应用工作区 ACL(这部分不需要管理员,是用户自己的文件;
   所有 ACL 编辑合并为单个 PowerShell 进程,ACL 已正确时跳过写入)。

删除工作区(从注册表中移除)时,会同时清理该工作区目录树上的沙盒
用户/组/capability SID 的 ACL,收回沙盒对已遗忘目录的访问权限。

切换级别、切换/打开/启动工作区时,会自动刷新工作区 ACL(非提权路径,
通过 `cli.core.sandbox.refresh_workspace_acls`)。

## 未预置时的行为(Fail closed)

如果 `sandbox_level` 配置为 `read_only` / `workspace_write` 但沙盒尚未
预置,`shell` 命令会被拒绝并返回明确错误,而不会静默降级为无沙盒执行。
配置 `full_access` 不需要预置。

## 失败识别与提权(bypass_sandbox)

### 明确告知失败是否由沙箱导致

在沙盒级别下执行的每条 `shell` 命令,结果都会附带沙盒上下文,让模型
能判断运行环境:

- `sandbox_level` / `sandbox_network`:本次命令的隔离级别与网络开关。
- `sandbox_bypassed: true`:本次命令经用户批准以完全权限执行(仅一次)。

当沙盒内命令**失败**且看起来是被沙盒拦截时,结果额外携带:

- `sandbox_related: true`:失败非常可能是沙箱限制导致(如访问被拒、
  网络被禁、沙盒启动失败),而非命令本身错误。
- `sandbox_reason`:`access_denied` / `network_blocked` / `spawn_failure`
  / `not_provisioned` 之一。
- `sandbox_escalation_hint`:给模型的引导文本,提示先反思命令是否必要、
  是否有沙盒安全的替代方案,确有必要时如何申请提权。

判定依据(仅当沙盒计划生效时):

- 沙盒进程启动失败(`spawn_failure`)或未预置(`not_provisioned`)为确定信号;
- Windows 退出码 `5`(ERROR_ACCESS_DENIED)或输出含
  "Access is denied" / "permission denied" / EACCES 等 → `access_denied`;
- `sandbox_network=false` 且命令为网络类(git fetch/clone/pull、curl、
  pip/npm install 等)且输出含网络不可达/超时 → `network_blocked`。

### 模型发起的一次性提权(需用户批准)

`shell` 工具新增可选参数 `bypass_sandbox`(布尔)。模型在因沙箱失败并
反思后,确有必要时可以重新调用 `shell` 并设置 `bypass_sandbox: true`,
申请以完全权限执行该命令(仅本次,不改变沙盒配置)。执行前一定会弹出
确认,用户有三个选择:

| 用户选择 | 行为 |
| --- | --- |
| 是(批准) | 本次命令以完全权限执行一次;结果标记 `sandbox_bypassed: true` |
| 否(拒绝) | 结束当前任务:命令不执行,返回 `user_cancelled`,运行时按用户取消处理,模型不得重试或再次申请提权 |
| 拒绝并补充信息 | 任务继续:命令不执行,返回 `user_rejected_with_supplement` + `user_supplement`,模型按用户反馈调整步骤后继续 |

提权批准视为**本次命令唯一的人工审核关卡**:

- 批准后**不会再弹出**第二次的常规命令执行确认(execution policy),直接执行;
- 该工具调用**跳过 AI 自动审核**(`_freedom_auto_confirm`),因为用户已在
  提权确认中人工把关;
- 仅当沙盒实际激活(`read_only` / `workspace_write` 且平台支持)时上述
  跳过才生效;`full_access` 或平台不支持时 `bypass_sandbox` 是空操作,
  AI 审核与常规确认照旧。

> 提权是**显式用户批准**的例外通道:它只影响被批准的那一条命令,不会
> 改变 `config.jsonc` 中的 `sandbox_level`,也不会让后续命令绕过沙盒。

## 配置项(`config.jsonc`)

```jsonc
{
  "sandbox_level": "full_access",      // read_only | workspace_write | full_access
  "sandbox_network": true               // workspace_write 级别下是否允许网络
}
```

## 平台支持

`cli/core/sandbox/` 提供平台抽象(`SandboxBackend`)。Windows 是当前唯一
实现;macOS / Linux 返回 `supported=False`,设置页会显示"当前平台暂不支持"。
后续可在同一接口下实现 seatbelt / bubblewrap 后端。

## 边界与限制

- 沙盒只约束 AI 的 `shell` 工具;`apply_patch` / `read` / grep 等进程内
  文件工具仍由现有路径策略约束;GUI 嵌入式终端是用户直接操作,不沙盒。
- 读取范围取决于沙盒用户身份被授予的权限:用户个人目录(`C:\Users\...`)
  下的文件通常不可读(这正是期望的安全边界)。
- 受保护目录(`.git`、`.codewood`)在沙盒内不可写——例如 `git commit`
  等需要写 `.git` 的命令在沙盒级别下会失败,请切换到 `full_access`。
- `read_only` 无法阻止**删除**(`DEL` 需要 `DELETE` 权限,加入 DENY 会
  破坏 `cmd` 内建读取);也无法阻止对**不继承父目录 ACL** 的已有文件的
  修改(这类文件的权限由其自身 ACL 决定)。这两种情况在文档层面视为
  已知限制,核心语义(不能创建/写入文件)得到保证。
- 沙盒内 `git` 通过环境变量注入 `safe.directory=*` 避免跨用户
  "dubious ownership" 误报。
- **尽力而为,非安全边界**:本实现是命令级隔离的尽力而为方案。受限令牌
  拦截的是"通过沙盒用户执行"的命令对文件系统的越权写入;进程内工具
  (`apply_patch` / `read`) 、GUI 操作、以及有权限提升通道的程序仍不在
  本机制的约束内。不要把它当作对抗恶意代码的沙箱。
- 工作区 ACL 修改会保留在文件权限中;如需清理,可手动用 `icacls` 撤销
  相应 ACE。
- 若组策略禁止创建防火墙规则,文件隔离仍生效,但网络隔离会降级
  (预置结果会记录该错误)。
- 沙盒不是 VM:不隔离显卡、剪贴板等同会话资源,也不保证防数据泄漏。
