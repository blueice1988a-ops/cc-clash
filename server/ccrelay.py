#!/usr/bin/env python3
"""Cc clash 中转服务器管理工具。

朋友的 Cc clash 只连到这台服务器；服务器按管理员写的规则分流，
再转发到这位朋友自己的机场节点（或固定 IP）。规则和节点都只存在服务器上。

用法（在服务器上运行）：
  ccrelay add 名字 订阅链接          添加一位朋友，并打印他要填进 Cc clash 的链接
  ccrelay ip 名字 类型 IP 端口 [用户名 密码]
                                     给朋友设置固定 IP，类型是 socks5 或 http
  ccrelay ip 名字 删除               去掉朋友的固定 IP
  ccrelay sub 名字 新订阅链接        更换朋友的机场订阅
  ccrelay link 名字                  再次显示朋友的 Cc clash 链接
  ccrelay del 名字                   删除一位朋友
  ccrelay list                       查看所有朋友和本月流量
  ccrelay rules                      编辑分流规则
  ccrelay reload                     重新加载配置
"""

import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.environ.get('CCRELAY_HOME', '/opt/ccrelay')
STATE = os.path.join(HOME, 'data', 'state.json')
USAGE = os.path.join(HOME, 'data', 'usage.json')
RULES = os.path.join(HOME, 'data', 'rules.txt')
CORE_DIR = os.path.join(HOME, 'core')
CORE_CONFIG = os.path.join(CORE_DIR, 'config.yaml')

# 规则里“去哪里”可以写的地区，以及用来从机场节点名里挑出该地区节点的关键词
REGIONS = {
    '日本': r'日本|东京|大阪|🇯🇵|(?i:\bjp\b|japan|tokyo|osaka)',
    '美国': r'美国|洛杉矶|硅谷|西雅图|🇺🇸|(?i:\bus\b|\busa\b|united states|america)',
    '香港': r'香港|🇭🇰|(?i:\bhk\b|hong ?kong)',
    '新加坡': r'新加坡|狮城|🇸🇬|(?i:\bsg\b|singapore)',
    '台湾': r'台湾|🇹🇼|(?i:\btw\b|taiwan)',
    '韩国': r'韩国|首尔|🇰🇷|(?i:\bkr\b|korea|seoul)',
}
# 机场常把“剩余流量”“到期时间”之类的信息伪装成节点，这些不能用来上网
EXCLUDE = r'剩余|到期|过期|官网|流量|重置|套餐|(?i:expire|traffic)'
SPECIAL_TARGETS = {'直连': 'DIRECT', '拒绝': 'REJECT'}
TARGETS = ['自动', *REGIONS, '固定IP', *SPECIAL_TARGETS]
NAME_RE = re.compile(r'^[\w-]{1,20}$')
HEALTH_URL = 'https://www.gstatic.com/generate_204'

DEFAULT_RULES = """\
# Cc clash 分流规则（只存在服务器上，朋友看不到）
#
# 每行一条：类型,内容,去哪里
#   类型常用的有：
#     DOMAIN-SUFFIX  某个网站及它的子域名，例如 DOMAIN-SUFFIX,openai.com,固定IP
#     DOMAIN-KEYWORD 网址里包含某个词，例如 DOMAIN-KEYWORD,google,自动
#     IP-CIDR        某一段 IP，例如 IP-CIDR,1.2.3.0/24,日本
#     GEOIP          某个国家的 IP，例如 GEOIP,JP,日本
#   去哪里可以写：自动 日本 美国 香港 新加坡 台湾 韩国 固定IP 直连 拒绝
#     自动    从这位朋友的机场节点里自动挑最快的
#     日本等  只用名字里带这个地区的节点，找不到时自动改用“自动”
#     固定IP  走朋友的固定 IP（先经过他的机场节点）；没设置固定 IP 时改用“美国”
#     直连    从服务器直接出去，不经过机场节点
#     拒绝    不让访问
#
# 从上往下匹配，第一条符合的生效；都不符合的走“自动”。
# 改完保存后，运行 ccrelay reload 生效（用 ccrelay rules 编辑会自动生效）。

DOMAIN-SUFFIX,openai.com,固定IP
DOMAIN-SUFFIX,chatgpt.com,固定IP
DOMAIN-SUFFIX,claude.ai,固定IP
DOMAIN-SUFFIX,anthropic.com,固定IP
DOMAIN-SUFFIX,oaistatic.com,固定IP
DOMAIN-SUFFIX,oaiusercontent.com,固定IP
DOMAIN-SUFFIX,claude.com,固定IP
"""


