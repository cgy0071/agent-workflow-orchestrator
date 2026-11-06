param(
    [string]$PublicKeyFile,          # the phone's public key (one line, ssh-ed25519 ...)
    [ValidateSet('cmd', 'powershell')][string]$Shell = 'cmd',
    [switch]$Disable                 # stop and disable SSH, remove the firewall rule and startup task
)
# Optional: a remote command line for a Windows PC, reachable only through Tailscale, key login only.
# Not part of the workflow tool; see references/remote-access.md. Run in an elevated (Administrator)
# Windows PowerShell 5.1. It prints no private data.
#
# What it changes:
#   - installs the OpenSSH Server optional feature and backs up %ProgramData%\ssh\sshd_config;
#   - sshd: keys only, no passwords, only this Windows user, no port or X11 forwarding,
#     listening only on this PC's Tailscale IPv4 address;
#   - firewall: disables the stock any-address OpenSSH rule, allows port 22 only from 100.64.0.0/10;
#   - default SSH shell (cmd or PowerShell) in HKLM:\SOFTWARE\OpenSSH;
#   - a SYSTEM startup task that waits for the Tailscale address, then starts sshd.
# Tested on Windows 11 Pro with a local administrator account. Tailscale's own SSH server does not
# run on Windows, which is why this uses Windows OpenSSH.
$ErrorActionPreference = 'Stop'
$ruleName = 'OpenSSH-Tailscale-Only'
$taskName = 'Start sshd after Tailscale'
$sshDir = Join-Path $env:ProgramData 'ssh'
$config = Join-Path $sshDir 'sshd_config'
$adminsSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')   # Administrators, any language
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole($adminsSid)
if (-not $isAdmin) { throw 'Run this in an elevated PowerShell (Run as administrator).' }

if ($Disable) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-Service sshd -ErrorAction SilentlyContinue) { Stop-Service sshd -ErrorAction SilentlyContinue; Set-Service sshd -StartupType Disabled }
    Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host 'SSH stopped and disabled; firewall rule and startup task removed. Installed files and keys are kept.'
    return
}

# 1. Tailscale must be installed and connected; SSH listens only on its address.
$tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (-not (Test-Path -LiteralPath $tailscale)) {
    $found = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($found) { $tailscale = $found.Source } else { throw 'Install Tailscale and sign in first: https://tailscale.com/download' }
}
$tsIp = (& $tailscale ip -4 2>$null | Select-Object -First 1)
if (-not $tsIp -or $tsIp -notmatch '^100\.') { throw 'Tailscale is not connected. Sign in, then run this again.' }

# 2. Public key from the phone.
if (-not $PublicKeyFile -or -not (Test-Path -LiteralPath $PublicKeyFile)) { throw 'Pass -PublicKeyFile with the phone public key (one line).' }
$key = (Get-Content -LiteralPath $PublicKeyFile -Raw).Trim()
if ($key -notmatch '^(ssh-ed25519|ecdsa-sha2-nistp(256|384|521)|ssh-rsa) [A-Za-z0-9+/=]{40,}( [^\r\n]{0,200})?$') {
    throw 'That does not look like one OpenSSH public key line (ssh-ed25519 AAAA... name).'
}
if ($key -match '^ssh-rsa') { Write-Host 'Note: an ed25519 key is preferred over RSA.' }

# 3. Install OpenSSH Server (a Windows optional feature).
$cap = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($cap.State -ne 'Installed') {
    Write-Host 'Installing OpenSSH Server...'
    Add-WindowsCapability -Online -Name $cap.Name | Out-Null
}
# Start once so it creates the default config and host keys, then stop to configure.
Start-Service sshd; Stop-Service sshd

