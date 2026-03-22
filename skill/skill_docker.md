# Docker & Debian Host

## Debian host rules
  - Never use 'sudo' in ssh_exec commands on this host — it will hang
  - All Docker apps live under /opt/docker/<appname>/
  - Docker Compose files: /opt/docker/<appname>/docker-compose.yml
  - Caddy config:  /opt/docker/caddy/Caddyfile
  - To read remote files: ssh_exec with 'cat <path>' — read_file() is LOCAL only

## Docker conventions
  docker ps
  docker logs --tail 50 <name>
  docker inspect <name>
  docker restart <name>   [requires user confirmation]
  ls /opt/docker/
  Always run 'docker ps' first to confirm container name before using it.
  When comparing Docker image digests, filter by architecture:
    Docker Hub API returns per-arch digests — match the 'amd64' entry only.
    The running container digest from 'docker inspect' is always amd64 on debian.
  To update a container, always use Compose — never 'docker stop && docker start':
    docker compose -f /opt/docker/<appname>/docker-compose.yml pull
    docker compose -f /opt/docker/<appname>/docker-compose.yml up -d

## CrowdSec on Debian (forwarding agent only)
  - debian runs a forwarding agent only (Docker container named 'crowdsec')
  - It parses Caddy logs and ships events to OPNsense LAPI
  - It does NOT hold decisions or run a bouncer
  Commands:
    docker exec crowdsec cscli metrics
    docker exec crowdsec cscli alerts list
    docker logs --tail 50 crowdsec
    /opt/docker/crowdsec/config/acquis.yaml  (monitors Caddy logs)
  - Do NOT run 'cscli decisions list' on debian — it has no LAPI
  - For global decisions/blocks always query opnsense
