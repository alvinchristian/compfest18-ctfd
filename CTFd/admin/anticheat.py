import json

from flask import redirect, render_template, request, url_for

from CTFd.admin import admin
from CTFd.models import AnticheatAlerts
from CTFd.utils import get_config, set_config
from CTFd.utils.decorators import admins_only
from CTFd.utils.anticheat import is_enabled


@admin.route("/admin/anticheat")
@admins_only
def anticheat_listing():
    alerts = (
        AnticheatAlerts.query.order_by(AnticheatAlerts.created_at.desc())
        .limit(200)
        .all()
    )
    for alert in alerts:
        alert.evidence_json = json.dumps(alert.evidence, indent=2, sort_keys=True, default=str)

    return render_template(
        "admin/anticheat.html",
        alerts=alerts,
        solve_window_minutes=get_config("anticheat_solve_window_minutes") or "20",
        anticheat_enabled=is_enabled(),
        webhook_configured=bool(get_config("anticheat_discord_webhook_url")),
    )


@admin.route("/admin/anticheat/config", methods=["POST"])
@admins_only
def anticheat_config():
    window = request.form.get("solve_window_minutes", "5").strip()
    try:
        window = max(1, int(window))
    except ValueError:
        window = 5

    set_config("anticheat_solve_window_minutes", str(window))
    set_config("anticheat_enabled", "1" if request.form.get("enabled") == "1" else "0")
    webhook_url = request.form.get("discord_webhook_url", "").strip()
    if webhook_url:
        set_config("anticheat_discord_webhook_url", webhook_url)
    elif request.form.get("clear_webhook"):
        set_config("anticheat_discord_webhook_url", "")

    return redirect(url_for("admin.anticheat_listing"))


@admin.route("/admin/anticheat/<int:alert_id>/status", methods=["POST"])
@admins_only
def anticheat_status(alert_id):
    alert = AnticheatAlerts.query.get_or_404(alert_id)
    status = request.form.get("status", "open")
    if status in {"open", "reviewed", "dismissed"}:
        alert.status = status
        from CTFd.models import db

        db.session.commit()
    return redirect(url_for("admin.anticheat_listing"))
