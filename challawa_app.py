"""
Challawa Pump Monitoring Dashboard
Real-time monitoring of Siemens S7-1200 PLC via Snap7
Monitors Pump 1 and Pump 2 with pressure, speed, and status
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

# Eventlet must be patched before other imports
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO
import snap7
from snap7.util import get_real, get_bool

# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """Application configuration with environment variable overrides."""
    PLC_IP: str = os.getenv("PLC_IP", "192.168.200.20")
    PLC_RACK: int = int(os.getenv("PLC_RACK", "0"))
    PLC_SLOT: int = int(os.getenv("PLC_SLOT", "1"))
    DB_NUMBER: int = int(os.getenv("DB_NUMBER", "39"))
    CYCLE_TIME: float = float(os.getenv("CYCLE_TIME", "0.5"))
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "pump_logs.db")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "5050"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "challawa-pump-monitor-secret")
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Pump names mapping
    PUMP_NAMES: dict = field(default_factory=lambda: {
        1: "LINE 7 MIXER",
        2: "LINE 7 AHU/SYRUP ROOM"
    })

config = Config()

# =============================================================================
# Logging Setup
# =============================================================================

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =============================================================================
# Data Models
# =============================================================================

@dataclass
class PumpData:
    """Data structure for a single pump's state."""
    ready: bool = False
    running: bool = False
    trip: bool = False
    pressure: float = 0.0
    pressure_setpoint: float = 0.0
    speed: float = 0.0
    status: str = "UNKNOWN"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemData:
    """Data structure for the complete system state."""
    alarm: bool = False
    pump1: PumpData = field(default_factory=PumpData)
    pump2: PumpData = field(default_factory=PumpData)
    connected: bool = False

    def to_dict(self) -> dict:
        return {
            "alarm": self.alarm,
            "pump1": self.pump1.to_dict(),
            "pump2": self.pump2.to_dict(),
            "connected": self.connected
        }

# =============================================================================
# Flask Application
# =============================================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25
)

# =============================================================================
# Database Layer
# =============================================================================