def die(msg):
    print(f'❌ {msg}', file=sys.stderr)
    sys.exit(1)


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def q(value):
    """把字符串写成 YAML 安全的格式（JSON 字符串本身就是合法的 YAML）。"""
    return json.dumps(str(value), ensure_ascii=False)


def month_key():
    return datetime.date.today().strftime('%Y-%m')


# ---------- 规则 ----------

def parse_rules(text):
    """把管理员写的规则翻译成 (mihomo 规则前半部分, 去哪里, 额外参数)，有错就报出行号。"""
    rules, errors = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.replace('，', ',').split(',')]
        if len(parts) < 3:
            errors.append(f'第 {lineno} 行格式不对，应该是“类型,内容,去哪里”：{raw.strip()}')
            continue
        kind, value, target, *extra = parts
        if target not in TARGETS:
            errors.append(f'第 {lineno} 行的“{target}”不认识，去哪里只能写：{" ".join(TARGETS)}')
            continue
        rules.append((f'{kind.upper()},{value}', target, extra))
    return rules, errors


def user_target(name, target, user):
    if target in SPECIAL_TARGETS:
        return SPECIAL_TARGETS[target]
    if target == '固定IP':
        # 固定 IP 多用于 ChatGPT、Claude 这类按地区限制的网站，没设置时退回美国节点
        return f'{name}-固定IP' if user.get('static') else f'{name}-美国'
    return f'{name}-{target}'


# ---------- 生成服务器上 mihomo 的配置 ----------

def build_core_config(state, rules):
    r = state['reality']
    out = [
        'log-level: warning',
        'mode: rule',
        'ipv6: false',
        f'external-controller: 127.0.0.1:{state["api_port"]}',
        f'secret: {q(state["api_secret"])}',
        'unified-delay: true',
        'listeners:',
        '  - name: relay',
        '    type: vless',
        f'    port: {state["port"]}',
        '    listen: 0.0.0.0',
        '    users:',
    ]
    users = state['users']
    if not users:
        # 还没有朋友时放一个占位用户，监听才能正常启动；占位用户的流量全部拒绝
        out.append(f'      - {{username: {q("_placeholder")}, uuid: {q(state["placeholder_uuid"])}, flow: xtls-rprx-vision}}')
    for name, u in users.items():
        out.append(f'      - {{username: {q(name)}, uuid: {q(u["uuid"])}, flow: xtls-rprx-vision}}')
    out += [
        '    reality-config:',
        f'      dest: {q(r["dest"])}',
        f'      private-key: {q(r["private_key"])}',
        f'      short-id: [{q(r["short_id"])}]',
        f'      server-names: [{q(r["server_name"])}]',
    ]

    if users:
        out.append('proxy-providers:')
        for name, u in users.items():
            out += [
                f'  {q("p-" + name)}:',
                '    type: http',
                f'    url: {q(u["sub"])}',
                f'    path: {q("./providers/" + name + ".yaml")}',
                '    interval: 3600',
                f'    exclude-filter: {q(EXCLUDE)}',
                '    health-check:',
                '      enable: true',
                f'      url: {q(HEALTH_URL)}',
                '      interval: 600',
            ]

        static = {n: u['static'] for n, u in users.items() if u.get('static')}
        if static:
            out.append('proxies:')
            for name, s in static.items():
                out += [
                    f'  - name: {q(name + "-固定IP")}',
                    f'    type: {s["type"]}',
                    f'    server: {q(s["server"])}',
                    f'    port: {int(s["port"])}',
                    f'    dialer-proxy: {q(name + "-自动")}',
                    '    udp: true',
                ]
                if s.get('username'):
                    out.append(f'    username: {q(s["username"])}')
                    out.append(f'    password: {q(s.get("password", ""))}')

        out.append('proxy-groups:')
        for name in users:
            provider = q('p-' + name)
            out.append(
                f'  - {{name: {q(name + "-自动")}, type: url-test, use: [{provider}], '
                f'url: {q(HEALTH_URL)}, interval: 300, tolerance: 50}}'
            )
            for region, pattern in REGIONS.items():
                # 先挑该地区最快的节点；该地区一个节点都没有或都不通时，退回“自动”
                out.append(
                    f'  - {{name: {q(name + "-" + region + "-节点")}, type: url-test, use: [{provider}], '
                    f'filter: {q(pattern)}, url: {q(HEALTH_URL)}, interval: 300, tolerance: 50}}'
                )
                out.append(
                    f'  - {{name: {q(name + "-" + region)}, type: fallback, '
                    f'proxies: [{q(name + "-" + region + "-节点")}, {q(name + "-自动")}], '
                    f'url: {q(HEALTH_URL)}, interval: 300}}'
                )

    out += [
        'rules:',
        # 服务器自己的连接（下载订阅、健康检查等）直接出去
        '  - IN-TYPE,INNER,DIRECT',
    ]
    if state.get('block_private', True):
        # 不让朋友通过中转访问服务器本机和内网
        for cidr in ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16'):
            out.append(f'  - IP-CIDR,{cidr},REJECT,no-resolve')
    for name in users:
        out.append(f'  - {q(f"SUB-RULE,(IN-USER,{name}),u-{name}")}')
    out.append('  - MATCH,REJECT')

    if users:
        out.append('sub-rules:')
        for name, u in users.items():
            out.append(f'  {q("u-" + name)}:')
            for head, target, extra in rules:
                tail = ''.join(',' + e for e in extra)
                out.append(f'    - {q(head + "," + user_target(name, target, u) + tail)}')
            out.append(f'    - {q("MATCH," + name + "-自动")}')
    return '\n'.join(out) + '\n'


