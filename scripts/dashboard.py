"""Local, ledger-first workflow dashboard with explicit single-attempt dispatch."""

import argparse
import copy
import errno
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import workflow_control as control
import workflow_scheduler as scheduler
import workflow_status
from monitor_task import Transport, identity_matches, sequence, validate_request


SCRIPTS = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
RECENT_FILE = Path.home() / ".agent-workflow-orchestrator" / "dashboard-recent.json"
RECENT_LIMIT = 10
TASK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
NONCE = re.compile(r"[0-9a-f]{16}\Z")
EVENT_ID = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACT_LIMIT = 200000  # bytes of each delivery file shown in the page
CHAT_MESSAGE_LIMIT = 20000  # characters in one chat message
CHAT_OUTPUT_LIMIT = 60000  # characters of recent role output shown in the page
PUBLIC_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
PUBLIC_NETLOC = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?\Z")
TLS_DIR = Path.home() / ".agent-workflow-orchestrator" / "tls"


def _private_lan_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise ValueError("--lan needs a private IPv4 address (10/8, 172.16/12, or 192.168/16)") from exc
    if not any(address in network for network in (
            ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"))):
        raise ValueError("--lan rejects loopback, public, link-local, and wildcard addresses; use a private IPv4")
    return str(address)


def _lan_candidates():
    """Find locally configured addresses without contacting another host."""
    found = set()
    if os.name == "nt":
        import winreg
        root = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as interfaces:
            for index in range(winreg.QueryInfoKey(interfaces)[0]):
                with winreg.OpenKey(interfaces, winreg.EnumKey(interfaces, index)) as interface:
                    for name in ("IPAddress", "DhcpIPAddress"):
                        try:
                            value = winreg.QueryValueEx(interface, name)[0]
                        except OSError:
                            continue
                        found.update(value if isinstance(value, list) else [value])
    else:
        import ctypes

        class Ifaddrs(ctypes.Structure):
            pass

        Ifaddrs._fields_ = [("next", ctypes.POINTER(Ifaddrs)), ("name", ctypes.c_char_p),
                            ("flags", ctypes.c_uint), ("addr", ctypes.c_void_p),
                            ("netmask", ctypes.c_void_p), ("dstaddr", ctypes.c_void_p),
                            ("data", ctypes.c_void_p)]
        libc = ctypes.CDLL(None)
        libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(Ifaddrs))]
        libc.getifaddrs.restype = ctypes.c_int
        libc.freeifaddrs.argtypes = [ctypes.POINTER(Ifaddrs)]
        head = ctypes.POINTER(Ifaddrs)()
        if libc.getifaddrs(ctypes.byref(head)) != 0:
            raise OSError("could not enumerate local network adapters")
        try:
            current = head
            while current:
                address = current.contents.addr
                if address:
                    raw = ctypes.string_at(address, 8)
                    family = raw[1] if sys.platform == "darwin" else raw[0]
                    if family == socket.AF_INET:
                        found.add(socket.inet_ntoa(raw[4:8]))
                current = current.contents.next
        finally:
            libc.freeifaddrs(head)
    candidates = set()
    for value in found:
        try:
            address = _private_lan_ip(value)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((address, 0))  # Reject stale adapter entries without sending any packets.
            candidates.add(address)
        except (ValueError, OSError):
            continue
    return sorted(candidates, key=lambda value: int(ipaddress.IPv4Address(value)))


def _choose_lan(value, candidates=None):
    if value != "auto":
        return _private_lan_ip(value)
    addresses = _lan_candidates() if candidates is None else sorted(set(candidates))
    if not addresses:
        raise ValueError("--lan auto found no local private IPv4 address; specify --lan ADDRESS after connecting")
    if len(addresses) > 1:
        raise ValueError("--lan auto found " + str(len(addresses)) + " private IPv4 addresses (" +
                         ", ".join(addresses) + "); specify --lan ADDRESS explicitly")
    return _private_lan_ip(addresses[0])


def _cert_valid(cert, key, address):
    if not cert.is_file() or not key.is_file():
        return False
    try:
        info = ssl._ssl._test_decode_cert(str(cert))
        if ("IP Address", address) not in info.get("subjectAltName", ()):
            return False
        if ssl.cert_time_to_seconds(info["notAfter"]) <= time.time() + 86400:
            return False
        if ssl.cert_time_to_seconds(info["notBefore"]) > time.time():
            return False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        return True
    except (OSError, ValueError, ssl.SSLError):
        return False


def _openssl_executable():
    command = shutil.which("openssl")
    if command:
        return command
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent.parent
            for candidate in (root / "usr" / "bin" / "openssl.exe", root / "mingw64" / "bin" / "openssl.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


def _lan_certificate(address, directory=TLS_DIR):
    directory = Path(directory).expanduser()
    cert, key = directory / (address + ".crt"), directory / (address + ".key")
    if _cert_valid(cert, key, address):
        return cert, key
    openssl = _openssl_executable()
    if not openssl:
        raise ValueError("OpenSSL is required to generate a LAN certificate; install it or provide --tls-cert and --tls-key")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with tempfile.TemporaryDirectory(prefix="dashboard-tls-", dir=directory) as temporary:
        generated_cert, generated_key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
        command = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "365",
                   "-subj", "/CN=" + address, "-addext", "subjectAltName=IP:" + address,
                   "-keyout", str(generated_key), "-out", str(generated_cert)]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=30)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError("OpenSSL could not generate a LAN certificate; provide --tls-cert and --tls-key") from exc
        if not _cert_valid(generated_cert, generated_key, address):
            raise ValueError("generated LAN certificate failed validation")
        os.chmod(generated_key, 0o600)
        os.replace(generated_key, key)
        os.replace(generated_cert, cert)
    return cert, key


def _certificate_fingerprint(cert):
    pem = Path(cert).read_text(encoding="ascii")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()


def _public_origin(value):
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("public origin must be an https://hostname[:port] URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public origin must be an https://hostname[:port] URL") from exc
    host = parsed.hostname
    if (parsed.scheme != "https" or not host or not PUBLIC_NETLOC.fullmatch(parsed.netloc)
            or not all(PUBLIC_LABEL.fullmatch(part) for part in host.split("."))
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or (port is not None and port == 0)):
        raise ValueError("public origin must be an https://hostname[:port] URL without a path")
    return "https://" + host.lower() + ((":" + str(port)) if port is not None else "")
