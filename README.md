# VMDEX - ECHO

A professional Discord bot for single-user virtual machine management using Docker containers.

## Features

- Single VM deployment and management
- Docker container-based virtualization
- Ubuntu 24.04 as the default operating system
- SSH access via direct connection and tmate sessions
- Clean, professional Discord interface with cyan-themed embeds
- Resource allocation (Memory, CPU, Disk)

## Commands

| Command | Description |
|---------|-------------|
| `/deploy` | Deploy a new virtual machine |
| `/start` | Start the virtual machine |
| `/stop` | Stop the virtual machine |
| `/restart` | Restart the virtual machine |
| `/connect` | Get connection details for the VM |
| `/status` | View VM status and resource usage |
| `/delete` | Delete the virtual machine |
| `/help` | Show all available commands |

## Requirements

- Python 3.11+
- Docker Engine installed and running
- Discord Bot Token
- Linux host system (recommended)

## Installation

1. Clone or download this repository

2. Install Python dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file with the following variables:
```env
DISCORD_TOKEN=your_discord_bot_token
OWNER_ID=your_discord_user_id
HOST_IP=your_server_public_ip
```

4. Ensure Docker is installed and running:
```bash
sudo systemctl start docker
sudo systemctl enable docker
```

5. Run the bot:
```bash
python vmdex.py
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DISCORD_TOKEN` | Your Discord bot token | Yes |
| `OWNER_ID` | Your Discord user ID (only this user can control the VM) | Yes |
| `HOST_IP` | Server public IP for SSH connections (auto-detected if not set) | No |

## VM Specifications

- Default OS: Ubuntu 24.04
- Memory: 1-64 GB (configurable)
- CPU: 1-16 cores (configurable)
- Disk: 10-500 GB (configurable)
- SSH Port: Randomly assigned (20000-30000)

## Systemd Service (Optional)

To run VMDEX as a system service:

1. Create a service file:
```bash
sudo nano /etc/systemd/system/vmdex.service
```

2. Add the following content:
```ini
[Unit]
Description=VMDEX Discord Bot
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/vmdex
ExecStart=/usr/bin/python3 /path/to/vmdex/bot.py
Restart=always
RestartSec=30
Environment="DOCKER_HOST=unix:///var/run/docker.sock"

[Install]
WantedBy=multi-user.target
```

3. Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable vmdex.service
sudo systemctl start vmdex.service
```

4. Check status:
```bash
sudo systemctl status vmdex.service
```

## License

This project is provided as-is for educational and personal use.

---
Coded by @Orbred