# ---------- 朋友在 Cc clash 里导入的配置 ----------

def build_client_config(state, name):
    r = state['reality']
    u = state['users'][name]
    return f"""\
proxies:
  - name: Cc 线路
    type: vless
    server: {q(state["public_ip"])}
    port: {state["port"]}
    uuid: {q(u["uuid"])}
    network: tcp
    tls: true
    udp: true
    flow: xtls-rprx-vision
    servername: {q(r["server_name"])}
    client-fingerprint: chrome
    reality-opts:
      public-key: {q(r["public_key"])}
      short-id: {q(r["short_id"])}
proxy-groups:
  - name: 代理
    type: select
    proxies: [Cc 线路]
# 虚拟网卡模式下靠 fake-ip 和嗅探把域名带到服务器，服务器才能按域名分流（比如 ChatGPT 走固定 IP）
dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
  fake-ip-filter:
    - '*.lan'
    - '+.local'
    - '+.msftconnecttest.com'
    - '+.msftncsi.com'
    - 'time.*.com'
    - 'ntp.*.com'
  default-nameserver: [223.5.5.5, 119.29.29.29]
  nameserver: ['https://223.5.5.5/dns-query', 'https://doh.pub/dns-query']
sniffer:
  enable: true
  sniff:
    HTTP:
      ports: [80, 8080-8880]
      override-destination: true
    TLS:
      ports: [443, 8443]
    QUIC:
      ports: [443, 8443]
rules:
  - GEOSITE,private,DIRECT
  - GEOIP,private,DIRECT,no-resolve
  - GEOSITE,cn,DIRECT
  - GEOIP,CN,DIRECT
  - MATCH,代理
"""


def client_link(state, name):
    return f'http://{state["public_ip"]}:{state["sub_port"]}/s/{state["users"][name]["token"]}'


# ---------- 应用配置 ----------

def write_and_reload(state, quiet=False):
    text = load_rules_text()
    rules, errors = parse_rules(text)
    if errors:
        die('分流规则有错误，没有生效：\n' + '\n'.join(errors))
    os.makedirs(CORE_DIR, exist_ok=True)
    new = build_core_config(state, rules)
    tmp = CORE_CONFIG + '.new'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new)
    os.chmod(tmp, 0o600)
    mihomo = os.path.join(HOME, 'bin', 'mihomo')
    if os.path.exists(mihomo):
        check = subprocess.run([mihomo, '-t', '-d', CORE_DIR, '-f', tmp], capture_output=True, text=True)
        if check.returncode != 0:
            os.remove(tmp)
            die('生成的配置检查没通过，旧配置保持不变：\n' + (check.stdout + check.stderr)[-1500:])
    os.replace(tmp, CORE_CONFIG)
    try:
        api('PUT', '/configs?force=true', state, {'path': CORE_CONFIG})
    except Exception:
        subprocess.run(['systemctl', 'restart', 'ccrelay-core'], check=False)
    if not quiet:
        print('✅ 配置已生效')