# UI translations live beside this file; JSON-in-script stays inert and cannot close its tag.
I18N_ZH = json.dumps(json.loads((Path(__file__).resolve().parent / "dashboard_i18n.zh-CN.json")
                                .read_text(encoding="utf-8")), ensure_ascii=True).replace("</", "<\\/")
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workflow Dashboard</title>
<style nonce="__CSP_NONCE__">
:root { color-scheme:light; --bg:#f3f5f4; --surface:#fff; --ink:#172b32; --muted:#60757b;
  --line:#dce5e3; --accent:#126b66; --accent-soft:#e5f3ef; --danger:#a43e37; --warn:#9a681d;
  --shadow:0 10px 30px rgba(22,47,50,.055); --hero:#173b40; --hero-deep:#10292f; --hero-muted:#b5d4d2; }
@media (prefers-color-scheme:dark) { :root { color-scheme:dark; --bg:#101d22; --surface:#1a2b30;
  --ink:#ecf5f1; --muted:#a7bdbd; --line:#385057; --accent:#8bdfc9; --accent-soft:#23463e;
  --danger:#f2a59d; --warn:#f0c580; --shadow:0 12px 36px rgba(0,0,0,.14);
  --hero:#1d464b; --hero-deep:#163238; --hero-muted:#c0e0dc; } }
* { box-sizing:border-box } html,body { margin:0; min-width:0 } body { background:var(--bg); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; overflow-x:hidden }
.shell { max-width:1460px; margin:auto; padding:22px clamp(14px,3vw,40px) 70px }
.masthead { position:relative; overflow:hidden; display:flex; gap:24px; align-items:flex-start;
  justify-content:space-between; flex-wrap:wrap; padding:30px 32px; border-radius:22px;
  background:radial-gradient(circle at 85% 15%,rgba(113,211,188,.15) 0 1px,transparent 2px) 0 0/18px 18px,
    linear-gradient(120deg,var(--hero-deep),var(--hero)); color:#f1faf7; box-shadow:0 16px 36px rgba(15,42,46,.12) }
.hero-copy { min-width:0; flex:1 1 390px; position:relative; z-index:1 }
.eyebrow { display:flex; align-items:center; gap:9px; text-transform:uppercase; font-size:11px;
  letter-spacing:.16em; font-weight:800; color:#9ce4cc }
.eyebrow:before { content:""; width:7px; height:7px; border-radius:50%; background:#9ce4cc;
  box-shadow:0 0 0 4px rgba(156,228,204,.15) }
h1 { margin:9px 0 12px; font-size:clamp(31px,3.5vw,48px); line-height:1.08; letter-spacing:-.045em }
h2 { margin:0; font-size:18px; line-height:1.25; letter-spacing:-.025em }
h3 { margin:0; font-size:15px } p { margin:7px 0 } small,.muted { color:var(--muted) }
.masthead .sub { color:var(--hero-muted) } .sub { max-width:900px; overflow-wrap:anywhere }
.state-path { display:inline-block; max-width:100%; padding:8px 11px; margin:2px 0 0; border-radius:9px;
  color:#d8ece7 !important; background:rgba(255,255,255,.09); border:1px solid rgba(255,255,255,.12);
  font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace }
.masthead #goal { margin:11px 0 0; font-size:13px }
.toolbar { display:flex; justify-content:flex-end; align-items:center; gap:8px; flex-wrap:wrap;
  max-width:500px; position:relative; z-index:1 }
button { cursor:pointer; border:1px solid var(--line); border-radius:9px; background:var(--surface);
  color:var(--ink); padding:9px 13px; font:inherit; font-weight:700; transition:background .15s,border-color .15s,transform .15s }
button:hover { border-color:var(--accent); color:var(--accent); transform:translateY(-1px) }
button:disabled { opacity:.45; cursor:default; transform:none } button:focus-visible,input:focus-visible,
select:focus-visible,textarea:focus-visible { outline:3px solid #5abfae; outline-offset:2px }
.masthead button { background:rgba(255,255,255,.1); color:#f5fcf9; border-color:rgba(255,255,255,.23) }
.masthead button:hover { background:rgba(255,255,255,.2); color:#fff; border-color:#9ce4cc }
button.primary { background:var(--accent); color:var(--bg); border-color:var(--accent) }
input,select,textarea { min-width:0; width:100%; border:1px solid var(--line); border-radius:9px;
  background:var(--surface); color:var(--ink); font:inherit; padding:10px 11px }
textarea { min-height:116px; resize:vertical } input[type=checkbox] { width:auto; margin:0 5px 0 0 }
label { display:block; font-weight:650; font-size:13px } label span { display:block; margin-bottom:5px }
.inline-label { display:flex; align-items:center; font-size:12px; color:var(--hero-muted); font-weight:650;
  padding:8px 9px; white-space:nowrap }
.section-meta { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
  margin:15px 2px 14px; color:var(--muted); font-size:12px }
.section-meta #updated { margin:0 } .local-tag { color:var(--accent); font-size:11px; font-weight:800;
  letter-spacing:.1em; text-transform:uppercase }
.overview { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0 0 }
.metric { min-width:0; padding:16px 19px; border:1px solid var(--line); border-radius:15px;
  background:var(--surface); box-shadow:var(--shadow) }
.metric-label { color:var(--muted); font-size:11px; font-weight:750; letter-spacing:.09em; text-transform:uppercase }
.metric-value { display:block; margin:3px 0 0; font-size:29px; font-weight:780; line-height:1.1; letter-spacing:-.05em }
.metric-note { display:block; margin-top:5px; color:var(--muted); font-size:11px; overflow-wrap:anywhere }
.grid { display:grid; grid-template-columns:minmax(0,2fr) minmax(280px,1fr); gap:16px }
.stack { display:grid; gap:18px } .panel { min-width:0; background:var(--surface); border:1px solid var(--line);
  border-radius:17px; padding:21px; box-shadow:var(--shadow) }
.panel-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:17px }
.panel-caption { font-size:11px; font-weight:700; letter-spacing:.04em; color:var(--muted) }
.wide { grid-column:1 / -1 } .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(235px,1fr)); gap:12px }
.role { border:1px solid var(--line); border-radius:13px; padding:15px; min-width:0;
  background:linear-gradient(160deg,var(--accent-soft),var(--surface) 38%); }
.role-top { display:flex; align-items:center; gap:10px; margin-bottom:14px }
.role-mark { display:grid; place-items:center; flex:none; width:34px; height:34px; border-radius:10px;
  color:var(--accent); background:var(--accent-soft); font-size:16px; font-weight:800; text-transform:uppercase }
.role-top h3 { margin:0; font-size:15px; line-height:1.15 }
.role-top .badge { margin-left:auto } .binding { margin:0 0 10px }
.role-meta { display:grid; grid-template-columns:78px minmax(0,1fr); gap:5px 8px; margin:0;
  padding:11px 0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); font-size:12px }
.meta-key { color:var(--muted) } .meta-value { font-weight:650; overflow-wrap:anywhere }
.role-tasks { margin:10px 0 0; font-size:12px; color:var(--muted); overflow-wrap:anywhere }
.role-summary { margin:5px 0; font-size:12px; color:var(--muted) }
.technical { margin-top:9px; font-size:12px; color:var(--muted) }
.technical summary { cursor:pointer; font-weight:700; color:var(--accent) }
.technical p { margin:5px 0; overflow-wrap:anywhere }
.technical .role-meta { margin-top:8px }
.badge { display:inline-block; border-radius:30px; padding:3px 9px; font-size:11px; font-weight:800; white-space:nowrap;
  letter-spacing:.015em; background:var(--accent-soft); color:var(--accent) }
.badge.accepted,.badge.submitted { background:#e3f3e9; color:#246a47 }
.badge.executing,.badge.running { background:#e5efff; color:#285c96 }
.badge.execution_unconfirmed,.badge.scheduling_blocked,.badge.awaiting_review { background:#fff0d5; color:#855a19 }
.badge.failed,.badge.review_blocked,.badge.needs_inspection { background:#fce9e5; color:#a43e37 }
@media (prefers-color-scheme:dark) { .badge.accepted,.badge.submitted { background:#234735;color:#a4e5b9 }
  .badge.executing,.badge.running { background:#243f5c;color:#a9ceff }
  .badge.execution_unconfirmed,.badge.scheduling_blocked,.badge.awaiting_review { background:#513d1d;color:#f5d99e }
  .badge.failed,.badge.review_blocked,.badge.needs_inspection { background:#50322f;color:#f6aaa1 } }
.table-scroll { max-width:100%; overflow-x:auto } table { width:100%; border-collapse:collapse; font-size:12px }
th { text-align:left; color:var(--muted); font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  border-bottom:1px solid var(--line); white-space:nowrap }
th,td { padding:12px 9px; vertical-align:top; overflow-wrap:break-word }
th:nth-child(2),th:nth-child(5),td:nth-child(2),td:nth-child(5) { min-width:90px; word-break:keep-all }
th:nth-child(4),th:nth-child(7),th:nth-child(8),td:nth-child(4),td:nth-child(7),td:nth-child(8) {
  min-width:115px; word-break:keep-all }
td { border-bottom:1px solid var(--line) } .task-row td:first-child { color:var(--ink); min-width:220px;
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif }
tr.task-row { cursor:pointer; transition:background .15s } tr.task-row:hover { background:var(--accent-soft) }
tr.task-row td:last-child { color:var(--warn) } tr.task-row td.info-note { color:var(--muted) }
.task-row td:nth-child(6) { min-width:170px }
.detail { background:var(--bg); padding:13px; border-radius:9px; overflow-wrap:anywhere }
.results { margin:9px 0 } .result-file { margin-top:10px } .result-file p { margin:3px 0 }
.task-name span { display:block } .task-title { font-weight:800; color:var(--ink) }
.task-excerpt { margin-top:3px; font-size:12px; color:var(--muted); max-width:46ch; line-height:1.4;
  display:-webkit-box !important; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden }
.task-id { margin-top:3px; font:400 10px ui-monospace,SFMono-Regular,Consolas,monospace; color:var(--muted) }
.result-file pre { max-height:360px; overflow:auto }
#chat-output { max-height:420px; overflow:auto } #chat-input { min-height:80px } .chat-entry p { margin:3px 0 0; white-space:pre-wrap }
.sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap }
pre { margin:7px 0; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
  background:var(--bg); border:1px solid var(--line); padding:12px; border-radius:9px; max-height:320px; overflow:auto }
.dag-run { min-width:0 } .dag-title { display:flex; align-items:center; gap:9px; margin:0 0 11px }
.dag-count { color:var(--muted); font-size:11px }
.dag-scroll { width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:12px;
  padding:12px; background:var(--bg) }
svg { display:block; max-width:none; height:auto; margin:auto } svg text { font:13px system-ui,sans-serif; fill:var(--ink) }
svg line { stroke:var(--muted); stroke-width:1.5 } svg rect { fill:var(--surface); stroke:var(--line); stroke-width:1.5 }
svg rect.accepted { stroke:#3baf70; stroke-width:2 } svg rect.executing { stroke:#478cd7; stroke-width:2 }
svg rect.scheduling_blocked,svg rect.review_blocked { stroke:var(--warn); stroke-width:2 }
.list { display:grid; gap:8px } .list-item { padding:9px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere }
.event-item,.alert-item { min-width:0; padding:10px 12px; border:1px solid var(--line); border-radius:10px;
  background:var(--bg) } .event-head { display:flex; justify-content:space-between; align-items:center; gap:8px }
.event-item small { display:block; overflow-wrap:anywhere; font-size:11px; margin-top:5px }
.event-item strong { font:700 13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif }
.alert-item { border-left:3px solid var(--warn); padding:13px 15px }
.alert-item.info { border-left-color:var(--accent) }
.alert-head { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:7px }
.alert-head strong { font:700 12px ui-monospace,SFMono-Regular,Consolas,monospace }
.alert-item h3 { margin:0 0 5px; font-size:13px; line-height:1.3 }
.alert-item p { margin:4px 0; font-size:12px; line-height:1.5 }
.alert-next { color:var(--ink); font-weight:650 }
.alert-technical { color:var(--muted); overflow-wrap:anywhere }
.detail-note { margin:10px 0; padding:10px 12px; border-left:3px solid var(--accent);
  background:var(--surface); border-radius:7px; font-size:12px }
.meter { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:center; margin:8px 0 }
progress { width:100%; height:12px; accent-color:var(--accent) }
.form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:13px }
.form-grid .full { grid-column:1 / -1 } .actions { display:flex; gap:9px; flex-wrap:wrap; margin-top:15px }
.notice { border-left:3px solid var(--accent); background:var(--accent-soft); padding:9px 12px; border-radius:5px; overflow-wrap:anywhere }
.error { color:var(--danger); font-weight:700 } .success { color:var(--accent); font-weight:700 }
#picker { margin-bottom:18px } .path-row { display:flex; gap:9px; align-items:flex-end; margin-top:12px }
.path-row label { flex:1; min-width:0 } .recent-item { text-align:left; width:100%; overflow-wrap:anywhere; font-weight:500 }
code { font:13px ui-monospace,SFMono-Regular,Consolas,monospace }
.hidden { display:none !important } .small-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap }
@media(max-width:900px) { .grid { grid-template-columns:1fr } .wide { grid-column:auto }
  .overview { grid-template-columns:repeat(2,minmax(0,1fr)) } }
@media(max-width:520px) { .shell { padding:12px 12px 44px } .masthead { padding:23px 18px; border-radius:16px }
  .toolbar { justify-content:flex-start } .inline-label { padding-left:0 } .panel { padding:16px }
  .metric { padding:13px 14px } .metric-value { font-size:25px } .form-grid { grid-template-columns:1fr }
  .form-grid .full { grid-column:auto } .cards { grid-template-columns:1fr } .table-scroll { overflow:visible }
  table,tbody,tr,td { display:block; width:100% } thead { display:none }
  tr.task-row { display:grid; grid-template-columns:1fr 1fr; gap:5px 8px; padding:11px 0; border-bottom:1px solid var(--line) }
  tr.task-row td { border:0; padding:0; min-width:0 }
  tr.task-row td::before { content:attr(data-label); display:block; color:var(--muted); font:700 10px/1.5 system-ui,sans-serif;
    letter-spacing:.06em; text-transform:uppercase }
  tr.task-row td:first-child,tr.task-row td:nth-child(6) { grid-column:1 / -1 }
  tr.task-row td:first-child { font-weight:800 }
  tr.detail-row td { border:0; padding:7px 0 } }
</style>
</head>
<body>
<div class="shell">
  <header class="masthead"><div class="hero-copy"><div class="eyebrow">Local workflow status</div>
    <h1>Workflow Dashboard</h1><p class="sub state-path" id="state" translate="no">Loading state...</p>
    <p class="sub" id="goal"></p></div>
    <div class="toolbar"><label class="inline-label"><input id="auto" type="checkbox" checked>Update page every 5s</label>
      <button id="refresh" type="button">Refresh page</button><button id="probe" type="button">Check workers now</button>
      <button id="chat-open" type="button" class="hidden">Chat</button>
      <button id="lang" type="button" translate="no" aria-label="Language">&#x4e2d;&#x6587;</button>
      <button id="switch" type="button" class="hidden">Choose workflow</button></div></header>
  <section class="overview" id="overview" aria-label="Workflow overview">
    <div class="metric"><span class="metric-label">Tasks</span><strong id="metric-tasks" class="metric-value">--</strong>
      <span id="metric-tasks-note" class="metric-note">Accepted tasks</span></div>
    <div class="metric"><span class="metric-label">Assigned roles</span><strong id="metric-roles" class="metric-value">--</strong>
      <span id="metric-roles-note" class="metric-note">Configured roles</span></div>
    <div class="metric"><span class="metric-label">Task plans</span><strong id="metric-dags" class="metric-value">--</strong>
      <span id="metric-dags-note" class="metric-note">Tasks linked by prerequisites</span></div>
    <div class="metric"><span class="metric-label">Completion updates</span><strong id="metric-inbox" class="metric-value">--</strong>
      <span id="metric-inbox-note" class="metric-note">Waiting for review</span></div>
  </section>
  <div class="section-meta"><p id="updated">Waiting for first update</p>
    <span class="local-tag">Page updates do not contact workers</span></div><p class="error" id="error" role="alert"></p>
  <section class="panel hidden" id="picker"><h2>Choose a workflow</h2>
    <p class="muted">Select a recent workflow data folder, or enter the full path created when the workflow was initialized.</p>
    <div id="recent" class="list"></div>
    <div class="path-row"><label><span>Workflow data folder</span><input id="state-input" autocomplete="off"
      placeholder="E:\work\state  or  /home/me/work/state"></label>
      <button id="open-state" class="primary" type="button">Open</button></div>
    <p id="picker-error" class="error" role="alert"></p></section>
  <div class="grid" id="main">
    <section class="panel wide hidden" id="chat-panel"><div class="panel-head"><h2>Chat with a role</h2>
      <span class="panel-caption" id="chat-caption">Sent by the bound controller</span></div>
      <p class="muted">Messages go to the selected agent's session as typed input.
        Each send is one attempt and is recorded in the ledger audit (hash and length only).</p>
      <label><span>Conversation target</span><select id="chat-role"></select></label>
      <p id="chat-busy" class="notice hidden"></p>
      <label id="chat-busy-confirm-row" class="hidden"><input id="chat-confirm-busy" type="checkbox">I understand this message may interrupt the active task</label>
      <label class="full"><span class="sr-only">Message</span>
        <textarea id="chat-input" maxlength="20000" placeholder="Ask this role about progress, or give an instruction. Ctrl+Enter sends."></textarea></label>
      <div class="actions"><button id="chat-send" class="primary" type="button">Send</button>
        <button id="chat-read" type="button">Read recent output</button></div>
      <p id="chat-status" role="status"></p>
      <div id="chat-log" class="list"></div>
      <pre id="chat-output" class="hidden"></pre></section>
    <section class="panel wide"><div class="panel-head"><h2>People and agents</h2>
      <span class="panel-caption">Who can work on these tasks</span></div><div id="roles" class="cards"></div></section>
    <section class="panel wide"><div class="panel-head"><h2>Tasks</h2>
      <span class="panel-caption">Select a row for details</span></div><div class="table-scroll"><table><thead><tr><th>Task</th><th>Stage</th>
      <th>Assigned to</th><th>Delivery</th><th>Review</th><th>Next step</th><th>Last check</th><th>Note</th>
      </tr></thead><tbody id="tasks"></tbody></table></div></section>
    <section class="panel wide"><div class="panel-head"><h2>Task plans</h2>
      <span class="panel-caption">A task can start after its prerequisites are accepted</span></div><div id="dags" class="stack"></div></section>
    <section class="panel"><div class="panel-head"><h2>Completion inbox</h2>
      <span class="panel-caption">Updates for reviewers</span></div><div id="inbox" class="list"></div></section>
    <section class="panel"><div class="panel-head"><h2>Resource limits</h2>
      <span class="panel-caption">Reserved capacity for active tasks</span></div><div id="resources"></div></section>
    <section class="panel"><div class="panel-head"><h2>Alerts & guidance</h2>
      <span class="panel-caption">Meaning and next step</span></div><div id="alerts" class="list"></div></section>
    <section class="panel" id="probe-panel"><div class="panel-head"><h2>Current worker check</h2>
      <span class="panel-caption">On demand only</span></div><p class="muted">The page refresh reads saved records. Use Check workers now to contact active workers.</p>
      <p id="probe-time" class="muted"></p><div id="probe-result" class="list"></div>
      <div class="small-actions"><button id="probe-top" type="button">Back to top</button>
        <button id="probe-clear" type="button">Clear</button></div></section>
    <section class="panel wide" id="dispatch-panel"><div class="panel-head"><h2>Start a task</h2>
      <span class="panel-caption">One send attempt after your confirmation</span></div>
      <p class="muted" id="dispatch-note"></p>
      <div id="dispatch-form" class="hidden">
        <div class="form-grid">
          <label><span>Who should do the work?</span><select id="worker-role"></select></label>
          <label><span>Task name or ID</span><input id="task-id" autocomplete="off" placeholder="TASK-123" maxlength="64"></label>
          <label><span>Approval source</span><input id="source" placeholder="Current owner instruction"></label>
          <label class="full"><span>What should they do? Include scope and acceptance checks.</span>
            <textarea id="body" placeholder="Objective:&#10;Allowed changes:&#10;Acceptance:"></textarea></label>
          <div class="full"><details class="technical"><summary>Advanced task settings</summary>
            <p>Use these only when the task needs different evidence files or reserved resources.</p>
            <div class="form-grid">
              <label><span>Completion marker file</span><input id="binding" value="HANDOFF.md"></label>
              <label><span>Other result files (comma separated)</span><input id="artifacts" value="RESULT.md"></label>
              <label><span>Resource reservation (JSON)</span><input id="resource-input" value="{}"></label>
              <label><span>One-time task token (created by preview)</span><input id="nonce" readonly></label>
              <label class="full"><input id="notify" type="checkbox">Allow a direct reviewer notification when the workflow policy permits it</label>
            </div></details></div>
        </div><div class="actions"><button id="preview" class="primary" type="button">Check and preview</button>
          <button id="confirm" type="button" disabled>Confirm and send once</button></div>
        <p id="dispatch-error" class="error" role="alert"></p>
        <div id="preview-output" class="hidden"><h3>Ready to send</h3><p class="muted">Check the complete request and worker message before confirming. This preview does not send anything.</p>
          <pre id="preview-request"></pre><pre id="preview-message"></pre></div>
        <div id="dispatch-output" class="hidden"><h3>Send result</h3><p id="dispatch-status"></p>
          <p class="notice">Sending a task does not prove the worker started. Confirm execution independently, write EXECUTION.md, then run the reconciliation command. If delivery is uncertain, inspect the record before any new action.</p>
          <div class="small-actions"><strong>EXECUTION.md template</strong><button type="button" data-copy="execution-template">Copy</button></div>
          <pre id="execution-template"></pre><div class="small-actions"><strong>Reconcile command</strong>
          <button type="button" data-copy="reconcile-command">Copy</button></div><pre id="reconcile-command"></pre>
          <div class="small-actions"><strong>Then monitor</strong><button type="button" data-copy="monitor-command">Copy</button></div>
          <pre id="monitor-command"></pre></div>
      </div></section>
    <section class="panel wide hidden" id="review-panel"><div class="panel-head"><h2>Review gate</h2>
      <span class="panel-caption">One ledger write after your confirmation</span></div>
      <p id="review-target" class="muted">Choose Acknowledge or Record review on a completion update.</p>
      <div id="review-form" class="hidden">
        <div class="form-grid" id="review-verdict-row"><label><span>Verdict (must match the Verdict: line in the review file)</span>
          <select id="review-verdict"></select></label></div>
        <div class="actions"><button id="review-preview" class="primary" type="button">Check and preview</button>
          <button id="review-confirm" type="button" disabled>Confirm once</button></div>
        <p id="review-error" class="error" role="alert"></p>
        <div id="review-output" class="hidden"><p id="review-summary" class="notice"></p><pre id="review-text"></pre></div>
        <p id="review-status"></p>
      </div></section>
  </div>
</div>
<script type="application/json" id="i18n-zh">__I18N_ZH__</script>
<script nonce="__CSP_NONCE__">
(() => {
  const id = name => document.getElementById(name);
  const token = location.hash.slice(1) || sessionStorage.getItem('dashboard-token') || '';
  if (token) { sessionStorage.setItem('dashboard-token', token); history.replaceState(null, '', location.pathname + location.search); }
  let preview = null;
  let lastData = null;
  const chatSessions = new Map();
  let chatBusyKey = '';
  const chatSession = role => {
    if (!chatSessions.has(role)) chatSessions.set(role, {draft:'', entries:[], output:null, status:''});
    return chatSessions.get(role);
  };
  // Elements that show ledger or agent data; the translator leaves them as written.
  const RAW = new Set(['task-title', 'task-excerpt', 'task-id', 'meta-value', 'role-mark', 'raw']);
  const el = (tag, className, value) => {
    const x = document.createElement(tag);
    if (className) x.className = className;
    if (className && className.split(' ').some(c => RAW.has(c))) x.setAttribute('translate', 'no');
    if (value !== undefined && value !== null) x.textContent = String(value);
    return x;
  };
  const add = (parent, tag, className, value) => { const x = el(tag, className, value); parent.append(x); return x; };
  const show = (name, visible) => id(name).classList.toggle('hidden', !visible);
  // Language: English source text, translated in place. Ledger data is marked translate="no" and left alone.
  const zh = JSON.parse(id('i18n-zh').textContent);
  const patterns = zh.patterns.map(([source, target]) => [new RegExp(source), target]);
  let lang = 'en';
  try { lang = localStorage.getItem('dashboard-lang') === 'zh' ? 'zh' : 'en'; } catch {}
  const asked = new URLSearchParams(location.search).get('lang');
  if (asked === 'zh' || asked === 'en') lang = asked;
  function tr(text) {
    const key = text.replace(/\s+/g, ' ').trim();
    if (lang !== 'zh' || !key) return text;
    if (zh.exact[key] !== undefined) return text.match(/^\s*/)[0] + zh.exact[key] + text.match(/\s*$/)[0];
    for (const [re, target] of patterns) {
      const m = key.match(re);
      if (m) return target.replace(/([$#])(\d)/g, (_, kind, n) => kind === '$' ? m[+n] : tr(m[+n]));
    }
    const parts = key.split('; ');
    if (parts.length > 1) { const done = parts.map(tr); if (done.some((x, i) => x !== parts[i])) return done.join('\uFF1B'); }
    const at = key.indexOf(': ');
    if (at > 0 && zh.exact[key.slice(0, at)] !== undefined)
      return zh.exact[key.slice(0, at)] + '\uFF1A' + tr(key.slice(at + 2));
    if (at > 0 && zh.exact[key.slice(at + 2)] !== undefined) return key.slice(0, at) + '\uFF1A' + zh.exact[key.slice(at + 2)];
    return text;
  }
  const baseText = new WeakMap(), baseAttr = new WeakMap(), ATTRS = ['placeholder', 'title', 'aria-label', 'data-label'];
  const skipped = node => { const p = node.nodeType === 1 ? node : node.parentElement;
    return !p || !!p.closest('script,style,pre,textarea,[translate="no"]'); };
  function localizeText(node) {
    if (skipped(node)) return;
    if (!baseText.has(node) || baseText.get(node).shown !== node.data) baseText.set(node, {source: node.data, shown: null});
    const record = baseText.get(node), next = lang === 'zh' ? tr(record.source) : record.source;
    record.shown = next; if (node.data !== next) node.data = next;
  }
  function localizeAttrs(element) {
    if (element.closest('[translate="no"]')) return;
    const saved = baseAttr.get(element) || {}; baseAttr.set(element, saved);
    for (const name of ATTRS) {
      const value = element.getAttribute(name); if (value === null) continue;
      if (!saved[name] || saved[name].shown !== value) saved[name] = {source: value, shown: null};
      const next = lang === 'zh' ? tr(saved[name].source) : saved[name].source;
      saved[name].shown = next; if (value !== next) element.setAttribute(name, next);
    }
  }
  function localize(root) {
    if (root.nodeType === 3) { localizeText(root); return; }
    if (root.nodeType !== 1) return;
    localizeAttrs(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let node = walker.nextNode(); node; node = walker.nextNode())
      node.nodeType === 3 ? localizeText(node) : localizeAttrs(node);
  }
  new MutationObserver(records => { for (const r of records) {
    if (r.type === 'childList') r.addedNodes.forEach(localize);
    else if (r.type === 'characterData') localizeText(r.target);
    else if (r.type === 'attributes') localizeAttrs(r.target);
  } }).observe(document.documentElement, {childList:true, subtree:true, characterData:true,
    attributes:true, attributeFilter:ATTRS});
  function setLang(next) {
    lang = next; try { localStorage.setItem('dashboard-lang', next); } catch {}
    document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en';
    id('lang').textContent = next === 'zh' ? 'English' : '\u4e2d\u6587';
    localize(document.documentElement);
  }
  async function api(path, payload) {
    if (!token) throw new Error('Open the URL printed by the dashboard server.');
    const options = {headers: {'X-Dashboard-Token': token}};
    if (payload !== undefined) { options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(payload); }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function empty(container, message) { container.replaceChildren(); add(container, 'p', 'muted', message); }
  function metrics(data) {
    const active = data.roles.filter(r => r.bindings.some(b => b.status === 'active'));
    const accepted = data.tasks.filter(t => t.phase === 'accepted').length;
    const pending = data.inbox.filter(e => e.status === 'pending').length;
    id('metric-tasks').textContent = data.tasks.length;
    id('metric-tasks-note').textContent = accepted + ' accepted';
    id('metric-roles').textContent = active.length;
    id('metric-roles-note').textContent = 'of ' + data.roles.length + ' configured';
    id('metric-dags').textContent = Object.keys(data.dags).length +
      Object.values(data.task_details).filter(t => t.parent_task).length;
    id('metric-dags-note').textContent = 'Task plans and follow-up links';
    id('metric-inbox').textContent = data.inbox.length;
    id('metric-inbox-note').textContent = pending + ' waiting for review';
  }
  const stageNames = {
    accepted:'Accepted', evidence_changed:'Accepted review; evidence changed', review_blocked:'Review needs follow-up',
    closed:'Closed after inspection', execution_unconfirmed:'Start not confirmed', awaiting_review:'Awaiting review',
    failed:'Worker stopped', needs_inspection:'Check worker status', executing:'In progress',
    scheduling_blocked:'Could not start', ready:'Ready to start', waiting:'Waiting for earlier tasks',
    blocked_dependency:'Earlier task needs attention', blocked:'Needs attention', under_review:'Under review',
    running:'In progress', submitted:'Sent; start unconfirmed', reserved:'Send reserved', uncertain:'Send uncertain',
    not_sent:'Not sent', terminated:'Stopped', pending:'Waiting for review', acknowledged:'Review acknowledged',
    reviewed:'Reviewed'
  };
  const reviewNames = {accept:'Accepted', reject:'Rejected', repair:'Needs repair'};
  const nextNames = {
    release_claim_or_advance:'Release reserved resources or continue the plan',
    inspect_changed_evidence:'Check why accepted evidence changed',
    plan_new_scoped_task:'Plan a follow-up task after the review',
    retain_evidence:'Keep the final evidence',
    inspect_execution_no_resend:'Check whether the worker started; do not resend blindly',
    acknowledge_and_review:'Acknowledge the update and review the evidence',
    inspect_termination:'Inspect why the worker stopped',
    inspect_monitor_and_worker:'Check the monitor record and worker',
    await_delivery:'Wait for the worker to deliver its result',
    inspect_notification_and_inbox:'Check the reviewer inbox; do not resend blindly',
    resolve_scheduling_block:'Resolve the start blocker',
    tick:'Run the scheduler to start this ready task',
    inspect_dependencies:'Check the earlier tasks'
  };
  const reportNames = {review_ready:'Work submitted for review', failed:'Worker failed', exited:'Worker exited'};
  const dagNames = {evidence_changed:'Evidence changed', execution_unconfirmed:'Start unconfirmed',
    blocked_dependency:'Prerequisite blocked', review_blocked:'Review follow-up',
    scheduling_blocked:'Could not start', waiting:'Waiting', ready:'Ready to start',
    under_review:'Under review', submitted:'Sent', uncertain:'Send uncertain'};
  function named(value, names) { return names[value] || (value ? String(value).replaceAll('_', ' ') : 'Not recorded'); }
  function deliveryText(task) {
    if (task.phase === 'accepted' || task.phase === 'evidence_changed') return 'Received and reviewed';
    if (task.phase === 'awaiting_review') return 'Received; awaiting review';
    if (task.phase === 'review_blocked') return 'Review needs follow-up';
    if (task.phase === 'closed') return 'Attempt closed';
    if (task.phase === 'execution_unconfirmed') return 'Start not confirmed';
    return task.dispatch ? named(task.dispatch, {reserved:'Send reserved', uncertain:'Send uncertain',
      submitted:'Sent; start unconfirmed', running:'Start confirmed', not_sent:'Not sent', terminated:'Stopped'}) :
      'No send recorded';
  }
  function detailLine(root, label, value) { add(root, 'p', '', label + ': ' + (value ?? 'not recorded')); }
  // Auto refresh rebuilds every list; remember what the reader opened so it stays open.
  const openKeys = new Set(), resultCache = new Map();
  function technical(root, label, key) {
    const details = add(root, 'details', 'technical'); add(details, 'summary', '', label);
    if (key) { details.open = openKeys.has(key);
      details.addEventListener('toggle', () => details.open ? openKeys.add(key) : openKeys.delete(key)); }
    return details;
  }
  function taskName(data, taskId) { return (data.task_details[taskId] || {}).title || taskId; }
  function ago(seconds) {
    const m = Math.floor(seconds / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (m < 1) return 'Just now';
    if (h < 1) return m + ' min ago';
    if (d < 1) return h + ' h ' + (m % 60) + ' min ago';
    return d + ' d ' + (h % 24) + ' h ago';
  }
  function warningInfo(code, task) {
    const finished = task && ['accepted', 'closed'].includes(task.phase);
    if (code === 'no_monitor_observation') return finished ? {
      label:'No live check saved', title:'No monitoring history saved', tone:'info',
      explanation:'The listed work has finished, but the ledger has no saved live monitoring check. This does not mean a worker failed.',
      next:'No action is needed unless you are investigating the task history.'
    } : {
      label:'No live check yet', title:'No monitoring update yet', tone:'info',
      explanation:'The ledger has no saved monitoring check for this task. A missing check alone does not mean the worker stopped.',
      next:'If you need a current status, use Check workers now before taking action.'
    };
    if (code === 'observation_stale') return {
      label:'Live check is old', title:'Last monitoring update is old', tone:finished ? 'info' : 'check',
      explanation:'The latest saved check is older than 15 minutes. It is a past observation, not proof of the worker\'s current state.',
      next:finished ? 'No action is needed unless you are reviewing this task\'s history.' :
        'Use Check workers now for a current check, then review the task\'s monitor record if the status still looks wrong.'
    };
    if (code === 'invalid_observation_timestamp') return {
      label:'Check time unavailable', title:'Monitoring time cannot be read', tone:'check',
      explanation:'The saved check has an invalid time, so the dashboard cannot tell how recent it is.',
      next:'Inspect the task\'s monitor record before relying on its age.'
    };
    if (code === 'notification_uncertain_no_resend') return {
      label:'Reviewer notice uncertain', title:'Reviewer notification may have arrived', tone:'check',
      explanation:'The send result is uncertain. Sending it again could create a duplicate message.',
      next:'Check the reviewer inbox and delivery evidence first. Do not resend blindly.'
    };
    return {label:'Status needs inspection', title:'Unrecognized status note', tone:'check',
      explanation:'The ledger contains a status note this dashboard cannot explain yet.',
      next:'Inspect the task details or ledger before taking action.', technical:String(code)};
  }
  function alertCard(root, taskIds, info) {
    const card = add(root, 'article', 'alert-item ' + info.tone);
    const head = add(card, 'div', 'alert-head');
    add(head, 'strong', taskIds.length === 1 ? 'raw' : '', taskIds.length === 1 ? taskName(lastData, taskIds[0]) : taskIds.length + ' tasks');
    add(head, 'span', 'badge', info.tone === 'info' ? 'For reference' : 'Check needed');
    add(card, 'h3', '', info.title);
    if (taskIds.length > 1) add(card, 'p', 'alert-technical', 'Tasks: ' + taskIds.map(x => taskName(lastData, x)).join('; '));
    add(card, 'p', '', info.explanation);
    add(card, 'p', 'alert-next', 'Next: ' + info.next);
    if (info.technical) add(card, 'p', 'alert-technical', 'Ledger detail: ' + info.technical);
  }
  function roles(data) {
    const root = id('roles'); root.replaceChildren();
    for (const role of data.roles) {
      const card = add(root, 'article', 'role'); const top = add(card, 'div', 'role-top');
      add(top, 'span', 'role-mark', role.role.slice(0, 1)); add(top, 'h3', 'raw', role.role);
      const current = role.bindings.find(b => b.status === 'active') || role.bindings[0];
      const purpose = {worker:'Runs assigned tasks', controller:'Coordinates tasks and next steps',
        reviewer:'Checks delivered work'};
      if (current) add(top, 'span', 'badge ' + current.status,
        current.status === 'active' ? 'Assigned' : 'Setup pending');
      add(card, 'p', 'role-summary', current ?
        (purpose[current.mode] || 'Assigned workflow role') : 'No active agent connection');
      add(card, 'p', 'role-tasks', role.tasks.length ?
        role.tasks.length + ' open task' + (role.tasks.length === 1 ? '' : 's') + ': ' +
          role.tasks.map(t => taskName(data, t.task_id)).join('; ') : 'No open tasks');
      if (role.bindings.length) {
        const extra = technical(card, 'Connection details', 'conn:' + role.role);
        for (const b of role.bindings) {
          const binding = add(extra, 'div', 'binding');
          if (role.bindings.length > 1) add(binding, 'span', 'badge ' + b.status, named(b.status, {active:'Assigned', pending:'Setup pending'}));
          const meta = add(binding, 'div', 'role-meta');
          for (const [key, value] of [['Mode', b.mode], ['Harness', b.harness], ['Adapter', b.adapter],
            ['Version', b.version], ['Context policy', b.context_policy], ['Instance', b.instance_id], ['Bound', b.bound_at]]) {
            add(meta, 'span', 'meta-key', key);
            const shown = key === 'Bound' && value && !Number.isNaN(new Date(value).getTime()) ?
              new Date(value).toLocaleString() : value;
            const cell = add(meta, 'span', 'meta-value', shown ?? 'unknown');
            if (key === 'Bound' && value) cell.title = value;
          }
        }
      }
    }
  }
  function resultFiles(root, data) {
    root.replaceChildren();
    const kinds = {binding:'Handoff file', artifact:'Result file', review:'Review file'};
    for (const f of data.files) {
      const box = add(root, 'div', 'result-file');
      add(box, 'strong', '', (kinds[f.kind] || 'File') + ': ' + f.name);
      if (f.error) { add(box, 'p', 'error', f.error); continue; }
      if (!f.exists) { add(box, 'p', 'muted', 'Not written yet'); continue; }
      const notes = [f.bytes + ' bytes'];
      if (f.matches_receipt === true) notes.push('matches the completion receipt');
      if (f.matches_receipt === false) notes.push('changed since the completion receipt');
      if (f.truncated) notes.push('showing the first part only');
      add(box, 'p', f.matches_receipt === false ? 'error' : 'muted', notes.join('; '));
      add(box, 'pre', '', f.text);
    }
  }
  function taskTable(data) {
    const root = id('tasks'); root.replaceChildren();
    if (!data.tasks.length) { const tr = add(root, 'tr'); const td = add(tr, 'td', 'muted', 'No tasks yet'); td.colSpan = 8; return; }
    // Newest last check first; tasks never checked use their latest ledger entry instead.
    const now = Date.now();
    const seen = t => { if (t.observation_age_seconds !== null) return now - t.observation_age_seconds * 1000;
      const journal = (data.task_details[t.task_id] || {}).journal || [];
      const at = journal.length ? Date.parse(journal[journal.length - 1].at) : NaN;
      return Number.isNaN(at) ? 0 : at; };
    const ordered = [...data.tasks].sort((a, b) => seen(b) - seen(a) || a.task_id.localeCompare(b.task_id));
    for (const t of ordered) {
      const tr = add(root, 'tr', 'task-row');
      const info = data.task_details[t.task_id] || {};
      const name = add(tr, 'td', 'task-name'); add(name, 'span', 'task-title', info.title || t.task_id);
      if (info.excerpt) add(name, 'span', 'task-excerpt', info.excerpt);
      if (info.title) add(name, 'span', 'task-id', t.task_id);
      const phase = add(tr, 'td');
      add(phase, 'span', 'badge ' + t.phase, named(t.phase, stageNames));
      add(tr, 'td', '', t.worker_role);
      add(tr, 'td', '', deliveryText(t));
      add(tr, 'td', '', t.review_verdicts.map(v => named(v, reviewNames)).join(', ') || 'Not reviewed');
      add(tr, 'td', '', t.next_role + ': ' + named(t.next_action, nextNames));
      const age = t.observation_age_seconds;
      const check = add(tr, 'td', '', age === null ? 'No saved check' : ago(age));
      if (age !== null) check.title = 'Checked ' + new Date(now - age * 1000).toLocaleString();
      const notes = t.warnings.map(code => warningInfo(code, t));
      add(tr, 'td', notes.length && notes.every(note => note.tone === 'info') ? 'info-note' : '',
        notes.map(note => note.label).join('; ') || 'No notes');
      [...tr.cells].forEach((cell, index) => { cell.dataset.label =
        ['Task', 'Stage', 'Assigned to', 'Delivery', 'Review', 'Next step', 'Last check', 'Note'][index]; });
      const detailRow = add(root, 'tr', 'detail-row' + (openKeys.has('row:' + t.task_id) ? '' : ' hidden'));
      const td = add(detailRow, 'td'); td.colSpan = 8;
      const detail = add(td, 'div', 'detail');
      if (t.dependencies?.length) detailLine(detail, 'Starts after', t.dependencies.map(d =>
        taskName(data, data.dags[t.run]?.tasks?.[d]?.task_id || d)).join('; '));
      if (t.blocking_reason) detailLine(detail, 'Why it could not start', t.blocking_reason);
      for (const code of t.warnings) {
        const note = warningInfo(code, t), box = add(detail, 'div', 'detail-note');
        add(box, 'strong', '', note.title);
        add(box, 'p', '', note.explanation);
        add(box, 'p', 'alert-next', 'Next: ' + note.next);
        if (note.technical) add(box, 'p', 'muted', 'Ledger detail: ' + note.technical);
      }
      const results = add(detail, 'div', 'results');
      const view = add(results, 'button', '', 'View result files'); view.type = 'button';
      const output = add(results, 'div');
      if (resultCache.has(t.task_id)) resultFiles(output, resultCache.get(t.task_id));
      view.addEventListener('click', async event => {
        event.stopPropagation(); view.disabled = true; empty(output, 'Reading result files...');
        try { const files = await api('/api/artifacts?task=' + encodeURIComponent(t.task_id));
          resultCache.set(t.task_id, files); resultFiles(output, files); }
        catch (error) { empty(output, error.message); } finally { view.disabled = false; }
      });
      const extra = technical(detail, 'Technical record', 'record:' + t.task_id);
      detailLine(extra, 'Task ID', t.task_id);
      detailLine(extra, 'Phase', t.phase); detailLine(extra, 'Harness / adapter', t.harness + ' / ' + t.adapter);
      detailLine(extra, 'Dispatch', t.dispatch); detailLine(extra, 'Resource reservation', t.claim_held ? 'held' : 'none');
      detailLine(extra, 'Review verdicts', t.review_verdicts.join(', ') || 'none');
      detailLine(extra, 'Next action', t.next_action); detailLine(extra, 'Working folder', info.cwd || 'unknown');
      const journal = (info.journal || []).map(j => [j.at, j.kind, j.actor].join(' | ')).join('\n');
      add(extra, 'pre', '', journal || 'No journal entries');
      tr.addEventListener('click', () => { const key = 'row:' + t.task_id;
        detailRow.classList.toggle('hidden') ? openKeys.delete(key) : openKeys.add(key); });
    }
  }
  const svgEl = tag => document.createElementNS('http://www.w3.org/2000/svg', tag);
  function fitSvgText(node, value) {
    const chars = Array.from(value);
    node.textContent = value;
    while (node.getComputedTextLength() > 150 && chars.length) {
      chars.pop(); node.textContent = chars.join('') + '...';
    }
  }
  function dagGraph(data) {
    const root = id('dags'); root.replaceChildren();
    const runs = Object.entries(data.dags);
    for (const [run, dag] of runs) drawGraph(root, run, dag.nodes, dag.dependencies,
      name => taskName(data, dag.tasks[name]?.task_id || name));
    // Standalone follow-ups (parent_task) form chains even without a loaded DAG plan.
    const phase = Object.fromEntries(data.tasks.map(t => [t.task_id, t.phase]));
    const linked = new Set();
    for (const [task, info] of Object.entries(data.task_details))
      if (info.parent_task && phase[task] && phase[info.parent_task]) { linked.add(task); linked.add(info.parent_task); }
    if (linked.size) {
      const nodes = {}, deps = {};
      for (const task of linked) { nodes[task] = phase[task];
        const parent = data.task_details[task].parent_task; deps[task] = linked.has(parent) ? [parent] : []; }
      drawGraph(root, 'Follow-up chains', nodes, deps, name => taskName(data, name));
    }
    if (!root.children.length) empty(root, 'No task plans or follow-up chains yet');
  }
  function drawGraph(root, run, nodes, dependencies, label) {
    {
      const dag = {nodes, dependencies};
      const box = add(root, 'div', 'dag-run'); const title = add(box, 'div', 'dag-title');
      add(title, 'h3', '', run); add(title, 'span', 'dag-count', Object.keys(dag.nodes).length + ' tasks');
      const levels = {}, visiting = new Set();
      const level = name => { if (levels[name] !== undefined) return levels[name];
        if (visiting.has(name)) return 0; visiting.add(name);
        const deps = dag.dependencies[name] || [];
        const n = deps.length ? 1 + Math.max(...deps.map(level)) : 0;
        visiting.delete(name); levels[name] = n; return n; };
      Object.keys(dag.nodes).forEach(level);
      const groups = []; for (const name of Object.keys(dag.nodes)) (groups[levels[name]] ||= []).push(name);
      const width = Math.max(300, groups.length * 230 + 30);
      const height = Math.max(130, Math.max(...groups.map(g => g.length)) * 105 + 24);
      const wrap = add(box, 'div', 'dag-scroll'); const svg = svgEl('svg');
      svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height); svg.setAttribute('role', 'img');
      svg.style.width = width + 'px'; svg.style.height = height + 'px';
      svg.setAttribute('aria-label', 'Dependency graph for ' + run); wrap.append(svg);
      const pos = {}; groups.forEach((group, col) => group.forEach((name, row) => { pos[name] = [24 + col * 230, 18 + row * 105]; }));
      for (const [name, deps] of Object.entries(dag.dependencies)) for (const dep of deps) {
        if (!pos[name] || !pos[dep]) continue;
        const line = svgEl('line'); line.setAttribute('x1', pos[dep][0] + 175); line.setAttribute('y1', pos[dep][1] + 31);
        line.setAttribute('x2', pos[name][0]); line.setAttribute('y2', pos[name][1] + 31); svg.append(line);
      }
      for (const [name, state] of Object.entries(dag.nodes)) {
        const [x,y] = pos[name]; const rect = svgEl('rect'); rect.setAttribute('x', x); rect.setAttribute('y', y);
        rect.setAttribute('width', 175); rect.setAttribute('height', 64); rect.setAttribute('rx', 10);
        rect.setAttribute('class', state); svg.append(rect);
        const text = label(name);
        const caption = svgEl('text'); caption.setAttribute('x', x + 12); caption.setAttribute('y', y + 26);
        caption.setAttribute('translate', 'no'); svg.append(caption); fitSvgText(caption, text);
        const sub = svgEl('text'); sub.setAttribute('x', x + 12); sub.setAttribute('y', y + 49);
        sub.setAttribute('translate', 'no'); svg.append(sub);
        fitSvgText(sub, tr(named(state, {...stageNames, ...dagNames})));
        const tip = svgEl('title'); tip.textContent = text + ' (' + name + '): ' + tr(named(state, stageNames)); rect.append(tip);
      }
    }
  }
  function lists(data) {
    const inbox = id('inbox'); inbox.replaceChildren();
    if (!data.inbox.length) empty(inbox, 'No completion updates yet');
    else for (const e of data.inbox) {
      const item = add(inbox, 'article', 'event-item'); const head = add(item, 'div', 'event-head');
      add(head, 'strong', 'raw', taskName(data, e.task_id)); add(head, 'span', 'badge ' + e.status, named(e.status, stageNames));
      add(item, 'p', 'role-summary', named(e.report_status, reportNames));
      if (e.status === 'pending') add(item, 'p', 'role-summary', 'Reviewer: open this update and acknowledge it before reviewing.');
      else if (e.status === 'acknowledged') add(item, 'p', 'role-summary', 'Reviewer: inspect the evidence and record a review.');
      if (data.allow_dispatch && e.role === data.actor_role && ['pending','acknowledged'].includes(e.status)) {
        const open = add(item, 'button', '', e.status === 'pending' ? 'Acknowledge...' : 'Record review...');
        open.type = 'button'; open.addEventListener('click', () => openReview(e, data));
      }
      if (e.created_at) {
        const received = new Date(e.created_at);
        add(item, 'small', '', 'Received ' + (Number.isNaN(received.getTime()) ? e.created_at : received.toLocaleString()));
      }
      const extra = technical(item, 'Event details', 'event:' + e.event); detailLine(extra, 'Event ID', e.event_id);
      detailLine(extra, 'Inbox status', e.status); detailLine(extra, 'Report status', e.report_status);
    }
    const alerts = id('alerts'); alerts.replaceChildren();
    if (!data.alerts.length && !Object.keys(data.scheduling_blocks).length)
      empty(alerts, 'Nothing needs attention right now.');
    else {
      const tasks = new Map(data.tasks.map(task => [task.task_id, task]));
      const groups = new Map();
      for (const a of data.alerts) for (const code of a.warnings) {
        const info = warningInfo(code, tasks.get(a.task_id));
        const key = code + '|' + info.next;
        if (!groups.has(key)) groups.set(key, {ids:[], info});
        groups.get(key).ids.push(a.task_id);
      }
      for (const group of groups.values()) alertCard(alerts, group.ids, group.info);
      for (const [task, block] of Object.entries(data.scheduling_blocks))
        alertCard(alerts, [task], {tone:'check', title:'Task could not start',
          explanation:'The scheduler has paused this task before dispatch.',
          next:'Read the scheduler detail and resolve the condition before the next scheduling pass.',
          technical:block.reason || 'No reason recorded'});
    }
    const resources = id('resources'); resources.replaceChildren();
    const caps = data.resource_capacities;
    if (!Object.keys(caps).length) add(resources, 'p', 'muted', 'No named resource limits are set for this workflow.');
    for (const [name, cap] of Object.entries(caps)) {
      const used = data.claims.reduce((sum,c) => sum + (c.resources[name] || 0), 0);
      const row = add(resources, 'div', 'meter'); const bar = add(row, 'div'); add(bar, 'strong', '', name);
      const meter = add(bar, 'progress'); meter.max = cap; meter.value = used;
      add(row, 'strong', '', used + ' of ' + cap + ' reserved');
    }
    add(resources, 'p', 'muted', data.claims.length ?
      data.claims.length + ' active task reservation' + (data.claims.length === 1 ? '' : 's') :
      'No active task reservations');
  }
  function workers(data) {
    const select = id('worker-role'), selected = select.value;
    const names = data.roles.filter(role => role.bindings.some(b => b.status === 'active' && b.mode === 'worker'))
      .map(role => role.role);
    // Rebuilding options closes an open dropdown; only touch it when the choices change.
    if (names.join('\n') === [...select.options].map(x => x.value).join('\n')) return;
    select.replaceChildren();
    for (const name of names) { const option = el('option', '', name); option.value = name; select.append(option); }
    if ([...select.options].some(x => x.value === selected)) select.value = selected;
  }
  function picker(data, open) {
    show('picker', open); show('main', !open && data.state !== null);
    show('overview', !open && data.state !== null);
    show('switch', data.can_switch && data.state !== null); show('probe', data.state !== null);
    id('switch').textContent = open && data.state !== null ? 'Cancel' : 'Choose workflow';
    if (!open) return;
    const root = id('recent'); root.replaceChildren();
    if (!data.recent.length) empty(root, 'No recent workflows');
    for (const path of data.recent) {
      const button = add(root, 'button', 'recent-item', path); button.type = 'button';
      if (path === data.state) button.disabled = true;
      button.addEventListener('click', () => openState(path));
    }
  }
  let picking = false;
  async function openState(path) {
    id('picker-error').textContent = '';
    try { await api('/api/state', {state: path}); picking = false; id('state-input').value = ''; await refresh(); }
    catch (error) { id('picker-error').textContent = error.message; }
  }
  function chatRole() { return id('chat-role').value; }
  function renderChat(role) {
    const session = chatSession(role), log = id('chat-log'), out = id('chat-output');
    id('chat-input').value = session.draft;
    id('chat-status').textContent = session.status;
    log.replaceChildren();
    for (const entry of session.entries) {
      const row = add(log, 'div', 'list-item chat-entry');
      add(row, 'small', '', entry.when + ' to ' + role + (entry.uncertain ? ' - delivery uncertain' : ''));
      add(row, 'p', 'raw', entry.message);
    }
    show('chat-output', session.output !== null);
    out.textContent = session.output || '';
    updateChatBusy(lastData);
  }
  function updateChatBusy(data) {
    const role = chatRole();
    const busy = (data?.tasks || []).find(t => t.worker_role === role &&
      (t.claim_held || (['executing', 'execution_unconfirmed'].includes(t.phase) &&
        ['reserved', 'uncertain', 'submitted', 'running'].includes(t.dispatch))));
    const visible = !!busy;
    const key = busy ? role + ':' + busy.task_id : '';
    if (key !== chatBusyKey) id('chat-confirm-busy').checked = false;
    chatBusyKey = key;
    id('chat-busy').textContent = busy ?
      'This role is working on ' + taskName(data, busy.task_id) + '. A message may interrupt its work.' : '';
    show('chat-busy', visible); show('chat-busy-confirm-row', visible);
  }
  function chatTargets(data) {
    const select = id('chat-role'), selected = select.value;
    const targets = data.chat_targets || [];
    const next = targets.map(x => x.role);
    const current = [...select.options].map(x => x.value);
    if (JSON.stringify(current) !== JSON.stringify(next)) {
      select.replaceChildren();
      for (const target of targets) { const option = add(select, 'option', '', target.role);
        option.value = target.role; option.setAttribute('translate', 'no'); }
      select.value = next.includes(selected) ? selected :
        (next.includes(data.actor_role) ? data.actor_role : next[0] || '');
      renderChat(select.value);
    } else updateChatBusy(data);
  }
  async function refresh() {
    try { const data = await api('/api/status'); lastData = data; id('error').textContent = '';
      picker(data, picking || data.state === null);
      if (data.state === null) { id('state').textContent = 'No workflow open'; id('goal').textContent = '';
        id('updated').textContent = 'Choose a workflow data folder to begin'; return; }
      id('state').textContent = data.state;
      id('goal').textContent = data.goal ? 'Goal: ' + data.goal : 'No goal description set';
      id('updated').textContent = 'Page updated: ' + new Date().toLocaleTimeString();
      metrics(data); roles(data); taskTable(data); dagGraph(data); lists(data); workers(data);
      show('dispatch-form', data.allow_dispatch);
      show('chat-panel', data.allow_chat); show('chat-open', data.allow_chat);
      if (data.allow_chat) { chatTargets(data); id('chat-caption').textContent = 'Talking to ' + chatRole(); }
      id('dispatch-note').textContent = data.allow_dispatch ?
        'Preview checks that the worker is available. Confirm sends the task once; check the result before taking another action.' :
        'Starting tasks is off for this dashboard session. To enable it, restart the server with --allow-dispatch and the required coordinator identity and task-folder options.';
    } catch (error) { id('error').textContent = error.message; }
  }
  function form(includeNonce) {
    let resources; try { resources = JSON.parse(id('resource-input').value); } catch { throw new Error('Resources must be JSON'); }
    const value = {worker_role:id('worker-role').value, task_id:id('task-id').value, body:id('body').value,
      source:id('source').value, binding_artifact:id('binding').value,
      artifacts:id('artifacts').value.split(',').map(x => x.trim()),
      notify_reviewer:id('notify').checked, resources};
    if (includeNonce) value.nonce = id('nonce').value;
    return value;
  }
  function invalidate() { preview = null; id('confirm').disabled = true; show('preview-output', false); }
  for (const name of ['worker-role','task-id','body','source','binding','artifacts','resource-input','notify'])
    id(name).addEventListener('input', invalidate);
  id('preview').addEventListener('click', async () => {
    invalidate(); id('dispatch-error').textContent = ''; show('dispatch-output', false);
    id('preview').disabled = true;
    try { preview = await api('/api/dispatch/preview', form(false)); id('nonce').value = preview.form.nonce;
      id('preview-request').textContent = JSON.stringify(preview.request, null, 2);
      id('preview-message').textContent = preview.message; show('preview-output', true);
      id('confirm').disabled = false;
    } catch (error) { id('dispatch-error').textContent = error.message; }
    finally { id('preview').disabled = false; }
  });
  id('confirm').addEventListener('click', async () => {
    if (!preview) return; const digest = preview.preview_sha256;
    id('confirm').disabled = true; id('dispatch-error').textContent = '';
    try { const result = await api('/api/dispatch/confirm', {...form(true), preview_sha256:digest});
      id('dispatch-status').textContent = result.status === 'SUBMITTED' ?
        'Task sent. Worker execution still needs independent confirmation.' :
        'Send result uncertain. Inspect the task record before taking another action.';
      id('dispatch-status').className = result.status === 'SUBMITTED' ? 'success' : 'error';
      for (const key of ['execution-template','reconcile-command','monitor-command']) id(key).textContent = result[key];
      show('dispatch-output', true); await refresh();
    } catch (error) { id('dispatch-error').textContent = error.message + ' Inspect the ledger before trying another task.'; }
    preview = null;
  });
  let reviewEvent = null, reviewPreview = null;
  function reviewInvalidate() { reviewPreview = null; id('review-confirm').disabled = true; show('review-output', false); }
  function openReview(e, data) {
    reviewEvent = e; reviewInvalidate(); id('review-error').textContent = ''; id('review-status').textContent = '';
    id('review-target').textContent = (e.status === 'pending' ? 'Acknowledge the update for ' :
      'Record a review for ') + taskName(data, e.task_id) + ' (' + e.task_id + ').';
    const select = id('review-verdict'); select.replaceChildren();
    for (const v of data.verdicts) { const option = add(select, 'option', '', named(v, reviewNames)); option.value = v; }
    show('review-verdict-row', e.status === 'acknowledged');
    show('review-panel', true); show('review-form', true);
    id('review-panel').scrollIntoView({behavior:'smooth', block:'start'});
  }
  id('review-verdict').addEventListener('input', reviewInvalidate);
  id('review-preview').addEventListener('click', async () => {
    if (!reviewEvent) return; reviewInvalidate(); id('review-error').textContent = '';
    id('review-preview').disabled = true;
    const payload = {event_id: reviewEvent.event};
    if (reviewEvent.status === 'acknowledged') payload.verdict = id('review-verdict').value;
    try { reviewPreview = await api('/api/review/preview', payload);
      id('review-summary').textContent = reviewPreview.step === 'acknowledge' ?
        'Confirm marks this update as received by ' + reviewPreview.actor + '. It does not accept the work.' :
        'Confirm records ' + named(reviewPreview.verdict, reviewNames) + ' (next: ' + reviewPreview.action +
        ') from ' + reviewPreview.review_path + ' (sha256 ' + reviewPreview.review_sha256.slice(0, 12) + ').';
      id('review-text').textContent = reviewPreview.review_text || '';
      show('review-output', true); id('review-confirm').disabled = false;
    } catch (error) { id('review-error').textContent = error.message; }
    finally { id('review-preview').disabled = false; }
  });
  id('review-confirm').addEventListener('click', async () => {
    if (!reviewPreview) return; id('review-confirm').disabled = true; id('review-error').textContent = '';
    const payload = {event_id: reviewPreview.event_id, preview_sha256: reviewPreview.preview_sha256};
    if (reviewPreview.verdict) payload.verdict = reviewPreview.verdict;
    try { const result = await api('/api/review/confirm', payload);
      id('review-status').textContent = result.status === 'ACKNOWLEDGED' ?
        'Acknowledged. Write the review file, then record the review.' :
        'Review recorded: ' + named(result.verdict, reviewNames) + '.';
      id('review-status').className = 'success'; show('review-form', true); reviewEvent = null;
      show('review-output', false); await refresh();
    } catch (error) { id('review-error').textContent = error.message; }
    reviewPreview = null;
  });
  id('probe').addEventListener('click', async () => {
    id('probe').disabled = true; const root = id('probe-result'); empty(root, 'Checking active workers...');
    id('probe-time').textContent = ''; id('probe-panel').scrollIntoView({behavior:'smooth', block:'start'});
    try { const data = await api('/api/probe', {}); root.replaceChildren();
      id('probe-time').textContent = 'Checked at ' + new Date().toLocaleTimeString() +
        '. Shows live worker status only; tasks appear above only when dispatched through this workflow ledger.';
      if (!data.workers.length) empty(root, 'No active workers to check');
      for (const w of data.workers) {
        const item = add(root, 'div', 'list-item');
        add(item, 'strong', '', w.role + ': ' + (w.error ? 'Could not check worker' :
          w.identity_matches === false ? 'Worker identity changed; inspect before acting' :
          named(w.status, {idle:'Available', working:'Working', blocked:'Needs help',
            failed:'Failed', exited:'Exited', unknown:'Status unknown'})));
        if (w.error) add(item, 'p', 'error', w.error);
        const extra = technical(item, 'Check details');
        detailLine(extra, 'Status', w.status); detailLine(extra, 'Completion sequence', w.completion_seq);
        detailLine(extra, 'Identity matches', w.identity_matches);
      }
    } catch (error) { empty(root, error.message); } finally { id('probe').disabled = false; }
  });
  id('probe-top').addEventListener('click', () => window.scrollTo({top:0, behavior:'smooth'}));
  async function chatRead(role = chatRole()) {
    if (!role) return; id('chat-read').disabled = true;
    try { const data = await api('/api/chat/output', {role});
      const session = chatSession(role);
      session.output = (data.truncated ? '[earlier output omitted]\n' : '') + (data.output || '(no output)');
      session.status = data.role + ' is ' + named(data.status, {idle:'idle', working:'working',
        blocked:'blocked', done:'done'}) + ' (read ' + new Date().toLocaleTimeString() + ')';
      if (chatRole() === role) { renderChat(role); id('chat-output').scrollTop = id('chat-output').scrollHeight;
        id('chat-status').className = 'muted'; }
    } catch (error) { if (chatRole() === role) { id('chat-status').textContent = error.message;
      id('chat-status').className = 'error'; } }
    finally { id('chat-read').disabled = false; }
  }
  async function chatSend() {
    const role = chatRole(), text = id('chat-input').value.trim(); if (!role || !text) return;
    const confirmBusy = id('chat-confirm-busy').checked;
    if (!id('chat-busy').classList.contains('hidden') && !confirmBusy) {
      id('chat-status').textContent = 'Confirm the interruption warning before sending to this busy worker.';
      id('chat-status').className = 'error'; return;
    }
    id('chat-send').disabled = true; id('chat-status').textContent = 'Sending...'; id('chat-status').className = 'muted';
    try { const result = await api('/api/chat/send', {role, message:text, confirm_busy:confirmBusy});
      const session = chatSession(role);
      session.entries.push({when:new Date().toLocaleTimeString(), message:text,
        uncertain:result.status !== 'SENT'});
      if (result.status === 'SENT') { session.draft = '';
        session.status = 'Sent. Reading the reply shortly...'; setTimeout(() => chatRead(role), 3000); }
      else session.status = 'Delivery uncertain: ' + (result.error || 'unknown') +
        '. Read the output before sending again.';
      if (chatRole() === role) { renderChat(role); id('chat-status').className =
        result.status === 'SENT' ? 'muted' : 'error'; }
    } catch (error) { if (chatRole() === role) { id('chat-status').textContent = error.message;
      id('chat-status').className = 'error'; } }
    finally { id('chat-send').disabled = false; }
  }
  id('chat-send').addEventListener('click', chatSend);
  id('chat-read').addEventListener('click', () => chatRead());
  id('chat-input').addEventListener('input', () => { if (chatRole()) chatSession(chatRole()).draft = id('chat-input').value; });
  id('chat-role').addEventListener('change', () => {
    id('chat-confirm-busy').checked = false; renderChat(chatRole());
    id('chat-caption').textContent = 'Talking to ' + chatRole();
  });
  id('chat-input').addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); chatSend(); } });
  id('chat-open').addEventListener('click', () => { id('chat-panel').scrollIntoView({behavior:'smooth', block:'start'});
    id('chat-input').focus(); });
  id('probe-clear').addEventListener('click', () => { id('probe-result').replaceChildren(); id('probe-time').textContent = ''; });
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    await navigator.clipboard.writeText(id(button.dataset.copy).textContent); button.textContent = 'Copied';
  }));
  id('switch').addEventListener('click', () => { picking = !picking; if (lastData) picker(lastData, picking); });
  id('open-state').addEventListener('click', () => openState(id('state-input').value));
  id('state-input').addEventListener('keydown', event => { if (event.key === 'Enter') openState(id('state-input').value); });
  id('refresh').addEventListener('click', refresh);
  setInterval(() => { if (id('auto').checked) refresh(); }, 5000);
  id('lang').addEventListener('click', () => { setLang(lang === 'zh' ? 'en' : 'zh'); if (lastData) refresh(); });
  setLang(lang);
  refresh();
})();
</script>
</body>
</html>"""


def _digest(request, message, resources):
    # Resources are scheduler arguments, not request fields, but still part of the frozen preview.
    frozen = json.dumps({"request": request, "resources": resources}, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((frozen + "\n" + message).encode("utf-8")).hexdigest()


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _artifact_path(value, cwd):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError("delivery path must be a nonempty file path")
    path = Path(value)
    if path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError("delivery path must stay inside the task directory")
    resolved = (cwd / path).resolve()
    if resolved == cwd.resolve():
        raise ValueError("delivery path must name a file")
    resolved.relative_to(cwd.resolve())
    return value


def _form(payload, nonce=None):
    if not isinstance(payload, dict):
        raise ValueError("form must be a JSON object")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or task_id in (".", ".."):
        raise ValueError("invalid task ID")
    nonce = secrets.token_hex(8) if nonce is None else nonce
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise ValueError("invalid nonce")
    worker_role = payload.get("worker_role")
    if not isinstance(worker_role, str) or not worker_role:
        raise ValueError("select a worker role")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > 20000:
        raise ValueError("task body must contain 1 to 20000 characters")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip() or len(source) > 2000:
        raise ValueError("authorization source is required")
    notify = payload.get("notify_reviewer", False)
    if type(notify) is not bool:
        raise ValueError("notify_reviewer must be boolean")
    binding = payload.get("binding_artifact", "HANDOFF.md")
    artifacts = payload.get("artifacts", ["RESULT.md"])
    if not isinstance(artifacts, list) or not artifacts or any(not isinstance(x, str) for x in artifacts):
        raise ValueError("artifacts must be a nonempty list of paths")
    resources = payload.get("resources", {})
    if not isinstance(resources, dict) or any(not isinstance(k, str) or not k or type(v) is not int or v < 1
                                               for k, v in resources.items()):
        raise ValueError("resources must map names to positive integer units")
    return {"task_id": task_id, "nonce": nonce, "worker_role": worker_role, "body": body,
            "source": source.strip(), "notify_reviewer": notify, "binding_artifact": binding,
            "artifacts": artifacts, "resources": resources}


def _state_path(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("state path must be a nonempty string")
    # Accept pasted paths with surrounding quotes, and the ledger file itself.
    path = Path(value.strip().strip('"').strip("'")).expanduser()
    if not path.is_absolute():
        raise ValueError("state path must be absolute")
    path = path.resolve()
    if path.name == "workflow.sqlite3" and path.is_file():
        path = path.parent
    if not (path / "workflow.sqlite3").is_file():
        raise ValueError("workflow state is not initialized: " + str(path))
    return path


def _recent(recent_file):
    if recent_file is None:
        return []
    try:
        values = json.loads(Path(recent_file).read_text(encoding="utf-8")).get("states", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [value for value in values if isinstance(value, str)][:RECENT_LIMIT]


def _remember(recent_file, state):
    """Record a state path for the picker; failure only loses the convenience list."""
    if recent_file is None:
        return
    path = Path(recent_file)
    values = [str(state)] + [value for value in _recent(path) if value != str(state)]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
        temporary.write_text(json.dumps({"states": values[:RECENT_LIMIT]}, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR lets a second socket bind an in-use port; keep port conflicts visible there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, state=None, *, allow_dispatch=False, allow_chat=False, actor=None, version=None,
                 context=None, tasks_root=None, transport_factory=Transport, recent_file=None, public_origins=(),
                 lan_tls=None):
        host, port = address
        if lan_tls is None and host not in ("127.0.0.1", "localhost"):
            raise ValueError("dashboard host must be loopback: 127.0.0.1 or localhost")
        if lan_tls is not None and host not in ("127.0.0.1", "localhost"):
            _private_lan_ip(host)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if state is None and allow_dispatch:
            raise ValueError("dispatch needs --state")
        if state is None and allow_chat:
            raise ValueError("chat needs --state")
        self.state = None if state is None else _state_path(str(Path(state).resolve()))
        self.recent_file = recent_file
        self.allow_dispatch = bool(allow_dispatch)
        self.allow_chat = bool(allow_chat)
        self.public_origins = tuple(dict.fromkeys(_public_origin(value) for value in public_origins))
        self.public_hosts = {urlsplit(value).netloc for value in self.public_origins}
        self.lan_tls = lan_tls is not None
        self.actor, self.version, self.context = actor, version, context
        self.tasks_root = None
        self.transport_factory = transport_factory
        self.token = secrets.token_urlsafe(32)
        self.csp_nonce = secrets.token_urlsafe(16)
        self.preview_records = {}
        self.preview_lock = threading.Lock()
        self.review_lock = threading.Lock()
        self.chat_lock = threading.Lock()
        self.actor_role = None
        self.task_locks = {}
        if self.allow_dispatch:
            if not actor or type(version) is not int or version < 1 or context != "main" or tasks_root is None:
                raise ValueError("dispatch needs --actor, --version, --context main, and --tasks-root")
            root = Path(tasks_root)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("tasks root must be an existing absolute directory")
            self.tasks_root = root.resolve()
            with scheduler._read_database(self.state) as con:
                binding = control.guard(con, actor, version, context, "dispatch")
                if binding["mode"] != "controller":
                    raise ValueError("actor is not the active controller")
            self.actor_role = binding["role"]
        if self.allow_chat:
            if not actor or type(version) is not int or version < 1 or context != "main":
                raise ValueError("chat needs --actor, --version, and --context main")
            try:
                self.actor_role = self.chat_binding()["role"]
            except PermissionError as exc:
                raise ValueError(str(exc)) from exc
        super().__init__((host, port), DashboardHandler)
        if lan_tls is not None:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(str(lan_tls[0]), str(lan_tls[1]))
                self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)
            except Exception:
                self.server_close()
                raise
        if self.state is not None:
            _remember(self.recent_file, self.state)

    def get_request(self):
        client, address = super().get_request()
        if self.lan_tls:
            client.settimeout(5)
        return client, address

    def handle_error(self, request, client_address):
        if self.lan_tls and isinstance(sys.exc_info()[1], (ssl.SSLError, TimeoutError,
                                                            ConnectionResetError, BrokenPipeError)):
            return  # Bad handshakes and interrupted clients must not disrupt later requests.
        super().handle_error(request, client_address)

    def chat_binding(self):
        """The active controller binding named at startup; rechecked before every chat call."""
        with scheduler._read_database(self.state) as con:
            row = con.execute("SELECT data FROM bindings WHERE id=? AND version=? AND status='active'",
                              (self.actor, self.version)).fetchone()
        if not row:
            raise PermissionError("stale or unregistered controller binding")
        binding = json.loads(row[0])
        if binding["mode"] != "controller":
            raise PermissionError("chat actor is not the active controller")
        return binding

    def chat_target(self, role, *, sending=False):
        sender = self.chat_binding()
        if role is None:
            role = sender["role"]  # Keep the original controller-only API payload usable.
        if not isinstance(role, str) or not role or len(role) > 128:
            raise ValueError("choose an active conversation role")
        with scheduler._read_database(self.state) as con:
            row = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
        if not row:
            raise ValueError("conversation role has no active binding: " + role)
        binding = json.loads(row[0])
        spec = binding["spec"]
        if sending and spec["adapter"] == "receipt":
            raise ValueError("this receipt adapter cannot send messages")
        if sending and spec["adapter"] == "command" and not spec.get("prompt_argv"):
            raise ValueError("this command adapter has no prompt command")
        transport = self.transport_factory({"monitor": {"command_timeout_seconds": 15}})
        observed = transport.status(spec)
        if not identity_matches(observed.get("identity", {}), spec["expected_identity"]):
            raise ValueError("conversation target identity changed; rebind before chatting")
        if sending and spec["adapter"] == "herdr":
            Transport.check_prompt_available(observed)
        return binding, spec, transport, observed

    def chat_output(self, payload=None):
        role = payload.get("role") if isinstance(payload, dict) else None
        binding, spec, transport, observed = self.chat_target(role)
        text = transport.output(spec)
        return {"role": binding["role"], "status": observed.get("status"),
                "output": text[-CHAT_OUTPUT_LIMIT:], "truncated": len(text) > CHAT_OUTPUT_LIMIT}

    def chat_busy(self, role):
        raw = scheduler.snapshot(self.state)
        claims = {item["task_id"] for item in raw["claims"]}
        for item in raw["tasks"]:
            if item["worker"]["role"] != role or item["execution"] == "resolved":
                continue
            dispatch = (item.get("dispatch") or {}).get("status")
            if item["task_id"] in claims or (item["execution"] == "running" and
                    dispatch in ("reserved", "uncertain", "submitted", "running")):
                detail = workflow_status.task_summary(item["task_id"], item["cwd"], [])
                return {"task_id": item["task_id"], "title": detail["title"] or item["task_id"]}
        return None

    def chat_send(self, payload):
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message is empty")
        message = message.replace("\r\n", "\n").strip()
        if len(message) > CHAT_MESSAGE_LIMIT or "\x00" in message:
            raise ValueError("message must be at most " + str(CHAT_MESSAGE_LIMIT) + " characters of text")
        role = payload.get("role") if isinstance(payload, dict) else None
        confirm_busy = payload.get("confirm_busy", False) if isinstance(payload, dict) else False
        if type(confirm_busy) is not bool:
            raise ValueError("confirm_busy must be a boolean")
        with self.chat_lock:
            binding, spec, transport, _ = self.chat_target(role, sending=True)
            busy = self.chat_busy(binding["role"]) if binding["mode"] == "worker" else None
            if busy and not confirm_busy:
                raise FileExistsError("This role is working on " + busy["title"] + " (" + busy["task_id"] +
                                      "). A message may interrupt its work. Confirm separately to send.")
            record = {"actor": self.actor, "version": self.version, "role": binding["role"],
                      "target_role": binding["role"], "target_version": binding["version"],
                      "confirm_busy": bool(busy and confirm_busy),
                      "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(), "chars": len(message)}
            try:
                transport.send(spec, message)
                record["status"] = "SENT"
            except Exception as exc:
                # One attempt only: an uncertain send is reported, never retried.
                record.update(status="SEND_UNCERTAIN", error=str(exc)[:300])
            with control.database(self.state) as con:
                control.audit(con, "dashboard_chat", record)
        result = {key: record[key] for key in ("status", "role", "chars")}
        if "error" in record:
            result["error"] = record["error"]
        return result

    def origin(self, host=None):
        return ("https://" if self.lan_tls else "http://") + (host or self.server_address[0] + ":" + str(self.server_port))

    def status_data(self):
        state = self.state
        recent = [value for value in _recent(self.recent_file) if (Path(value) / "workflow.sqlite3").is_file()]
        fixed = self.allow_dispatch or self.allow_chat
        base = {"allow_dispatch": self.allow_dispatch, "allow_chat": self.allow_chat, "can_switch": not fixed,
                "recent": recent, "actor_role": self.actor_role if fixed else None, "chat_targets": []}
        if state is None:
            return dict(base, read_only=True, state=None)
        summary = workflow_status.summarize(state)
        details = workflow_status.dashboard_details(state, summary)
        targets = [{"role": item["role"], "mode": binding["mode"]}
                   for item in details["roles"] for binding in item["bindings"] if binding["status"] == "active"]
        base["chat_targets"] = targets
        return dict(summary, **details, **base, state=str(state))

    def select_state(self, payload):
        # Viewing only: dispatch mode is bound to the controller checked at startup.
        if self.allow_dispatch or self.allow_chat:
            raise PermissionError("state is fixed while dispatch or chat is enabled")
        state = _state_path(payload.get("state"))
        self.state = state
        _remember(self.recent_file, state)
        return {"state": str(state)}

    def artifacts(self, task_id):
        """Read only the delivery files the ledger declares for one task; never writes or polls."""
        if self.state is None:
            raise ValueError("open a workflow state first")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task ID")
        with scheduler._read_database(self.state) as con:
            row = con.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise ValueError("unknown task")
            request = json.loads(row["data"])["request"]
            receipt = {}
            for event in con.execute("SELECT data FROM events WHERE task=? ORDER BY id", (task_id,)):
                receipt = json.loads(event[0]).get("receipt") or receipt
        cwd = Path(request["cwd"]).resolve()
        completion = request["completion"]
        names = [("binding", completion["binding_artifact"])] + [("artifact", x) for x in completion.get("artifacts", [])]
        if request.get("review_path"):
            names.append(("review", request["review_path"]))
        received = {}
        for item in receipt.get("artifacts", []) + [receipt.get("binding_artifact", {})]:
            if item.get("path"):
                received[str(Path(item["path"]).resolve())] = item.get("sha256")
        files = []
        for kind, item in names:
            name = item if isinstance(item, str) else item["path"]
            entry = {"kind": kind, "name": name, "exists": False}
            files.append(entry)
            try:
                path = (cwd / name).resolve()
                path.relative_to(cwd)  # Reject traversal and escaping symlinks.
            except (OSError, ValueError):
                entry["error"] = "path is outside the task directory"
                continue
            entry["path"] = str(path)
            if not path.is_file():
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            entry.update(exists=True, bytes=len(raw), sha256=digest, truncated=len(raw) > ARTIFACT_LIMIT,
                         text=raw[:ARTIFACT_LIMIT].decode("utf-8", errors="replace"))
            if str(path) in received:
                entry["matches_receipt"] = received[str(path)] == digest
        return {"task_id": task_id, "cwd": str(cwd), "files": files}

    def probe(self):
        if self.state is None:
            raise ValueError("open a workflow state first")
        with scheduler._read_database(self.state) as con:
            bindings = [json.loads(row[0]) for row in con.execute(
                "SELECT data FROM bindings WHERE status='active' AND mode='worker' ORDER BY role")]
        result = []
        for binding in bindings:
            spec = binding["spec"]
            try:
                observed = self.transport_factory({"monitor": {"command_timeout_seconds": 10}}).status(spec)
                result.append({"role": binding["role"], "status": observed.get("status"),
                               "completion_seq": observed.get("completion_seq"),
                               "identity_matches": identity_matches(observed.get("identity", {}), spec["expected_identity"])})
            except Exception as exc:
                result.append({"role": binding["role"], "error": str(exc)[:300]})
        return {"workers": result}

    def prepare(self, payload, *, confirm=False):
        if confirm and "nonce" not in payload:
            raise ValueError("confirm requires the nonce returned by preview")
        form = _form(payload, payload.get("nonce") if confirm else None)
        cwd = (self.tasks_root / form["task_id"]).resolve()
        if cwd.parent != self.tasks_root or cwd.exists():
            raise FileExistsError("task directory already exists or is outside tasks root")
        binding_artifact = _artifact_path(form["binding_artifact"], cwd)
        artifacts = [_artifact_path(value, cwd) for value in form["artifacts"]]
        if binding_artifact in artifacts:
            raise ValueError("binding artifact must differ from other artifacts")
        with scheduler._read_database(self.state) as con:
            cfg = control.config_of(con)
            reviewer_role = cfg.get("workflow", {}).get("reviewer_role")
            if not reviewer_role:
                raise ValueError("workflow reviewer_role is required")
            def bound(role):
                row = con.execute("SELECT data FROM bindings WHERE role=? AND status='active'", (role,)).fetchone()
                if not row:
                    raise ValueError("role has no active binding: " + role)
                return json.loads(row[0])
            worker = bound(form["worker_role"])
            reviewer = bound(reviewer_role)
            if worker["mode"] != "worker" or reviewer["mode"] not in ("controller", "reviewer"):
                raise ValueError("selected roles do not hold worker/reviewer modes")
            if worker["spec"]["adapter"] not in ("command", "herdr"):
                raise ValueError("worker adapter cannot accept scheduler dispatch")
        worker_spec = copy.deepcopy(worker["spec"])
        reviewer_spec = copy.deepcopy(reviewer["spec"])
        observed = self.transport_factory({"monitor": {"command_timeout_seconds": 10}}).status(worker_spec)
        if not identity_matches(observed.get("identity", {}), worker_spec["expected_identity"]):
            raise ValueError("worker identity changed")
        baseline = sequence(observed.get("completion_seq"))
        worker_spec["baseline_completion_seq"] = baseline
        marker = "TASK_DONE " + form["task_id"] + " " + form["nonce"]
        request = {"task_id": form["task_id"], "nonce": form["nonce"], "generation": 1,
                   "cwd": str(cwd), "workflow_state": str(self.state), "worker": worker_spec,
                   "reviewer": reviewer_spec, "dispatch_confirmed": False,
                   "authorization": {"source": form["source"], "dispatch_worker": True,
                                     "notify_reviewer": form["notify_reviewer"]},
                   "completion": {"binding_artifact": binding_artifact, "artifacts": artifacts,
                                  "marker": marker, "marker_source": "artifact"}}
        if not scheduler._worker_quiescent(request, observed):
            raise ValueError("worker is not idle")
        # Structural validation uses the existing root until the task directory exists.
        # The exact request is validated again after exclusive mkdir, before dispatch.
        structural = copy.deepcopy(request)
        structural["cwd"] = str(self.tasks_root)
        validate_request(structural, require_dispatch=False)
        message = ("Task ID: " + form["task_id"] + "\nNonce: " + form["nonce"] +
                   "\nCwd: " + str(cwd) + "\nCompletion marker: " + marker + "\n\n" +
                   form["body"].rstrip() + "\n\nDeliverables: write " +
                   ", ".join([binding_artifact] + artifacts) + ". The final line of " +
                   binding_artifact + " must be exactly:\n" + marker + "\n")
        return {"form": form, "request": request, "message": message,
                "preview_sha256": _digest(request, message, form["resources"]),
                "baseline_completion_seq": baseline}

    def preview(self, payload):
        result = self.prepare(payload)
        with self.preview_lock:
            self.preview_records[result["preview_sha256"]] = time.monotonic()
        return result

    def confirm(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("preview_sha256"), str):
            raise ValueError("preview_sha256 is required")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("invalid task ID")
        with self.preview_lock:
            lock = self.task_locks.setdefault(task_id, threading.Lock())
        with lock:
            result = self.prepare(payload, confirm=True)
            digest = result["preview_sha256"]
            if not hmac.compare_digest(payload["preview_sha256"], digest):
                raise ValueError("preview changed; preview again before dispatch")
            with self.preview_lock:
                created = self.preview_records.pop(digest, None)
            if created is None or time.monotonic() - created > 600:
                raise ValueError("preview missing or expired; preview again")
            request, message = result["request"], result["message"]
            cwd = Path(request["cwd"])
            cwd.mkdir(exist_ok=False)
            try:
                validate_request(request, require_dispatch=False)
            except Exception:
                cwd.rmdir()  # still empty; leaves the task ID usable after a validation failure
                raise
            with (cwd / "task.md").open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(message)
            with (cwd / "request.json").open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(request, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
            outcome = scheduler.dispatch(self.state, request, message, self.actor, self.version, "main",
                                         resources=result["form"]["resources"],
                                         transport=self.transport_factory(request))
            return {"status": outcome["status"], "result": outcome, "request": request,
                    "reconcile_command": "& " + _ps_quote(sys.executable) + " -B " + _ps_quote(SCRIPTS / "workflow_scheduler.py") +
                    " --state " + _ps_quote(self.state) +
                    " reconcile --task " + request["task_id"] + " --outcome running --evidence " +
                    _ps_quote(cwd / "EXECUTION.md") + " --request-out " +
                    _ps_quote(cwd / "confirmed.json") + " --actor " + self.actor +
                    " --version " + str(self.version) + " --context main",
                    "monitor_command": "& " + _ps_quote(sys.executable) + " -B " + _ps_quote(SCRIPTS / "monitor_task.py") +
                    " --request " +
                    _ps_quote(cwd / "confirmed.json") + " --notify",
                    "execution_template": "Task ID: " + request["task_id"] + "\nNonce: " + request["nonce"] +
                    "\nOutcome: running\nObserved execution: \n"}

    def prepare_review(self, payload):
        """Read-only check of one review gate step; workflow_control repeats every check on confirm."""
        if not isinstance(payload, dict) or not isinstance(payload.get("event_id"), str) \
                or not EVENT_ID.fullmatch(payload["event_id"]):
            raise ValueError("invalid event ID")
        event_id = payload["event_id"]
        with scheduler._read_database(self.state) as con:
            who = control.guard(con, self.actor, self.version, self.context, "review")
            event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not event or event["role"] != who["role"]:
                raise ValueError("event belongs to another reviewer role")
            request = json.loads(con.execute("SELECT data FROM tasks WHERE id=?", (event["task"],)).fetchone()[0])["request"]
            transitions = control.config_of(con).get("workflow", {}).get("transitions", {})
            status = event["status"]
        base = {"event_id": event_id, "task_id": event["task"], "nonce": event["nonce"], "event_status": status,
                "actor": self.actor, "version": self.version}
        if status == "pending":
            step = dict(base, step="acknowledge")
        elif status == "acknowledged":
            verdict = payload.get("verdict")
            if verdict not in transitions:
                raise ValueError("choose a verdict configured in workflow.transitions")
            # Only the frozen review_path, or REVIEW.md in the task folder; the page never writes it.
            path = Path(request.get("review_path") or Path(request["cwd"]) / "REVIEW.md").resolve()
            if not path.is_file():
                raise ValueError("write " + str(path) + " before recording the review")
            raw = path.read_bytes()
            if len(raw) > ARTIFACT_LIMIT:
                raise ValueError("review file is too large to preview")
            text = raw.decode("utf-8-sig")
            if event["task"] not in text or event["nonce"] not in text:
                raise ValueError("review file must contain the task ID and nonce")
            if not re.search(r"^Verdict:\s*" + re.escape(verdict) + r"\s*$", text, re.M):
                raise ValueError("review file needs the line: Verdict: " + verdict)
            step = dict(base, step="record_review", verdict=verdict, action=transitions[verdict],
                        review_path=str(path), review_sha256=hashlib.sha256(raw).hexdigest(), review_text=text)
        else:
            raise ValueError("this event is already " + status)
        step["preview_sha256"] = control.digest(step)
        return step

    def review_preview(self, payload):
        result = self.prepare_review(payload)
        with self.preview_lock:
            self.preview_records["review:" + result["preview_sha256"]] = time.monotonic()
        return result

    def review_confirm(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("preview_sha256"), str):
            raise ValueError("preview_sha256 is required")
        with self.review_lock:
            result = self.prepare_review(payload)
            digest = result["preview_sha256"]
            if not hmac.compare_digest(payload["preview_sha256"], digest):
                raise ValueError("review step changed; preview again")
            with self.preview_lock:
                created = self.preview_records.pop("review:" + digest, None)
            if created is None or time.monotonic() - created > 600:
                raise ValueError("preview missing or expired; preview again")
            if result["step"] == "acknowledge":
                return control.acknowledge(self.state, result["event_id"], self.actor, self.version, self.context)
            recorded = control.record_review(self.state, result["event_id"], self.actor, self.version, self.context,
                                             result["review_path"], result["verdict"])
            return dict(recorded, status="REVIEW_RECORDED")


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format, *args):
        pass

    def _headers(self, status, content_type, length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" +
                         self.server.csp_nonce + "'; style-src 'nonce-" + self.server.csp_nonce +
                         "'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()

    def _json(self, status, value):
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _host(self):
        if self.server.lan_tls:
            allowed = {self.server.server_address[0] + ":" + str(self.server.server_port)}
        else:
            allowed = {"127.0.0.1:" + str(self.server.server_port),
                       "localhost:" + str(self.server.server_port)}
            allowed.update(self.server.public_hosts)
        return self.headers.get("Host", "").lower() in allowed

    def _token(self):
        supplied = self.headers.get("X-Dashboard-Token", "")
        return hmac.compare_digest(supplied, self.server.token)

    def _origin(self):
        host = self.headers.get("Host", "").lower()
        if self.server.lan_tls:
            return (host == self.server.server_address[0] + ":" + str(self.server.server_port)
                    and self.headers.get("Origin") == "https://" + host)
        local = {"127.0.0.1:" + str(self.server.server_port),
                 "localhost:" + str(self.server.server_port)}
        expected = "http://" + host if host in local else "https://" + host
        return self.headers.get("Origin") == expected and (host in local or host in self.server.public_hosts)

    def _read_raw(self):
        # Consume the bounded body before any reply: on Windows, closing with unread bytes sends RST, not FIN.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 < length <= 100000:
            return None
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    def _body(self, raw):
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not 0 < length <= 100000 or raw is None or len(raw) != length:
            raise ValueError("request body must be 1 to 100000 bytes")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self):
        if not self._host():
            self._error(403, "invalid Host")
            return
        path = urlsplit(self.path).path
        if path == "/":
            body = HTML.replace("__CSP_NONCE__", self.server.csp_nonce).replace("__I18N_ZH__", I18N_ZH).encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if not self._token():
            self._error(403, "invalid dashboard token")
            return
        if path == "/api/artifacts":
            try:
                task = parse_qs(urlsplit(self.path).query).get("task", [None])[0]
                self._json(200, self.server.artifacts(task))
            except ValueError as exc:
                self._error(400, str(exc)[:300])
            except Exception:
                self._error(500, "result file read failed")
            return
        if path != "/api/status":
            self._error(404, "not found")
            return
        try:
            self._json(200, self.server.status_data())
        except Exception:
            self._error(500, "status read failed")

    def do_POST(self):
        raw = self._read_raw()
        if not self._host():
            self._error(403, "invalid Host")
            return
        if not self._origin():
            self._error(403, "invalid Origin")
            return
        if not self._token():
            self._error(403, "invalid dashboard token")
            return
        path = urlsplit(self.path).path
        if path.startswith(("/api/dispatch/", "/api/review/")) and not self.server.allow_dispatch:
            self._error(403, "dispatch is disabled")
            return
        if path.startswith("/api/chat/") and not self.server.allow_chat:
            self._error(403, "chat is disabled")
            return
        if path not in ("/api/state", "/api/probe", "/api/dispatch/preview", "/api/dispatch/confirm",
                        "/api/review/preview", "/api/review/confirm", "/api/chat/send", "/api/chat/output"):
            self._error(404, "not found")
            return
        try:
            payload = self._body(raw)
            if path == "/api/state":
                result = self.server.select_state(payload)
            elif path == "/api/probe":
                result = self.server.probe()
            elif path == "/api/chat/send":
                result = self.server.chat_send(payload)
            elif path == "/api/chat/output":
                result = self.server.chat_output(payload)
            elif path == "/api/review/preview":
                result = self.server.review_preview(payload)
            elif path == "/api/review/confirm":
                result = self.server.review_confirm(payload)
            elif path.endswith("/preview"):
                result = self.server.preview(payload)
            else:
                result = self.server.confirm(payload)
            self._json(200, result)
        except PermissionError as exc:
            self._error(403, str(exc))
        except FileExistsError as exc:
            self._error(409, str(exc))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error(400, str(exc)[:300])
        except Exception:
            self._error(500, "operation failed; inspect the ledger before any retry")


def open_server(host, port, state, **options):
    """Bind the requested port; when the default port is busy, fall back to a free one."""
    try:
        return DashboardServer((host, port), state, **options)
    except OSError as exc:
        if port != DEFAULT_PORT or exc.errno not in (errno.EADDRINUSE, 10048):
            raise
        return DashboardServer((host, 0), state, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", help="workflow state directory; omit to choose one in the page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--lan", metavar="ADDRESS", help="listen on a private IPv4 address over HTTPS; use auto only when unique")
    parser.add_argument("--tls-cert", metavar="PATH", help="LAN HTTPS certificate PEM; requires --tls-key")
    parser.add_argument("--tls-key", metavar="PATH", help="LAN HTTPS private key PEM; requires --tls-cert")
    parser.add_argument("--lan-allow-control", action="store_true", help="acknowledge that LAN token holders can control agents")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    parser.add_argument("--public-origin", action="append", default=[], metavar="URL",
                        help="authorized HTTPS reverse-proxy origin; repeat for more than one")
    parser.add_argument("--allow-dispatch", action="store_true")
    parser.add_argument("--allow-chat", action="store_true",
                        help="let the bound controller chat with any active role")
    parser.add_argument("--actor")
    parser.add_argument("--version", type=int)
    parser.add_argument("--context", choices=("main", "side", "unknown"))
    parser.add_argument("--tasks-root")
    args = parser.parse_args(argv)
    try:
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ValueError("--tls-cert and --tls-key must be provided together")
        if args.lan is None and (args.tls_cert or args.lan_allow_control):
            raise ValueError("--tls-cert, --tls-key, and --lan-allow-control require --lan")
        if args.lan is not None and args.host != "127.0.0.1":
            raise ValueError("--lan cannot be combined with --host")
        if args.lan is not None and args.public_origin:
            raise ValueError("--lan and --public-origin are separate access modes; choose one")
        if args.lan is not None and (args.allow_chat or args.allow_dispatch) and not args.lan_allow_control:
            raise ValueError("LAN token holders can direct agents; add --lan-allow-control only after accepting this risk")
        host = _choose_lan(args.lan) if args.lan is not None else args.host
        lan_tls = ((Path(args.tls_cert), Path(args.tls_key)) if args.tls_cert else _lan_certificate(host)) if args.lan else None
        server = open_server(host, args.port, args.state, allow_dispatch=args.allow_dispatch,
                             allow_chat=args.allow_chat,
                             actor=args.actor, version=args.version, context=args.context,
                             tasks_root=args.tasks_root, recent_file=RECENT_FILE,
                             public_origins=args.public_origin, lan_tls=lan_tls)
    except (ValueError, OSError, ssl.SSLError) as exc:
        parser.exit(2, "dashboard: " + str(exc) + "\n")
    with server:
        url = server.origin() + "/#" + server.token
        print(("Dashboard (LAN): " if server.lan_tls else "Dashboard: ") + url, flush=True)
        if server.lan_tls:
            print("Certificate SHA-256: " + _certificate_fingerprint(lan_tls[0]), flush=True)
        for public in server.public_origins:
            print("Dashboard (remote): " + public + "/#" + server.token, flush=True)
        print("State: " + (str(server.state) if server.state else "choose in the page"), flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
