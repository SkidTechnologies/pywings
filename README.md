# pywings

A lightweight, rootless Python implementation of the **Pterodactyl Wings** daemon powered by **udocker**.

`pywings` provides a complete drop-in replacement for standard Wings, allowing you to run Pterodactyl game servers without root privileges or full Docker daemon access. Because it uses `udocker`, it can run on virtually any Linux environment (including unprivileged containers, shared environments, and user-space nodes).

## Features

- **Full Pterodactyl Wings API Compatibility**: Supports all `/api/*` endpoints used by the Pterodactyl Panel.
- **udocker Runtime Engine**: Runs containers in user-space using PRoot (`udocker`) without needing Docker daemon or root/sudo.
- **Live Real-Time WebSocket Console**: Live bidirectional WebSocket console streaming stdout/stderr, stats, power actions, and install logs.
- **Full Egg Configuration Parser**: Automatically updates server files (`server.properties`, JSON, YAML, INI, text configs) and resolves environment variables.
- **Complete Installation Lifecycle**: Runs egg install scripts, binds `/mnt/server` and `/mnt/install`, and notifies Panel (`/api/remote/servers/{uuid}/install`) to mark servers as installed.
- **Power Lifecycle & Done-Line Matching**: Implements `starting` -> `running` -> `stopping` -> `offline` states with egg startup done matchers and graceful shutdown.
- **Crash Detection & Auto-Restart**: Detects unexpected process crashes and automatically recovers servers with loop protection.
- **Integrated SFTP Server**: Built-in SFTP server on port `2022` authenticating directly against the Panel (`/api/remote/sftp`).
- **Backups & Node Transfers**: Full support for `.tar.gz` backups, restore, and inter-node server transfers.

## Requirements

- Python 3.10+
- `udocker` installed and available in system `PATH`

## Installation

```bash
git clone https://github.com/SkidTechnologies/pywings.git
cd pywings

pip install -r requirements.txt
```

## Configuration

Copy `config.example.yml` to `config.yml` or paste the configuration generated from the Pterodactyl Panel:

```bash
cp config.example.yml config.yml
```

Edit `config.yml` with your node credentials, Panel URL, and tokens.

## Running

### Development Mode
```bash
python app.py
```

### Production Mode (Gunicorn)
```bash
gunicorn -w 1 -k gthread --threads 10 -b 0.0.0.0:8080 wsgi:app
```