def api(method, path, state, body=None):
    req = urllib.request.Request(
        f'http://127.0.0.1:{state["api_port"]}{path}',
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': f'Bearer {state["api_secret"]}', 'Content-Type': 'application/json'},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as res:
        data = res.read()
        return json.loads(data) if data else None


def load_rules_text():
    try:
        with open(RULES, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return DEFAULT_RULES


def load_state():
    state = load_json(STATE, None)
    if state is None:
        die(f'找不到 {STATE}，请先运行安装脚本')
    return state


# ---------- 命令 ----------

def check_name(name):
    if not NAME_RE.match(name):
        die('名字只能用中文、字母、数字、下划线或横线，最长 20 个字')


def check_sub(sub):
    if not re.match(r'^https?://', sub):
        die('订阅链接要以 http:// 或 https:// 开头')


def cmd_add(name, sub):
    check_name(name)
    check_sub(sub)
    state = load_state()
    if name in state['users']:
        die(f'“{name}”已经存在了。要换订阅请用：ccrelay sub {name} 新链接')
    state['users'][name] = {
        'uuid': str(uuid.uuid4()),
        'token': secrets.token_urlsafe(18),
        'sub': sub,
        'static': None,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    save_json(STATE, state)
    write_and_reload(state, quiet=True)
    print(f'✅ 已添加“{name}”。把下面这个链接发给 TA，在 Cc clash 的“订阅”里粘贴导入：\n')
    print(f'   {client_link(state, name)}\n')
    print('这个链接只给 TA 一个人用，泄露了可以删掉重新添加。')


def cmd_sub(name, sub):
    check_sub(sub)
    state = load_state()
    if name not in state['users']:
        die(f'没有“{name}”这个人')
    state['users'][name]['sub'] = sub
    save_json(STATE, state)
    write_and_reload(state)


def cmd_ip(name, *args):
    state = load_state()
    if name not in state['users']:
        die(f'没有“{name}”这个人')
    if args and args[0] in ('删除', 'del', 'remove', 'none'):
        state['users'][name]['static'] = None
    else:
        if len(args) not in (3, 5):
            die('用法：ccrelay ip 名字 类型 IP 端口 [用户名 密码]，类型是 socks5 或 http')
        kind, server, port = args[0].lower(), args[1], args[2]
        if kind not in ('socks5', 'http'):
            die('类型只能是 socks5 或 http')
        if not port.isdigit():
            die('端口要是数字')
        static = {'type': kind, 'server': server, 'port': int(port)}
        if len(args) == 5:
            static['username'], static['password'] = args[3], args[4]
        state['users'][name]['static'] = static
    save_json(STATE, state)
    write_and_reload(state)


def cmd_link(name):
    state = load_state()
    if name not in state['users']:
        die(f'没有“{name}”这个人')
    print(client_link(state, name))


def cmd_del(name):
    state = load_state()
    if state['users'].pop(name, None) is None:
        die(f'没有“{name}”这个人')
    save_json(STATE, state)
    write_and_reload(state)
    print(f'已删除“{name}”，TA 的 Cc clash 将无法再连接')


def fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024


def cmd_list():
    state = load_state()
    usage = load_json(USAGE, {}).get(month_key(), {})
    if not state['users']:
        print('还没有添加任何人。用 ccrelay add 名字 订阅链接 添加。')
        return
    print(f'本月（{month_key()}）流量：')
    total = 0
    for name, u in state['users'].items():
        used = usage.get(name, {})
        n = used.get('up', 0) + used.get('down', 0)
        total += n
        static = f'固定IP {u["static"]["server"]}' if u.get('static') else ''
        print(f'  {name:<12} {fmt_bytes(n):>10}   {static}')
    print(f'  {"合计":<12} {fmt_bytes(total):>10}')


def cmd_rules():
    if not os.path.exists(RULES):
        with open(RULES, 'w', encoding='utf-8') as f:
            f.write(DEFAULT_RULES)
    editor = os.environ.get('EDITOR', 'nano')
    subprocess.run([editor, RULES])
    write_and_reload(load_state())


def cmd_reload():
    write_and_reload(load_state())


# ---------- 后台服务：给朋友提供订阅链接，并统计流量 ----------

def track_usage(state):
    seen = {}
    last_save = time.time()
    usage = load_json(USAGE, {})
    while True:
        try:
            data = api('GET', '/connections', state) or {}
            month = usage.setdefault(month_key(), {})
            alive = {}
            for c in data.get('connections') or []:
                user = (c.get('metadata') or {}).get('inboundUser')
                if not user:
                    continue
                up, down = c.get('upload', 0), c.get('download', 0)
                prev_up, prev_down = seen.get(c['id'], (0, 0))
                bucket = month.setdefault(user, {'up': 0, 'down': 0})
                bucket['up'] += max(0, up - prev_up)
                bucket['down'] += max(0, down - prev_down)
                alive[c['id']] = (up, down)
            seen = alive
            if time.time() - last_save > 60:
                save_json(USAGE, usage)
                last_save = time.time()
        except Exception:
            pass
        time.sleep(3)


class SubHandler(BaseHTTPRequestHandler):
    server_version = 'nginx'
    sys_version = ''

    def do_GET(self):
        m = re.fullmatch(r'/s/([\w-]+)', urllib.parse.urlparse(self.path).path)
        state = load_json(STATE, {'users': {}})
        name = None
        if m:
            name = next((n for n, u in state['users'].items() if secrets.compare_digest(u['token'], m.group(1))), None)
        if not name:
            self.send_response(404)
            self.end_headers()
            return
        body = build_client_config(state, name).encode()
        used = load_json(USAGE, {}).get(month_key(), {}).get(name, {})
        self.send_response(200)
        self.send_header('Content-Type', 'text/yaml; charset=utf-8')
        self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote('Cc clash'))
        self.send_header('profile-update-interval', '24')
        self.send_header('subscription-userinfo', f'upload={used.get("up", 0)}; download={used.get("down", 0)}; total=0; expire=0')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def cmd_serve():
    state = load_state()
    threading.Thread(target=track_usage, args=(state,), daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', state['sub_port']), SubHandler).serve_forever()


def cmd_init(public_ip, private_key, public_key):
    """安装脚本调用：第一次生成服务器的基本信息。已存在时只更新 IP。"""
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    state = load_json(STATE, None)
    if state is None:
        state = {
            'public_ip': public_ip,
            'port': 443,
            'sub_port': 80,
            'api_port': 9090,
            'api_secret': secrets.token_urlsafe(24),
            'placeholder_uuid': str(uuid.uuid4()),
            'reality': {
                'dest': 'www.microsoft.com:443',
                'server_name': 'www.microsoft.com',
                'private_key': private_key,
                'public_key': public_key,
                'short_id': secrets.token_hex(4),
            },
            'users': {},
        }
    else:
        state['public_ip'] = public_ip
    save_json(STATE, state)
    if not os.path.exists(RULES):
        with open(RULES, 'w', encoding='utf-8') as f:
            f.write(DEFAULT_RULES)
    rules, errors = parse_rules(load_rules_text())
    if errors:
        die('\n'.join(errors))
    os.makedirs(CORE_DIR, exist_ok=True)
    with open(CORE_CONFIG, 'w', encoding='utf-8') as f:
        f.write(build_core_config(state, rules))
    os.chmod(CORE_CONFIG, 0o600)


COMMANDS = {
    'add': (cmd_add, 2),
    'sub': (cmd_sub, 2),
    'ip': (cmd_ip, None),
    'link': (cmd_link, 1),
    'del': (cmd_del, 1),
    'list': (cmd_list, 0),
    'rules': (cmd_rules, 0),
    'reload': (cmd_reload, 0),
    'serve': (cmd_serve, 0),
    'init': (cmd_init, 3),
}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return
    if 'CCRELAY_HOME' not in os.environ and os.geteuid() != 0:
        die('请先运行 sudo -i 切换到管理员，再执行 ccrelay 命令')
    fn, nargs = COMMANDS[argv[1]]
    args = argv[2:]
    if nargs is not None and len(args) != nargs:
        print(__doc__)
        sys.exit(1)
    fn(*args)


if __name__ == '__main__':
    main(sys.argv)
