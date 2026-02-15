"""
Serial / USB Device Skill
Communicate with Arduino, microcontrollers, sensors, and other USB serial devices.
Inspired by PyGPT's serial port plugin.

Dependencies: pyserial (auto-installed on first use).
"""

import json
import logging
import os
import subprocess
import sys
import time
from typing import Optional, Dict

logger = logging.getLogger("libre_bird.skills.serial_usb")

_serial_mod = None
_open_ports: Dict[str, object] = {}  # path -> serial.Serial instance


def _ensure_pyserial():
    """Lazy-load pyserial."""
    global _serial_mod
    if _serial_mod is not None:
        return _serial_mod

    try:
        import serial
        import serial.tools.list_ports
    except ImportError:
        logger.info("Installing pyserial...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pyserial"])
        import serial
        import serial.tools.list_ports

    _serial_mod = serial
    return serial


def tool_serial_list_ports(args: dict) -> dict:
    """List all available serial/USB ports on the system."""
    _ensure_pyserial()
    import serial.tools.list_ports

    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({
            "device": port.device,
            "description": port.description,
            "manufacturer": port.manufacturer or "Unknown",
            "serial_number": port.serial_number or "",
            "vid": f"0x{port.vid:04x}" if port.vid else None,
            "pid": f"0x{port.pid:04x}" if port.pid else None,
        })

    return {"ports": ports, "count": len(ports)}


def _get_or_open(port: str, baudrate: int = 9600, timeout: float = 2.0):
    """Get an existing connection or open a new one."""
    serial = _ensure_pyserial()

    if port in _open_ports:
        conn = _open_ports[port]
        if conn.is_open:
            return conn
        # Closed, remove it
        del _open_ports[port]

    conn = serial.Serial(port, baudrate=baudrate, timeout=timeout)
    _open_ports[port] = conn
    time.sleep(1.5)  # Wait for Arduino reset
    return conn


def tool_serial_send(args: dict) -> dict:
    """Send data to a serial/USB device."""
    port = args.get("port", "")
    data = args.get("data", "")
    baudrate = int(args.get("baudrate", 9600))
    newline = args.get("newline", True)

    if not port:
        return {"error": "port is required (e.g. '/dev/cu.usbmodem14101')"}
    if not data:
        return {"error": "data is required"}

    try:
        conn = _get_or_open(port, baudrate)
        message = data
        if newline and not data.endswith("\n"):
            message += "\n"
        conn.write(message.encode("utf-8"))
        conn.flush()

        return {
            "success": True,
            "port": port,
            "sent": data,
            "bytes": len(message),
        }
    except Exception as e:
        return {"error": str(e)}


def tool_serial_read(args: dict) -> dict:
    """Read data from a serial/USB device."""
    port = args.get("port", "")
    baudrate = int(args.get("baudrate", 9600))
    timeout = float(args.get("timeout", 2.0))
    lines = int(args.get("lines", 5))

    if not port:
        return {"error": "port is required"}

    try:
        conn = _get_or_open(port, baudrate, timeout)

        received = []
        for _ in range(lines):
            line = conn.readline().decode("utf-8", errors="replace").strip()
            if line:
                received.append(line)
            elif len(received) > 0:
                break  # Stop on empty line after getting some data

        return {
            "port": port,
            "lines_read": len(received),
            "data": received,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_serial_monitor(args: dict) -> dict:
    """Read from a serial port for a duration, capturing all output. Good for monitoring sensor data."""
    port = args.get("port", "")
    baudrate = int(args.get("baudrate", 9600))
    duration = float(args.get("duration_seconds", 5.0))

    if not port:
        return {"error": "port is required"}

    duration = min(duration, 30)  # Cap at 30 seconds

    try:
        conn = _get_or_open(port, baudrate, timeout=1.0)

        received = []
        start = time.time()
        while time.time() - start < duration:
            line = conn.readline().decode("utf-8", errors="replace").strip()
            if line:
                received.append({
                    "time": round(time.time() - start, 2),
                    "data": line,
                })

        return {
            "port": port,
            "duration_seconds": round(time.time() - start, 2),
            "messages": len(received),
            "data": received[-50:],  # Last 50 entries
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "serial_list_ports",
            "description": "List all available serial/USB ports on the system. Shows device path, description, and manufacturer.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serial_send",
            "description": "Send a text command to a serial/USB device like an Arduino.",
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Serial port path (e.g. '/dev/cu.usbmodem14101')"},
                    "data": {"type": "string", "description": "Data to send"},
                    "baudrate": {"type": "integer", "description": "Baud rate (default 9600)"},
                    "newline": {"type": "boolean", "description": "Append newline (default true)"},
                },
                "required": ["port", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serial_read",
            "description": "Read lines of data from a serial/USB device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Serial port path"},
                    "baudrate": {"type": "integer", "description": "Baud rate (default 9600)"},
                    "lines": {"type": "integer", "description": "Max lines to read (default 5)"},
                    "timeout": {"type": "number", "description": "Read timeout in seconds (default 2)"},
                },
                "required": ["port"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serial_monitor",
            "description": "Monitor a serial port for a duration, capturing all incoming data. Good for sensor readings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {"type": "string", "description": "Serial port path"},
                    "baudrate": {"type": "integer", "description": "Baud rate (default 9600)"},
                    "duration_seconds": {"type": "number", "description": "How long to monitor (default 5, max 30)"},
                },
                "required": ["port"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "serial_list_ports": tool_serial_list_ports,
    "serial_send": tool_serial_send,
    "serial_read": tool_serial_read,
    "serial_monitor": tool_serial_monitor,
}
