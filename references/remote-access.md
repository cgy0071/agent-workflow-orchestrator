# Remote command line from a phone (Windows, optional)

English | [Chinese](remote-access.zh-CN.md)

This is an optional add-on, not part of the workflow tool. It gives a Windows PC a command line you can reach from your phone through Tailscale. Use it to check status, read output or run a command when you are away. To view the dashboard instead, see [Phone access through Tailscale](monitoring.md#phone-access-through-tailscale).

The files are in [contrib/windows-remote/](../contrib/windows-remote/):

| File | Purpose |
|---|---|
| `setup-ssh-tailscale.ps1` | Sets up Windows OpenSSH so that it is reachable only through Tailscale and accepts only your key |
| `hd.cmd`, `hd.ps1` | Short commands for reading and typing into Herdr panes over SSH |

## Choose a tool

| | Remote desktop app | SSH through Tailscale |
|---|---|---|
| Herdr and other full-screen programs | Work as on the PC | Herdr's full-screen UI renders poorly; use `hd` |
| Where the traffic goes | Usually through the vendor's servers | Only your own tailnet |
| Login | The vendor's account | A key that stays on your phone |
| Slow or lossy network | Blurry, laggy picture | Still usable; text uses little data |

A remote desktop app is the easier way to drive Herdr. SSH is a private backup for quick checks and commands.

## Set up SSH

You need Tailscale installed and signed in to the same account on the PC and the phone, and an SSH client on the phone, for example Termius.

1. **Make a key on the phone.** In Termius: **Vaults -> Keychain -> + -> Generate Key**, type **SSH key**, **ED25519**. Copy the public key, one line starting with `ssh-ed25519`, to the PC and save it as a file, for example `E:\phone.pub`. The private key never leaves the phone.
2. **Run the setup script** in an elevated PowerShell (Run as administrator):

   ```powershell
   powershell -ExecutionPolicy Bypass -File contrib\windows-remote\setup-ssh-tailscale.ps1 -PublicKeyFile E:\phone.pub
   ```

   It installs OpenSSH Server on first use and prints the address, user name and host-key fingerprint. Add `-Shell powershell` to start sessions in PowerShell instead of cmd.
3. **Add the host on the phone.** In Termius: **Hosts -> + -> New Host**. Use the printed `100.x` address, port 22, the printed user name and the key from step 1. Leave the password empty. On first connect, accept only if the fingerprint matches the one the script printed.

Run the script again if the PC's Tailscale address changes. Run it with `-Disable` to stop SSH and remove its firewall rule and startup task. Installed files and keys are kept.

## What the script locks down

- **Keys only.** Password and keyboard-interactive logins are off. Only the Windows user who ran the script may log in. Port and X11 forwarding are off.
- **Tailscale only.** sshd listens on the PC's Tailscale address. The stock any-address firewall rule is disabled, and a new rule allows port 22 only from `100.64.0.0/10`. The PC is not reachable over the LAN or the internet.
- **Key file permissions.** For an administrator account the key goes to `%ProgramData%\ssh\administrators_authorized_keys`, readable only by SYSTEM and Administrators, as Windows OpenSSH requires.
- **Safe edits.** The original `sshd_config` is backed up first, and `sshd -t` must accept the new one. Rerunning replaces the script's own block and does not duplicate it.
- **Startup.** At boot the Tailscale address appears after services start, so a SYSTEM startup task waits for it and then starts sshd. sshd also restarts after a crash.

If other people's devices are in your tailnet, they still cannot log in without your phone's private key. To also block the connection itself, add a Tailscale access rule that lets only your own devices reach this PC on port 22.

Tailscale's own SSH server is not used because it runs only on Linux and macOS ([Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh)).

## Herdr over SSH

Herdr's full-screen interface does not work reliably through Windows OpenSSH on a phone terminal: the layout breaks and key bindings are lost. Use its command line instead. Copy `hd.cmd` and `hd.ps1` together into a folder on your PATH, then:

```text
hd list                     list panes: id, agent, status, title
hd read w1:p2               the pane's last 40 lines (add a number for more)
hd say w1:p2 continue       type the text into the pane and press Enter
hd keys w1:p2 esc           send keys such as esc or enter
```

Text sent with `hd say` is you talking to that agent directly, the same as typing at the PC. It counts as your own instruction, including any approval it contains.

## Tips and limits

- If Chinese or other non-ASCII text is garbled in cmd, run `chcp 65001`.
- On Android only one VPN runs at a time. Turn off a proxy app before connecting, or Tailscale is disconnected. Allow Tailscale to run in the background without battery restrictions.
- If a proxy on the PC runs in TUN mode, Tailscale may fall back to its relay servers. It still works, with higher latency. If the connection fails, add direct rules for `tailscaled.exe` and `100.64.0.0/10` in the proxy.
- Tested on one Windows 11 Pro PC with a local administrator account and an Android phone. Not yet confirmed: startup after a reboot, and `hd say` / `hd keys`.
