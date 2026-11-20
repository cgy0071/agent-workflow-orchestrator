# Optional phone-friendly Herdr helper for SSH sessions. It uses Herdr's command line, not its full-screen
# UI, which renders poorly through Windows OpenSSH on phone terminals. See references/remote-access.md.
#   hd list                    list panes: id, agent, status, title
#   hd read <pane> [lines]     show the pane's recent output (default 40 lines)
#   hd say <pane> <text...>    type the text into the pane and press Enter
#   hd keys <pane> <key...>    send keys, e.g.  hd keys w1:p2 esc   or   hd keys w1:p2 enter
# Text typed into an agent pane is you talking to that agent directly, the same as typing at the PC.
$ErrorActionPreference = 'Stop'
$found = Get-Command herdr.exe -ErrorAction SilentlyContinue
$herdr = if ($found) { $found.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Herdr\bin\herdr.exe' }
if (-not (Test-Path -LiteralPath $herdr)) { Write-Host 'herdr not found on PATH or in %LOCALAPPDATA%\Programs\Herdr\bin'; exit 1 }
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$cmd = if ($args.Count) { [string]$args[0] } else { 'help' }
switch ($cmd) {
    'list' {
        $j = (& $herdr pane list) -join "`n" | ConvertFrom-Json
        foreach ($p in $j.result.panes) {
            $title = [string]$p.terminal_title_stripped
            if ($title.Length -gt 34) { $title = $title.Substring(0, 34) }
            '{0,-8} {1,-7} {2,-8} {3}' -f $p.pane_id, $(if ($p.agent) { $p.agent } else { '-' }), $p.agent_status, $title
        }
    }
    'read' {
        if ($args.Count -lt 2) { Write-Host 'usage: hd read <pane> [lines]'; exit 1 }
        $n = if ($args.Count -ge 3) { [string]$args[2] } else { '40' }
        & $herdr pane read ([string]$args[1]) --source recent --lines $n
    }
    'say' {
        if ($args.Count -lt 3) { Write-Host 'usage: hd say <pane> <text...>'; exit 1 }
        $text = ($args[2..($args.Count - 1)] | ForEach-Object { [string]$_ }) -join ' '
        & $herdr pane run ([string]$args[1]) $text | Out-Null
        Write-Host "sent to $($args[1])"
    }
    'keys' {
        if ($args.Count -lt 3) { Write-Host 'usage: hd keys <pane> <key...>'; exit 1 }
        & $herdr pane send-keys ([string]$args[1]) @($args[2..($args.Count - 1)] | ForEach-Object { [string]$_ }) | Out-Null
        Write-Host "keys sent to $($args[1])"
    }
    default {
        Write-Host 'hd list | hd read <pane> [lines] | hd say <pane> <text...> | hd keys <pane> <key...>'
    }
}
