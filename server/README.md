# Cc clash 中转服务器

朋友的 Cc clash 只连到这台服务器。服务器按你写的规则分流，再转发到这位朋友自己的机场节点或固定 IP。规则和节点都只保存在服务器上，朋友那边看不到。

## 安装

在云服务器的网页控制台登录后，依次运行：

```bash
sudo -i
curl -fsSL https://raw.githubusercontent.com/blueice1988a-ops/cc-clash/smart_core/server/install.sh | bash
```

然后在云服务器网页后台的“防火墙”里放行 TCP 80 和 TCP 443 端口。

## 常用命令

先运行 `sudo -i`，再执行：

| 命令                                         | 作用                                         |
| -------------------------------------------- | -------------------------------------------- |
| `ccrelay add 名字 订阅链接`                  | 添加一位朋友，打印 TA 要填进 Cc clash 的链接 |
| `ccrelay ip 名字 socks5 IP 端口 用户名 密码` | 给朋友设置固定 IP（类型也可以是 http）       |
| `ccrelay ip 名字 删除`                       | 去掉朋友的固定 IP                            |
| `ccrelay sub 名字 新订阅链接`                | 更换朋友的机场订阅                           |
| `ccrelay link 名字`                          | 再次显示朋友的 Cc clash 链接                 |
| `ccrelay del 名字`                           | 删除一位朋友                                 |
| `ccrelay list`                               | 查看所有朋友和本月流量                       |
| `ccrelay rules`                              | 编辑分流规则（保存后自动生效）               |

编辑规则时用的是 nano 编辑器：改完按 `Ctrl+O` 再按回车保存，最后按 `Ctrl+X` 退出。

## 数据位置

所有数据都在 `/opt/ccrelay/data`：`state.json` 存朋友和密钥，`rules.txt` 存分流规则，`usage.json` 存流量统计。
