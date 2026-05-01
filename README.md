# forex-backend

FastAPI multi-user backend wrapping [`forex-agent`](https://github.com/sppidy/janus-forex-agent). Part of [`janus`](https://github.com/sppidy/janus).

- FastAPI + uvicorn
- **PostgreSQL**-backed (multi-user) — users, portfolios, positions, orders, trades, audit logs
- Docker / Docker Compose deployment
- Admin vs user roles, X-API-Key header auth

## Quick start (Docker)

```bash
cp .env.example .env                                      # set POSTGRES_PASSWORD + ADMIN_API_KEY
docker compose up -d
# Server on http://localhost:8445
```

## Quick start (local Python)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# Set POSTGRES_* env vars to point at a local Postgres
python api_server.py
```

## Endpoints (prefix `/api/`)

Same surface as `nse-backend` plus:

- `admin/users` — user management (admin role)
- `strategies` — list configured strategies
- `market-regime`, `candles` — market data passthroughs

All endpoints require `X-API-Key`. Admin endpoints additionally require an admin-role key.

## Schema

See `users.py` for the SQLAlchemy models — users, portfolios, positions, orders, trades, audit logs. Everything is user-scoped.

## License

[Apache-2.0](LICENSE). Contributing guidelines and security policy live in the [super-repo](https://github.com/sppidy/janus).