class Database:
    """SQLite database operations for pump logging."""

    def __init__(self, path: str):
        self.path = path
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_tables(self) -> None:
        """Initialize database tables."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trip_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    pump_name TEXT NOT NULL,
                    pump_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    pressure REAL,
                    speed REAL,
                    description TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pump_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    pump_id INTEGER NOT NULL,
                    pump_name TEXT NOT NULL,
                    total_trips INTEGER DEFAULT 0,
                    total_runtime_hours REAL DEFAULT 0,
                    last_trip_time TEXT,
                    status TEXT
                )
            """)
            conn.commit()
        logger.info("Database initialized")

    def log_trip_event(
        self,
        pump_id: int,
        pump_name: str,
        event_type: str,
        pressure: float,
        speed: float,
        description: str
    ) -> None:
        """Log a trip event to the database."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trip_events 
                (timestamp, pump_name, pump_id, event_type, pressure, speed, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, pump_name, pump_id, event_type, pressure, speed, description))
            conn.commit()
        logger.warning(f"Trip logged: {pump_name} - {description}")

    def get_trip_events(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        pump_id: Optional[int] = None
    ) -> list[dict]:
        """Query trip events with optional filters."""
        query = "SELECT * FROM trip_events WHERE 1=1"
        params = []

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)
        if end_date:
            query += " AND timestamp <= ?"
            params.append(f"{end_date} 23:59:59")
        if pump_id:
            query += " AND pump_id = ?"
            params.append(pump_id)

        query += " ORDER BY timestamp DESC"

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [{
            "id": r[0],
            "timestamp": r[1],
            "pump_name": r[2],
            "pump_id": r[3],
            "event_type": r[4],
            "pressure": r[5],
            "speed": r[6],
            "description": r[7]
        } for r in rows]

    def get_pump_health_stats(self) -> list[dict]:
        """Get health statistics for all pumps."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT pump_id, pump_name, COUNT(*) as trip_count, MAX(timestamp) as last_trip
                FROM trip_events WHERE event_type = 'TRIP'
                GROUP BY pump_id
            """)
            stats = {row[0]: row for row in cursor.fetchall()}

        health_data = []
        for pump_id, pump_name in config.PUMP_NAMES.items():
            stat = stats.get(pump_id)
            if stat:
                total_trips = stat[2]
                health_data.append({
                    "pump_id": pump_id,
                    "pump_name": pump_name,
                    "total_trips": total_trips,
                    "last_trip": stat[3],
                    "health_score": max(0, 100 - (total_trips * 5))
                })
            else:
                health_data.append({
                    "pump_id": pump_id,
                    "pump_name": pump_name,
                    "total_trips": 0,
                    "last_trip": None,
                    "health_score": 100
                })
        return health_data


# Global database instance
db = Database(config.DATABASE_PATH)

# =============================================================================
# PLC Monitor
# =============================================================================

class PLCMonitor:
    """Siemens S7 PLC monitoring with automatic reconnection."""

    MAX_READ_ERRORS = 3
    RECONNECT_DELAY = 2.0

    def __init__(self):
        self.plc = snap7.client.Client()
        self.connected = False
        self.running = False
        self.last_data: Optional[SystemData] = None
        self.read_count = 0
        self._read_error_count = 0
        self._prev_pump1_trip = False
        self._prev_pump2_trip = False
        self._thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        """Establish connection to PLC."""
        try:
            if self.plc.get_connected():
                self.plc.disconnect()

            self.plc.connect(config.PLC_IP, config.PLC_RACK, config.PLC_SLOT)
            self.connected = self.plc.get_connected()

            if self.connected:
                logger.info(f"Connected to PLC at {config.PLC_IP}")
                self._read_error_count = 0

            return self.connected
        except Exception as e:
            logger.error(f"PLC connection failed: {e}")
            self.connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from PLC."""
        try:
            if self.plc.get_connected():
                self.plc.disconnect()
        except Exception:
            pass
        finally:
            self.connected = False

    def _get_status(self, ready: bool, running: bool, trip: bool) -> str:
        """Determine pump status from flags."""
        if trip:
            return "TRIP"
        if running:
            return "RUNNING"
        if ready:
            return "READY"
        return "UNKNOWN"

    def _create_error_data(self) -> SystemData:
        """Create error state data structure."""
        return SystemData(
            alarm=False,
            pump1=PumpData(status="ERROR"),
            pump2=PumpData(status="ERROR"),
            connected=False
        )

    def read_data(self) -> SystemData:
        """Read current data from PLC DB39."""
        try:
            if not self.plc.get_connected():
                self.connected = False
                return self._create_error_data()

            # Read 28 bytes from DB39
            data = self.plc.db_read(config.DB_NUMBER, 0, 28)
            self._read_error_count = 0

            # Parse Pump 1 (bytes 0-13)
            pump1 = PumpData(
                ready=get_bool(data, 0, 1),
                running=get_bool(data, 0, 2),
                trip=get_bool(data, 0, 3),
                pressure=round(get_real(data, 2), 2),
                pressure_setpoint=round(get_real(data, 6), 2),
                speed=round(get_real(data, 10), 2)
            )
            pump1.status = self._get_status(pump1.ready, pump1.running, pump1.trip)

            # Parse Pump 2 (bytes 14-27)
            pump2 = PumpData(
                running=get_bool(data, 14, 0),
                ready=get_bool(data, 14, 1),
                trip=get_bool(data, 14, 2),
                pressure=round(get_real(data, 16), 2),
                pressure_setpoint=round(get_real(data, 20), 2),
                speed=round(get_real(data, 24), 2)
            )
            pump2.status = self._get_status(pump2.ready, pump2.running, pump2.trip)

            return SystemData(
                alarm=get_bool(data, 0, 0),
                pump1=pump1,
                pump2=pump2,
                connected=True
            )

        except Exception as e:
            self._read_error_count += 1
            logger.warning(f"Read error ({self._read_error_count}/{self.MAX_READ_ERRORS}): {e}")

            if self._read_error_count >= self.MAX_READ_ERRORS:
                logger.error("Too many read errors, forcing reconnection...")
                self.disconnect()
                self._read_error_count = 0

            return self._create_error_data()

    def _check_trip_events(self, data: SystemData) -> None:
        """Check for and log new trip events."""
        if data.pump1.trip and not self._prev_pump1_trip:
            db.log_trip_event(
                1, config.PUMP_NAMES[1], "TRIP",
                data.pump1.pressure, data.pump1.speed,
                "Pump 1 tripped - fault detected"
            )

        if data.pump2.trip and not self._prev_pump2_trip:
            db.log_trip_event(
                2, config.PUMP_NAMES[2], "TRIP",
                data.pump2.pressure, data.pump2.speed,
                "Pump 2 tripped - fault detected"
            )

        self._prev_pump1_trip = data.pump1.trip
        self._prev_pump2_trip = data.pump2.trip

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self.running:
            if not self.connected:
                if not self.connect():
                    time.sleep(self.RECONNECT_DELAY)
                    continue

            data = self.read_data()
            self.last_data = data

            if data.connected:
                self.read_count += 1
                self._check_trip_events(data)

                if self.read_count % 20 == 0:
                    logger.debug(
                        f"Read #{self.read_count}: "
                        f"P1={data.pump1.pressure:.2f}bar, "
                        f"P2={data.pump2.pressure:.2f}bar"
                    )

            try:
                socketio.emit("pump_data", data.to_dict())
            except Exception as e:
                logger.error(f"Socket emit error: {e}")

            time.sleep(config.CYCLE_TIME)

    def start(self) -> None:
        """Start the monitoring thread."""
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("PLC monitoring started")

    def stop(self) -> None:
        """Stop monitoring and disconnect."""
        self.running = False
        self.disconnect()
        logger.info("PLC monitoring stopped")


