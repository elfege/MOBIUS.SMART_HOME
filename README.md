# 0_MOBIUS.SMART_HOME

Python/Flask application for migrating Hubitat Groovy apps to a web-based automation system with multi-instance support.

## Features

- **Multi-Instance Architecture**: Create multiple instances of each app type (e.g., "Advanced Lights - Office", "Advanced Lights - Bedroom")
- **Hubitat Integration**: Connects to Hubitat hubs via Maker API
- **Device Caching**: PostgreSQL-backed caching reduces API polling
- **Event-Driven**: Webhook-based event routing to app instances
- **Modern UI**: Dashboard with instance management, device picker wizard

## First App: Advanced Motion Lighting

Ported from Hubitat Groovy with full feature parity:
- Motion-triggered light control
- Memoization (remembers user overrides)
- Mode-specific timeouts and dimming
- Illuminance threshold checking
- Pause/resume functionality

## Quick Start

1. **Prerequisites**
   - Docker and Docker Compose
   - AWS CLI configured with profile 'personal'
   - Hubitat hub with Maker API installed

2. **Deploy**
   ```bash
   cd /home/elfege/0_MOBIUS.SMART_HOME
   ./deploy.sh
   ```

3. **Access**
   - Dashboard: http://<LAN_IP>:5001/
   - API: http://<LAN_IP>:5001/api/

## Architecture

### Services (Docker Compose)

| Service | Port | Purpose |
|---------|------|---------|
| smart-home | 5001 | Flask application |
| postgres | 5432 | Database |
| postgrest | 3001 | REST API from schema |
| nginx | 8082 | Reverse proxy |

### Key Directories

```
0_MOBIUS.SMART_HOME/
├── app.py              # Flask entry point
├── apps/               # App type implementations
├── services/           # Core services
├── models/             # Pydantic models
├── templates/          # Jinja2 templates
├── static/             # CSS/JS assets
├── config/             # Configuration files
└── psql/               # Database schema
```

### Multi-Instance Design

```
app_types (blueprints)
    ↓
app_instances (user-created automations)
    ↓
device_subscriptions (event routing)
```

## API Endpoints

### Instances
- `GET /api/instances` - List all instances
- `POST /api/instances` - Create instance
- `PUT /api/instances/{id}` - Update instance
- `DELETE /api/instances/{id}` - Delete instance
- `POST /api/instances/{id}/pause` - Pause instance
- `POST /api/instances/{id}/resume` - Resume instance

### Devices
- `GET /api/devices` - List devices (filter by capability)
- `GET /api/devices/{id}` - Get device details

### Webhooks (from Hubitat)
- `POST /api/webhook/event` - Device events
- `POST /api/webhook/mode` - Mode changes

## Configuration

### Hubitat Connection

Edit `config/settings.json`:
```json
{
    "hubitat": {
        "primary": {
            "hub_ip": "<LAN_IP>",
            "app_number": "268",
            "token_env": "HUBITAT_API_TOKEN_MAIN"
        }
    }
}
```

### AWS Secrets

Tokens stored in AWS Secrets Manager under secret name `HUBITAT`.
`start.sh` maps AWS names → app-standardized names:
- `HUBITAT_API_TOKEN_MAIN` - Primary hub token (from `HUBITAT_API_TOKEN_4`)
- `HUBITAT_API_TOKEN_OTHER_HUB_1-3` - Other hubs (from `HUBITAT_API_TOKEN_1-3`)

## Development

### Local Development (without Docker)

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export HUBITAT_API_TOKEN_MAIN="your-token"
export POSTGREST_URL="http://localhost:3001"

# Run FastAPI
uvicorn app:app --host 0.0.0.0 --port 5000 --reload
```

### Adding New App Types

1. Create module in `apps/your_app_type/`
2. Extend `BaseApp` class
3. Implement required methods: `initialize()`, `on_event()`, `master()`, `get_settings_schema()`, `get_device_categories()`
4. Register in `apps/app_registry.py`

## License

Personal project - not for distribution.
