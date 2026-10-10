"""Local-only FastAPI dashboard. Run with:
    uvicorn app.dashboard.main:app --reload --port 8000
No external services, no paid tiers, nothing leaves your machine except the
outbound requests the collectors themselves make to public free APIs.
"""
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.db import get_conn, init_db

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Crypto Shutdown Radar")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

init_db()


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    with get_conn() as conn:
        total_coins = conn.execute("SELECT COUNT(*) c FROM coins WHERE is_active=1").fetchone()["c"]
        verified_accounts = conn.execute(
            "SELECT COUNT(*) c FROM official_accounts WHERE platform='x' AND verification_status='VERIFIED'"
        ).fetchone()["c"]
        unverified_accounts = conn.execute(
            "SELECT COUNT(*) c FROM official_accounts WHERE platform='x' AND verification_status='UNVERIFIED'"
        ).fetchone()["c"]
        accounts_ok = conn.execute("SELECT COUNT(*) c FROM account_monitor_state WHERE status='OK'").fetchone()["c"]
        accounts_unreachable = conn.execute(
            "SELECT COUNT(*) c FROM account_monitor_state WHERE status='UNREACHABLE'"
        ).fetchone()["c"]
        last_rss = conn.execute("SELECT * FROM scheduler_state WHERE job_name='rss_cycle'").fetchone()
        last_x = conn.execute("SELECT * FROM scheduler_state WHERE job_name='x_cycle'").fetchone()
        sev_counts = {
            row["severity"]: row["c"]
            for row in conn.execute("SELECT severity, COUNT(*) c FROM events GROUP BY severity").fetchall()
        }
        total_events = sum(sev_counts.values())

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "total_coins": total_coins,
            "verified_accounts": verified_accounts,
            "unverified_accounts": unverified_accounts,
            "accounts_ok": accounts_ok,
            "accounts_unreachable": accounts_unreachable,
            "last_rss": last_rss,
            "last_x": last_x,
            "sev_counts": sev_counts,
            "total_events": total_events,
        },
    )


@app.get("/coins", response_class=HTMLResponse)
def coins(request: Request, q: str = Query("")):
    with get_conn() as conn:
        if q:
            rows = conn.execute(
                "SELECT * FROM coins WHERE is_active=1 AND (name LIKE ? OR symbol LIKE ?) ORDER BY market_cap_rank ASC LIMIT 200",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM coins WHERE is_active=1 ORDER BY market_cap_rank ASC LIMIT 200").fetchall()
    return templates.TemplateResponse("coins.html", {"request": request, "coins": rows, "q": q})


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, status: str = Query("")):
    query = """
        SELECT oa.*, c.name AS coin_name, c.symbol, ams.status AS monitor_status, ams.last_error
        FROM official_accounts oa
        LEFT JOIN coins c ON c.coingecko_id = oa.coingecko_id
        LEFT JOIN account_monitor_state ams ON ams.account_id = oa.id
        WHERE oa.platform='x'
    """
    params = []
    if status:
        query += " AND (ams.status = ? OR (? = 'NEVER_CHECKED' AND ams.status IS NULL))"
        params.extend([status, status])
    query += " ORDER BY c.market_cap_rank ASC LIMIT 500"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return templates.TemplateResponse("accounts.html", {"request": request, "accounts": rows, "status": status})


@app.get("/events", response_class=HTMLResponse)
def events(request: Request, severity: str = Query("")):
    query = "SELECT * FROM events"
    params = []
    if severity:
        query += " WHERE severity = ?"
        params.append(severity)
    query += " ORDER BY detection_timestamp DESC LIMIT 200"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return templates.TemplateResponse("events.html", {"request": request, "events": rows, "severity": severity})


@app.get("/alerts", response_class=HTMLResponse)
def alerts(request: Request):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.*, e.event_label, e.severity AS event_severity, e.affected_entity, e.coingecko_id
            FROM alerts a JOIN events e ON e.id = a.event_id
            ORDER BY a.sent_at DESC LIMIT 200
            """
        ).fetchall()
    return templates.TemplateResponse("alerts.html", {"request": request, "alerts": rows})


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM source_health ORDER BY source_key").fetchall()
    return templates.TemplateResponse("sources.html", {"request": request, "sources": rows})


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    from app import config as cfg

    return templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "target_poll_minutes": cfg.TARGET_POLL_MINUTES,
            "universe_size": cfg.UNIVERSE_SIZE,
            "news_sources": cfg.NEWS_RSS_SOURCES,
            "announcement_sources": cfg.ANNOUNCEMENT_RSS_SOURCES,
            "telegram_configured": bool(cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID),
        },
    )