# 4. Configuration: keys only, no passwords, listen only on the Tailscale address, only this user.
Copy-Item -LiteralPath $config -Destination "$config.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
$managed = @(
    '# --- managed by setup-ssh-tailscale.ps1 ---',
    "ListenAddress $tsIp",
    'PubkeyAuthentication yes',
    'PasswordAuthentication no',
    'KbdInteractiveAuthentication no',
    'PermitEmptyPasswords no',
    'AllowTcpForwarding no',
    'X11Forwarding no',
    'MaxAuthTries 3',
    'LoginGraceTime 30',
    "AllowUsers $($env:USERNAME.ToLower())",
    '# --- end managed ---'
)
$text = Get-Content -LiteralPath $config -Raw
$text = [regex]::Replace($text, '(?s)# --- managed by setup-ssh-tailscale\.ps1 ---.*?# --- end managed ---\r?\n?', '')
# Global settings must come before any "Match" block; the stock file ends with "Match Group administrators".
$at = [regex]::Match($text, '(?m)^Match ')
$block = ($managed -join "`r`n") + "`r`n"
$text = if ($at.Success) { $text.Insert($at.Index, $block) } else { $text.TrimEnd() + "`r`n" + $block }
[System.IO.File]::WriteAllText($config, $text, (New-Object System.Text.UTF8Encoding($false)))
& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t
if ($LASTEXITCODE -ne 0) { throw 'sshd -t rejected the configuration; the previous file is kept as sshd_config.bak-*' }

# 5. Authorized key. Members of Administrators use one shared file that only SYSTEM and Administrators may access.
$isGroupAdmin = [bool](Get-LocalGroupMember -SID $adminsSid.Value | Where-Object { $_.SID -eq [Security.Principal.WindowsIdentity]::GetCurrent().User })
$auth = if ($isGroupAdmin) { Join-Path $sshDir 'administrators_authorized_keys' } else { Join-Path $env:USERPROFILE '.ssh\authorized_keys' }
New-Item -ItemType Directory -Force -Path (Split-Path $auth) | Out-Null
$existing = if (Test-Path -LiteralPath $auth) { @(Get-Content -LiteralPath $auth) } else { @() }
if ($existing -notcontains $key) { Add-Content -LiteralPath $auth -Value $key -Encoding ascii }
$owner = if ($isGroupAdmin) { '*S-1-5-32-544' } else { "*$([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)" }
icacls $auth /inheritance:r /grant "${owner}:F" /grant '*S-1-5-18:F' | Out-Null   # S-1-5-18 = SYSTEM

# 6. Default shell for SSH sessions.
$shellPath = if ($Shell -eq 'cmd') { "$env:WINDIR\System32\cmd.exe" } else { "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" }
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value $shellPath -PropertyType String -Force | Out-Null

# 7. Firewall: replace the stock any-address rule with one limited to Tailscale's 100.64.0.0/10 range.
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH (Tailscale only)' -Direction Inbound -Protocol TCP `
    -LocalPort 22 -RemoteAddress '100.64.0.0/10' -Action Allow -Profile Any | Out-Null

# 8. At boot the Tailscale address appears after services start, so sshd is started by a task that waits for it.
Set-Service sshd -StartupType Manual
sc.exe failure sshd reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
$wait = "`$ts = '$tailscale'; for (`$i = 0; `$i -lt 60; `$i++) { if ((& `$ts ip -4 2>`$null) -match '^100\.') { break }; Start-Sleep 5 }; " +
        "if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd }"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -Command "' + $wait + '"')
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Start-Service sshd

$fingerprint = & "$env:WINDIR\System32\OpenSSH\ssh-keygen.exe" -lf (Join-Path $sshDir 'ssh_host_ed25519_key.pub')
$name = (& $tailscale status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
Write-Host ''
Write-Host 'SSH is on. Connect from a device in your tailnet:'
Write-Host "  ssh $($env:USERNAME.ToLower())@$name      (or $tsIp)"
Write-Host "Default shell: $Shell. Type 'powershell' or 'cmd' inside a session to switch."
Write-Host "Host key to compare on first connect: $fingerprint"
Write-Host 'If the Tailscale address ever changes, run this script again. To turn SSH off: run it with -Disable.'
