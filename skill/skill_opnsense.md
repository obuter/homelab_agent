# OPNsense & CrowdSec

## OPNsense host rules
  - FreeBSD system, logged in as root — never use sudo
  - Network interfaces: vtnet0 (WAN), vtnet1 (LAN) — never em0, eth0, etc.
  - Firewall rules use 'pass' and 'block' — never 'allow' or 'deny'
  - pfctl is at /sbin/pfctl — NEVER use /usr/sbin/pfctl
  - Logs: plain text via syslog-ng — use standard Unix tools
    tail -n 50 /var/log/filter.log
    grep '1.2.3.4' /var/log/filter.log
    tail -n 50 /var/log/auth.log
    ls /var/log/
  - do NOT use 'log show' (macOS only), do NOT use clog
  - tcpdump: always use '-c <count>' to limit packets
    Correct: tcpdump -i vtnet1 -nn -c 20
    WRONG:   tcpdump -i vtnet1 -w file.pcap 10
  - ARP table: 'arp -an'
  - Tailscale uses 100.x.x.x range; 172.16.0.1 is likely Tailscale subnet router
    netstat -rn | grep 172.16
    ifconfig | grep -A2 172.16
  - opnsense() API tool may not be configured — if it returns connection errors twice,
    stop retrying and use ssh_exec instead
  - Prefer opnsense() API for structured data when it works; fall back to ssh_exec

## CrowdSec architecture
  - OPNsense is the LAPI (central server) — all decisions, alerts, bouncers live here
  - debian runs a forwarding agent only — parses Caddy logs, ships to OPNsense LAPI
  - CrowdSec LAPI listens on OPNsense at 0.0.0.0:8080

## CrowdSec on OPNsense
  Commands (run via ssh_exec host=opnsense):
    cscli decisions list
    cscli alerts list
    cscli machines list
    cscli bouncers list
    cscli metrics
    cscli hub list

  pf tables (blocklists enforced by crowdsec_firewall bouncer):
    pfctl -t crowdsec_blocklists -T show | wc -l     # IPv4 count
    pfctl -t crowdsec6_blocklists -T show | wc -l    # IPv6 count
    pfctl -t crowdsec_blocklists -T show | grep <ip> # check specific IP
    pfctl -vsA   # list pf anchor tables (user_rules, user_rules/crowdsec)
    /usr/local/etc/rc.d/crowdsec_firewall status      # bouncer process

  Config paths:
    /usr/local/etc/crowdsec/config.yaml
    /usr/local/etc/crowdsec/acquis.yaml   (monitors: nginx, auth.log, httpd)
    /usr/local/etc/crowdsec/profiles.yaml (ban 4h, notify via ntfy)
    /usr/local/etc/crowdsec/notifications/ntfy.yaml
    /var/log/crowdsec/
