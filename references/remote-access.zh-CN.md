# 用手机远程使用命令行（Windows，可选）

[English](remote-access.md) | 中文

这是可选的附加功能，不属于工作流工具本身。它让你在手机上通过 Tailscale 连到一台 Windows 电脑的命令行，出门在外时可以查看状态、读输出或运行命令。如果只想看状态面板，见[通过 Tailscale 用手机访问](monitoring.zh-CN.md#通过-tailscale-用手机访问)。

文件在 [contrib/windows-remote/](../contrib/windows-remote/)：

| 文件 | 作用 |
|---|---|
| `setup-ssh-tailscale.ps1` | 配置 Windows OpenSSH：只能通过 Tailscale 连接，只接受你的密钥 |
| `hd.cmd`、`hd.ps1` | 在 SSH 里读取和输入 Herdr 窗口的短命令 |

## 选哪种方式

| | 远程桌面软件 | 通过 Tailscale 的 SSH |
|---|---|---|
| Herdr 等全屏程序 | 和在电脑上一样 | Herdr 全屏界面显示不正常，改用 `hd` |
| 流量经过哪里 | 通常经过厂商的服务器 | 只在你自己的 tailnet 里 |
| 登录方式 | 厂商账号 | 只存在手机上的密钥 |
| 网络慢或丢包时 | 画面模糊、卡顿 | 仍然能用，文字流量很小 |

操作 Herdr 用远程桌面软件更方便。SSH 适合作为私密的备用方式，用来快速查看和运行命令。

## 配置 SSH

电脑和手机都要安装 Tailscale 并登录同一个账号，手机上还要有 SSH 客户端，例如 Termius。

1. **在手机上生成密钥。** Termius 里依次点 **Vaults → Keychain → + → Generate Key**，选 **SSH key**、**ED25519**。把公钥（以 `ssh-ed25519` 开头的一行）发到电脑上，保存成文件，例如 `E:\phone.pub`。私钥一直留在手机上。
2. **运行配置脚本。** 在以管理员身份运行的 PowerShell 里执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File contrib\windows-remote\setup-ssh-tailscale.ps1 -PublicKeyFile E:\phone.pub
   ```

   第一次运行会安装 OpenSSH 服务器，然后打印地址、用户名和主机指纹。想让会话默认进入 PowerShell 而不是 cmd，加 `-Shell powershell`。
3. **在手机上添加主机。** Termius 里点 **Hosts → + → New Host**，填打印出的 `100.x` 地址、端口 22、打印出的用户名，密钥选第 1 步生成的那个，不填密码。第一次连接时，指纹和脚本打印的一致才接受。

电脑的 Tailscale 地址变了，重新运行一次脚本。加 `-Disable` 运行会停止 SSH，并删除它的防火墙规则和开机任务；已安装的文件和密钥会保留。

## 脚本做了哪些限制

- **只认密钥。** 关闭密码登录和键盘交互登录，只允许运行脚本的那个 Windows 用户登录，关闭端口转发和 X11 转发。
- **只走 Tailscale。** sshd 只监听电脑的 Tailscale 地址。系统自带的“任何地址都能连”的防火墙规则被关掉，新规则只放行 `100.64.0.0/10` 访问 22 端口。局域网和互联网都连不上。
- **密钥文件权限。** 管理员账户的公钥写入 `%ProgramData%\ssh\administrators_authorized_keys`，只有 SYSTEM 和 Administrators 能读，这是 Windows OpenSSH 的要求。
- **改配置前先备份。** 原 `sshd_config` 会先备份，新配置必须通过 `sshd -t` 检查。重复运行只替换脚本自己写的那一段，不会叠加。
- **开机启动。** 开机时 Tailscale 拿到地址比服务启动晚，所以由一个 SYSTEM 开机任务等到地址出现后再启动 sshd。sshd 崩溃后也会自动重启。

即使 tailnet 里有别人的设备，没有你手机上的私钥也登录不了。如果还想连连接本身也挡住，在 Tailscale 访问规则里只允许你自己的设备访问这台电脑的 22 端口。

没有用 Tailscale 自带的 SSH 服务端，因为它只能运行在 Linux 和 macOS 上（[Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh)）。

## 在 SSH 里用 Herdr

Herdr 的全屏界面经过 Windows OpenSSH 到手机终端时不稳定：布局错乱，快捷键失效。改用它的命令行：把 `hd.cmd` 和 `hd.ps1` 一起复制到 PATH 里的某个文件夹，然后：

```text
hd list                     列出窗口：编号、Agent、状态、标题
hd read w1:p2               窗口最近 40 行输出（后面加数字可以看更多）
hd say w1:p2 继续            向窗口输入文字并按回车
hd keys w1:p2 esc           发送按键，例如 esc 或 enter
```

用 `hd say` 发出的文字，就是你直接对那个 Agent 说的话，和坐在电脑前打字一样，算作你本人的指令，包括其中的授权。

## 提示与限制

- cmd 里中文乱码时，先运行 `chcp 65001`。
- Android 同时只能运行一个 VPN。连接前先关掉代理 App，否则 Tailscale 会被断开。把 Tailscale 设为允许后台运行、不限制电池。
- 电脑上的代理如果开了 TUN 模式，Tailscale 可能只能走中转服务器，仍然能用，只是延迟更高。连不上时，在代理里给 `tailscaled.exe` 和 `100.64.0.0/10` 加直连规则。
- 只在一台 Windows 11 专业版电脑（本地管理员账户）和一台 Android 手机上测试过。尚未确认：重启后的自动启动，以及 `hd say` / `hd keys`。
