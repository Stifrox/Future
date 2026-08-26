"""Local-network status for Anycubic Kobra 3 / S1 generation printers.

Uses the printer's own LAN Mode (Settings -> Network -> LAN Mode on the
printer's touchscreen) instead of Anycubic's cloud account, so no API key or
login is ever required. Backed by the open-source `anycubic-cloud-api`
package's LAN handshake + local MQTT client.
"""
import asyncio
import os
from typing import Dict


def _lan_host() -> str:
    host = os.getenv("ANYCUBIC_LAN_HOST", "").strip()
    if not host:
        raise RuntimeError("ANYCUBIC_LAN_HOST is not configured.")
    return host


async def _fetch_lan_report_async(host: str, wait_seconds: float) -> Dict[str, dict]:
    import aiohttp
    from anycubic_cloud_api.lan import AnycubicLANClient, AnycubicLANHandshake

    async with aiohttp.ClientSession() as session:
        broker = await AnycubicLANHandshake(session, host).async_authenticate()

    reports: Dict[str, dict] = {}

    def _on_message(_topic, message_type, payload):
        reports[message_type] = payload

    client = AnycubicLANClient(broker, _on_message)
    await client.async_connect()
    try:
        client.query_all()
        await asyncio.sleep(wait_seconds)
    finally:
        await client.async_disconnect()

    return reports


def fetch_lan_print_status(wait_seconds: float = 3.0) -> Dict[str, object]:
    """Poll the printer directly over the local network and return a status snapshot."""
    host = _lan_host()
    reports = asyncio.run(_fetch_lan_report_async(host, wait_seconds))

    info = (reports.get("info") or {}).get("data") or {}
    temp = info.get("temp") or {}
    project = info.get("project") or {}

    printing = bool(project)
    remain_time = project.get("remain_time")
    eta = f"{int(remain_time)}m" if isinstance(remain_time, (int, float)) else "--"

    return {
        "printing": printing,
        "file": (project.get("filename") if printing else None) or "No active print",
        "layer": project.get("curr_layer", 0) or 0,
        "layer_total": project.get("total_layers", 0) or 0,
        "progress_pct": project.get("progress", 0) or 0,
        "eta": eta,
        "nozzle_temp": temp.get("curr_nozzle_temp", 0) or 0,
        "hotbed_temp": temp.get("curr_hotbed_temp", 0) or 0,
        "printer_name": info.get("printerName") or "Anycubic printer",
        "backend": "anycubic_lan",
    }
