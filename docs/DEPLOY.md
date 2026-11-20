# Running the desk continuously (and opening it on an iPhone)

The team works autonomously **only while its server process runs**. A Claude Code cloud session is not a host: its
container is temporary and not reachable from the internet. For 24/7 operation you need an always-on machine
(a small VPS, a home server, a NAS that runs Docker). **Nothing has been deployed yet, and 24/7 operation has not been verified.**

## Minimum setup (VPS, Docker)

```bash
git clone <your repo> && cd AI-trading-desk
cp .env.example .env
# edit .env:
#   TRADEDESK_ACCESS_TOKEN=<long random string>   (required for phone access)
#   TRADEDESK_OWNER_TOKEN=<another long random string>
#   OANDA_API_TOKEN=<practice token>                (real-time quotes; read-only use)
docker compose up -d
docker compose logs -f
```

The container listens on `127.0.0.1:8000`. Put an HTTPS reverse proxy in front, for example Caddy:

```
desk.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

On the iPhone, open `https://desk.example.com` and enter the access token once. A cookie keeps you logged in for 30 days.
Safari → Share → "Add to Home Screen" makes it behave like an app.

## Without Docker (systemd)

```bash
sudo useradd -m desk && sudo mkdir -p /opt && sudo git clone <repo> /opt/AI-trading-desk
cd /opt/AI-trading-desk && sudo pip install -r requirements.txt && sudo cp .env.example .env   # edit .env
sudo cp scripts/tradedesk.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now tradedesk
```

## What persists across restarts

Everything is in SQLite (`data/tradedesk.db`): the account ledger, positions, orders, trades, decisions, debates,
errors, lessons, experiments and reports. On restart, open positions are picked up again and managed with fresh quotes.
Back up the file regularly.

## Verification status

| Item | Verified? |
|---|---|
| Server, background loops and dashboard on localhost | yes (in the development container) |
| Docker image build | **no** — the Docker daemon is not available in the development container |
| systemd unit | **no** — not tested on a real host |
| Phone access through HTTPS | **no** — needs your host and domain |