# Global monitor instance
monitor = PLCMonitor()

# =============================================================================
# Routes
# =============================================================================

@app.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Get current pump status."""
    if monitor.connected and monitor.last_data:
        return jsonify(monitor.last_data.to_dict())
    return jsonify(monitor.read_data().to_dict())


@app.route("/api/debug")
def api_debug():
    """Debug endpoint for system status."""
    return jsonify({
        "plc_connected": monitor.connected,
        "read_count": monitor.read_count,
        "read_error_count": monitor._read_error_count,
        "last_data": monitor.last_data.to_dict() if monitor.last_data else None,
        "running": monitor.running
    })


@app.route("/api/reports/events")
def api_events():
    """Query trip events."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    pump_id = request.args.get("pump_id", type=int)
    return jsonify(db.get_trip_events(start_date, end_date, pump_id))


@app.route("/api/reports/health")
def api_health():
    """Get pump health statistics."""
    return jsonify(db.get_pump_health_stats())


@app.route("/api/reports/download")
def api_download_report():
    """Generate and download HTML report."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    pump_id = request.args.get("pump_id", type=int)

    events = db.get_trip_events(start_date, end_date, pump_id)
    health = db.get_pump_health_stats()

    html = render_template(
        "report.html",
        events=events,
        health=health,
        start_date=start_date or "All time",
        end_date=end_date or "Present",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": "attachment; filename=pump_report.html"}
    )

# =============================================================================
# Socket Events
# =============================================================================

@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    logger.info("Client connected")
    if monitor.last_data:
        socketio.emit("pump_data", monitor.last_data.to_dict())


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    logger.info("Client disconnected")

# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """Application entry point."""
    monitor.start()

    print("\n" + "=" * 60)
    print("  🌊 CHALLAWA PUMP MONITORING DASHBOARD")
    print("=" * 60)
    print(f"  PLC Address: {config.PLC_IP}")
    print(f"  Web Interface: http://localhost:{config.WEB_PORT}")
    print(f"  Monitoring: {', '.join(config.PUMP_NAMES.values())}")
    print(f"  Update Rate: {config.CYCLE_TIME}s")
    print(f"  Database: {config.DATABASE_PATH}")
    print("=" * 60 + "\n")

    try:
        socketio.run(app, host="0.0.0.0", port=config.WEB_PORT, debug=config.DEBUG)
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
