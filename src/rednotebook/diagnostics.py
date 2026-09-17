"""Local diagnostics: no navigation, model request, grant creation or automatic resume."""

from rednotebook.errors import DomainError
from rednotebook.research.config import load_config


def diagnose(db, profile, config):
    from playwright.sync_api import sync_playwright

    # Not used from the async MCP loop; the async counterpart supplies the path.
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    return local_status(db, profile, config, executable)


def local_status(db, profile, config, executable):
    from pathlib import Path

    from rednotebook.browser_control import AccessGate

    try:
        model = load_config(config)
        model_status = {"state": "configured", "model": model.model, "connectivity": "not_probed"}
    except DomainError as exc:
        model_status = {"state": "unavailable", "error_code": exc.code}
    except Exception:
        model_status = {"state": "unavailable", "error_code": "model_config_invalid"}
    allowed, blocked = db.allowed_sources()
    sources = []
    for source in allowed:
        row = db.conn.execute("SELECT expires_at FROM sources WHERE id=?", (source,)).fetchone()
        grant = db.require_source(source)
        sources.append(
            {
                "source_id": source,
                "expires_at": row[0],
                "automated_access": grant.permissions.automated_access,
            }
        )
    return {
        "schema_version": db.conn.execute("PRAGMA user_version").fetchone()[0],
        "chromium_installed": Path(executable).is_file(),
        "browser_install_command": "uv run playwright install chromium",
        "model": model_status,
        "sources": sources,
        "blocked_sources": blocked,
        "browser_access": AccessGate(profile).status(),
        "source_setup": "rednotebook source register --file <explicit-grant.json>",
        "network_access": False,
    }
