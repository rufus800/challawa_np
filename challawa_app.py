"""
Challawa Pump Monitoring Dashboard
Real-time monitoring of Siemens S7-1200 PLC via Snap7
Monitors Pump 1 and Pump 2 with pressure, speed, and status
"""

import eventlet
eventlet.monkey_patch()

# Now import all other modules
from flask import Flask, render_template, jsonify, request, send_file
from flask_socketio import SocketIO
import snap7
from snap7.util import get_real, get_bool
import time
import threading
import sqlite3
from datetime import datetime, timedelta
import io
import os

# Try to use eventlet for better async support
try:
    import eventlet
    eventlet.monkey_patch()
    async_mode = 'eventlet'
    print("Using eventlet async mode")
except ImportError:
    async_mode = 'threading'
    print("Using threading async mode (install eventlet for better performance)")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'challawa-pump-monitor-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode, ping_timeout=60, ping_interval=25)

# PLC Configuration
PLC_IP = "192.168.200.20"
PLC_RACK = 0
PLC_SLOT = 1
DB_NUMBER = 39
CYCLE_TIME = 0.5  # 500ms
DATABASE_PATH = 'pump_logs.db'
WEB_PORT = 5050  # Changed from 5000 to avoid conflict

# Database initialization
def init_database():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute('''
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
    ''')
    cursor.execute('''
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
    ''')
    conn.commit()
    conn.close()

def log_trip_event(pump_id, pump_name, event_type, pressure, speed, description):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
        INSERT INTO trip_events (timestamp, pump_name, pump_id, event_type, pressure, speed, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (timestamp, pump_name, pump_id, event_type, pressure, speed, description))
    conn.commit()
    conn.close()

def get_trip_events(start_date=None, end_date=None, pump_id=None):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    query = 'SELECT * FROM trip_events WHERE 1=1'
    params = []
    if start_date:
        query += ' AND timestamp >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND timestamp <= ?'
        params.append(end_date + ' 23:59:59')
    if pump_id:
        query += ' AND pump_id = ?'
        params.append(pump_id)
    query += ' ORDER BY timestamp DESC'
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_pump_health_stats():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Get trip counts per pump
    cursor.execute('''
        SELECT pump_id, pump_name, COUNT(*) as trip_count, MAX(timestamp) as last_trip
        FROM trip_events WHERE event_type = 'TRIP'
        GROUP BY pump_id
    ''')
    stats = cursor.fetchall()
    conn.close()
    
    pump_names = {
        1: 'LINE 7 MIXER',
        2: 'LINE 7 AHU/SYRUP ROOM'
    }
    
    health_data = []
    for pump_id in [1, 2]:
        pump_stat = next((s for s in stats if s[0] == pump_id), None)
        if pump_stat:
            health_data.append({
                'pump_id': pump_id,
                'pump_name': pump_names.get(pump_id, f'Pump {pump_id}'),
                'total_trips': pump_stat[2],
                'last_trip': pump_stat[3],
                'health_score': max(0, 100 - (pump_stat[2] * 5))  # Simple health calculation
            })
        else:
            health_data.append({
                'pump_id': pump_id,
                'pump_name': pump_names.get(pump_id, f'Pump {pump_id}'),
                'total_trips': 0,
                'last_trip': None,
                'health_score': 100
            })
    return health_data

class DualPumpMonitor:
    def __init__(self):
        self.plc = snap7.client.Client()
        self.connected = False
        self.running = False
        self.prev_pump1_trip = False
        self.prev_pump2_trip = False
        self.pump_names = {
            1: 'LINE 7 MIXER',
            2: 'LINE 7 AHU/SYRUP ROOM'
        }
        self.read_error_count = 0
        self.max_read_errors = 3
        self.last_data = None  # Store last successful read
        self.read_count = 0  # Count successful reads
        
    def connect(self):
        try:
            # Disconnect first if already connected
            if self.plc.get_connected():
                self.plc.disconnect()
            self.plc.connect(PLC_IP, PLC_RACK, PLC_SLOT)
            self.connected = self.plc.get_connected()
            if self.connected:
                print(f"✓ Connected to PLC at {PLC_IP}")
                self.read_error_count = 0
            return self.connected
        except Exception as e:
            print(f"✗ Connection failed: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        try:
            if self.plc.get_connected():
                self.plc.disconnect()
            self.connected = False
        except:
            pass
    
    def read_db39(self):
        """Read data from DB39"""
        try:
            # Check connection before reading
            if not self.plc.get_connected():
                self.connected = False
                return self._get_error_data()
            
            # Read 28 bytes from DB39 starting at byte 0
            data = self.plc.db_read(DB_NUMBER, 0, 28)
            
            # Reset error count on successful read
            self.read_error_count = 0
            
            # Parse Pump 1 data
            alarm = get_bool(data, 0, 0)
            pump1_ready = get_bool(data, 0, 1)
            pump1_running = get_bool(data, 0, 2)
            pump1_trip = get_bool(data, 0, 3)
            pump1_pressure = get_real(data, 2)
            pump1_pressure_sp = get_real(data, 6)
            pump1_speed = get_real(data, 10)
            
            # Parse Pump 2 data
            pump2_running = get_bool(data, 14, 0)
            pump2_ready = get_bool(data, 14, 1)
            pump2_trip = get_bool(data, 14, 2)
            pump2_pressure = get_real(data, 16)
            pump2_pressure_sp = get_real(data, 20)
            pump2_speed = get_real(data, 24)
            
            # Determine pump statuses
            pump1_status = self._get_status(pump1_ready, pump1_running, pump1_trip)
            pump2_status = self._get_status(pump2_ready, pump2_running, pump2_trip)
            
            return {
                "alarm": alarm,
                "pump1": {
                    "ready": pump1_ready,
                    "running": pump1_running,
                    "trip": pump1_trip,
                    "pressure": round(pump1_pressure, 2),
                    "pressure_setpoint": round(pump1_pressure_sp, 2),
                    "speed": round(pump1_speed, 2),
                    "status": pump1_status
                },
                "pump2": {
                    "ready": pump2_ready,
                    "running": pump2_running,
                    "trip": pump2_trip,
                    "pressure": round(pump2_pressure, 2),
                    "pressure_setpoint": round(pump2_pressure_sp, 2),
                    "speed": round(pump2_speed, 2),
                    "status": pump2_status
                },
                "connected": True
            }
        except Exception as e:
            self.read_error_count += 1
            print(f"Read error ({self.read_error_count}/{self.max_read_errors}): {e}")
            
            # If too many consecutive errors, force reconnect
            if self.read_error_count >= self.max_read_errors:
                print("Too many read errors, forcing reconnection...")
                self.disconnect()
                self.connected = False
                self.read_error_count = 0
            
            return self._get_error_data()
    
    def _get_status(self, ready, running, trip):
        """Determine mutually exclusive status"""
        if trip:
            return "TRIP"
        elif running:
            return "RUNNING"
        elif ready:
            return "READY"
        return "UNKNOWN"
    
    def _get_error_data(self):
        """Return error data structure"""
        return {
            "alarm": False,
            "pump1": {
                "ready": False,
                "running": False,
                "trip": False,
                "pressure": 0.0,
                "pressure_setpoint": 0.0,
                "speed": 0.0,
                "status": "ERROR"
            },
            "pump2": {
                "ready": False,
                "running": False,
                "trip": False,
                "pressure": 0.0,
                "pressure_setpoint": 0.0,
                "speed": 0.0,
                "status": "ERROR"
            },
            "connected": False
        }
    
    def monitor_loop(self):
        """Continuous monitoring loop"""
        reconnect_delay = 2  # seconds between reconnect attempts
        
        while self.running:
            if not self.connected:
                if not self.connect():
                    # Wait before trying to reconnect
                    time.sleep(reconnect_delay)
                    continue
            
            if self.connected:
                data = self.read_db39()
                
                # Store last data for debugging
                self.last_data = data
                
                # Only process data if we got valid data
                if data.get('connected', False):
                    self.read_count += 1
                    # Print status every 20 reads (10 seconds)
                    if self.read_count % 20 == 0:
                        print(f"📊 Data read #{self.read_count}: P1={data['pump1']['pressure']:.2f}bar, P2={data['pump2']['pressure']:.2f}bar")
                    
                    # Check for trip events and log them
                    if data['pump1']['trip'] and not self.prev_pump1_trip:
                        log_trip_event(
                            1, self.pump_names[1], 'TRIP',
                            data['pump1']['pressure'], data['pump1']['speed'],
                            'Pump 1 tripped - fault detected'
                        )
                    if data['pump2']['trip'] and not self.prev_pump2_trip:
                        log_trip_event(
                            2, self.pump_names[2], 'TRIP',
                            data['pump2']['pressure'], data['pump2']['speed'],
                            'Pump 2 tripped - fault detected'
                        )
                    
                    # Update previous states
                    self.prev_pump1_trip = data['pump1']['trip']
                    self.prev_pump2_trip = data['pump2']['trip']
                
                # Always emit data (even error state)
                try:
                    socketio.emit('pump_data', data)
                except Exception as e:
                    print(f"Socket emit error: {e}")
            
            time.sleep(CYCLE_TIME)
    
    def start(self):
        self.running = True
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()
    
    def stop(self):
        self.running = False
        self.disconnect()

# Global monitor instance
monitor = DualPumpMonitor()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    if monitor.connected:
        return jsonify(monitor.read_db39())
    return jsonify(monitor._get_error_data())

@app.route('/api/debug')
def get_debug():
    """Debug endpoint to check system status"""
    return jsonify({
        'plc_connected': monitor.connected,
        'read_count': monitor.read_count,
        'read_error_count': monitor.read_error_count,
        'last_data': monitor.last_data,
        'running': monitor.running
    })

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    if monitor.connected and monitor.last_data:
        socketio.emit('pump_data', monitor.last_data)
    elif monitor.connected:
        socketio.emit('pump_data', monitor.read_db39())

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

# Reports API endpoints
@app.route('/api/reports/events')
def get_events():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    pump_id = request.args.get('pump_id')
    if pump_id:
        pump_id = int(pump_id)
    
    events = get_trip_events(start_date, end_date, pump_id)
    return jsonify([{
        'id': e[0],
        'timestamp': e[1],
        'pump_name': e[2],
        'pump_id': e[3],
        'event_type': e[4],
        'pressure': e[5],
        'speed': e[6],
        'description': e[7]
    } for e in events])

@app.route('/api/reports/health')
def get_health():
    return jsonify(get_pump_health_stats())

@app.route('/api/reports/download')
def download_report():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    pump_id = request.args.get('pump_id')
    if pump_id:
        pump_id = int(pump_id)
    
    events = get_trip_events(start_date, end_date, pump_id)
    health = get_pump_health_stats()
    
    # Generate HTML report for PDF conversion
    html_content = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Pump Trip Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; }}
            h1 {{ color: #667eea; border-bottom: 2px solid #667eea; padding-bottom: 10px; }}
            h2 {{ color: #333; margin-top: 30px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
            th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
            th {{ background: #667eea; color: white; }}
            tr:nth-child(even) {{ background: #f9f9f9; }}
            .health-good {{ color: #00b894; font-weight: bold; }}
            .health-warning {{ color: #fdcb6e; font-weight: bold; }}
            .health-critical {{ color: #d63031; font-weight: bold; }}
            .report-meta {{ color: #666; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <h1>🌊 Challawa Pump Station - Trip Event Report</h1>
        <div class="report-meta">
            <p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p><strong>Period:</strong> {start_date or 'All time'} to {end_date or 'Present'}</p>
        </div>
        
        <h2>Pump Health Summary</h2>
        <table>
            <tr><th>Pump</th><th>Total Trips</th><th>Last Trip</th><th>Health Score</th></tr>
    '''
    
    for h in health:
        health_class = 'health-good' if h['health_score'] >= 80 else ('health-warning' if h['health_score'] >= 50 else 'health-critical')
        html_content += f'''
            <tr>
                <td>{h['pump_name']}</td>
                <td>{h['total_trips']}</td>
                <td>{h['last_trip'] or 'Never'}</td>
                <td class="{health_class}">{h['health_score']}%</td>
            </tr>
        '''
    
    html_content += '''
        </table>
        
        <h2>Trip Event Log</h2>
        <table>
            <tr><th>Date/Time</th><th>Pump</th><th>Event</th><th>Pressure (Bar)</th><th>Speed (Hz)</th><th>Description</th></tr>
    '''
    
    for e in events:
        html_content += f'''
            <tr>
                <td>{e[1]}</td>
                <td>{e[2]}</td>
                <td>{e[4]}</td>
                <td>{e[5]:.2f}</td>
                <td>{e[6]:.1f}</td>
                <td>{e[7]}</td>
            </tr>
        '''
    
    if not events:
        html_content += '<tr><td colspan="6" style="text-align:center;">No trip events recorded</td></tr>'
    
    html_content += '''
        </table>
    </body>
    </html>
    '''
    
    # Return HTML file (can be printed to PDF by browser)
    return html_content, 200, {'Content-Type': 'text/html', 'Content-Disposition': 'attachment; filename=pump_report.html'}

# HTML Template (save as templates/index.html)
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Challawa Pump Dashboard</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1600px;
            margin: 0 auto;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 30px 40px;
            border-radius: 20px;
            box-shadow: 0 10px 40px rgba(102, 126, 234, 0.3);
            margin-bottom: 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header-left {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        .logo {
            width: 60px;
            height: 60px;
            background: white;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 30px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
        h1 {
            color: white;
            font-size: 32px;
            font-weight: 800;
            letter-spacing: -0.5px;
        }
        .subtitle {
            color: rgba(255,255,255,0.9);
            font-size: 14px;
            font-weight: 500;
            margin-top: 4px;
        }
        .header-right {
            display: flex;
            gap: 20px;
            align-items: center;
        }
        .alarm-box {
            background: rgba(255,255,255,0.2);
            backdrop-filter: blur(10px);
            padding: 15px 25px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .alarm-indicator {
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: rgba(255,255,255,0.3);
            transition: all 0.3s;
        }
        .alarm-indicator.active {
            background: #ff4757;
            box-shadow: 0 0 25px #ff4757, 0 0 50px #ff4757;
            animation: blink 1s infinite;
        }
        .alarm-text {
            color: white;
            font-weight: 700;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        @keyframes blink {
            0%, 50%, 100% { opacity: 1; }
            25%, 75% { opacity: 0.4; }
        }
        .connection-badge {
            background: rgba(255,255,255,0.2);
            backdrop-filter: blur(10px);
            padding: 12px 20px;
            border-radius: 25px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #ff4757;
            box-shadow: 0 0 10px #ff4757;
            animation: pulse 2s infinite;
        }
        .status-dot.connected {
            background: #2ed573;
            box-shadow: 0 0 10px #2ed573;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.6; transform: scale(0.95); }
        }
        .connection-text {
            color: white;
            font-weight: 600;
            font-size: 13px;
        }
        .timestamp {
            color: rgba(255,255,255,0.8);
            font-size: 12px;
            font-weight: 500;
        }
        .pumps-container {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
            gap: 20px;
        }
        .pump-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        .pump-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 12px;
            border-bottom: 1px solid #f0f0f0;
        }
        .pump-title {
            font-size: 16px;
            font-weight: 800;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .pump-status-badge {
            padding: 8px 20px;
            border-radius: 20px;
            font-weight: 700;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .pump-status-badge.ready {
            background: #ffeaa7;
            color: #d63031;
        }
        .pump-status-badge.running {
            background: #55efc4;
            color: #00b894;
        }
        .pump-status-badge.trip {
            background: #ff7675;
            color: #d63031;
        }
        .pump-status-badge.error {
            background: #dfe6e9;
            color: #636e72;
        }
        .metrics-row {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 15px;
        }
        .metric-box {
            background: #f8f9fa;
            border-radius: 10px;
            padding: 15px;
            border: 1px solid #e9ecef;
        }
        .metric-label {
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #636e72;
            margin-bottom: 8px;
        }
        .metric-value-row {
            display: flex;
            align-items: baseline;
            gap: 4px;
            margin-bottom: 8px;
        }
        .metric-value {
            font-size: 28px;
            font-weight: 800;
            color: #2d3436;
            line-height: 1;
        }
        .metric-unit {
            font-size: 12px;
            font-weight: 600;
            color: #636e72;
        }
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #e0e0e0;
            border-radius: 4px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            border-radius: 4px;
            transition: width 0.3s ease;
        }
        .progress-fill.pressure { background: linear-gradient(90deg, #667eea, #764ba2); }
        .progress-fill.speed { background: linear-gradient(90deg, #00b894, #55efc4); }
        .progress-labels {
            display: flex;
            justify-content: space-between;
            margin-top: 4px;
            font-size: 9px;
            color: #999;
            font-weight: 600;
        }
        .setpoint-box {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border: none;
        }
        .setpoint-box .metric-label,
        .setpoint-box .metric-value,
        .setpoint-box .metric-unit {
            color: white;
        }
        .status-grid {
            display: flex;
            gap: 10px;
        }
        .status-box {
            flex: 1;
            background: #f0f0f0;
            border-radius: 8px;
            padding: 10px;
            text-align: center;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            border: 2px solid transparent;
            transition: all 0.2s;
        }
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #b2bec3;
            transition: all 0.3s;
        }
        .status-box.ready.active {
            background: #fff8e1;
            border-color: #fdcb6e;
        }
        .status-box.ready.active .status-indicator {
            background: #fdcb6e;
            box-shadow: 0 0 10px #fdcb6e;
        }
        .status-box.running.active {
            background: #e8f8f5;
            border-color: #00b894;
        }
        .status-box.running.active .status-indicator {
            background: #00b894;
            box-shadow: 0 0 10px #00b894;
        }
        .status-box.trip.active {
            background: #fdeaea;
            border-color: #d63031;
        }
        .status-box.trip.active .status-indicator {
            background: #d63031;
            box-shadow: 0 0 10px #d63031;
            animation: blink 0.5s infinite;
        }
        .status-text {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #636e72;
        }
        .status-box.active .status-text {
            color: #2d3436;
        }

        /* Navigation */
        .nav-btn {
            background: rgba(255,255,255,0.2);
            backdrop-filter: blur(10px);
            border: none;
            padding: 12px 20px;
            border-radius: 10px;
            color: white;
            font-weight: 700;
            font-size: 13px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .nav-btn:hover {
            background: rgba(255,255,255,0.3);
            transform: translateY(-2px);
        }
        .nav-btn.active {
            background: white;
            color: #667eea;
        }

        /* Reports Page */
        .reports-container {
            display: none;
        }
        .reports-container.active {
            display: block;
        }
        .dashboard-container.hidden {
            display: none;
        }
        .reports-header {
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        .reports-title {
            font-size: 24px;
            font-weight: 800;
            color: #2d3436;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .filter-row {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: flex-end;
        }
        .filter-group {
            display: flex;
            flex-direction: column;
            gap: 5px;
        }
        .filter-group label {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            color: #636e72;
        }
        .filter-group input, .filter-group select {
            padding: 10px 15px;
            border: 2px solid #e9ecef;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            outline: none;
            transition: border-color 0.3s;
        }
        .filter-group input:focus, .filter-group select:focus {
            border-color: #667eea;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s;
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        .btn-success {
            background: linear-gradient(135deg, #00b894 0%, #55efc4 100%);
            color: white;
        }
        .btn-success:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(0, 184, 148, 0.4);
        }

        /* Health Cards */
        .health-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .health-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        .health-card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .health-card-title {
            font-size: 14px;
            font-weight: 700;
            color: #2d3436;
        }
        .health-score {
            font-size: 32px;
            font-weight: 800;
            padding: 5px 15px;
            border-radius: 8px;
        }
        .health-score.good {
            background: #e8f8f5;
            color: #00b894;
        }
        .health-score.warning {
            background: #fff8e1;
            color: #fdcb6e;
        }
        .health-score.critical {
            background: #fdeaea;
            color: #d63031;
        }
        .health-stats {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
        }
        .health-stat {
            background: #f8f9fa;
            padding: 12px;
            border-radius: 8px;
        }
        .health-stat-label {
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            color: #636e72;
            margin-bottom: 5px;
        }
        .health-stat-value {
            font-size: 18px;
            font-weight: 800;
            color: #2d3436;
        }

        /* Events Table */
        .events-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        .events-table {
            width: 100%;
            border-collapse: collapse;
        }
        .events-table th {
            background: #f8f9fa;
            padding: 12px;
            text-align: left;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            color: #636e72;
            border-bottom: 2px solid #e9ecef;
        }
        .events-table td {
            padding: 12px;
            border-bottom: 1px solid #f0f0f0;
            font-size: 13px;
        }
        .events-table tr:hover {
            background: #f8f9fa;
        }
        .event-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 15px;
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
        }
        .event-badge.trip {
            background: #fdeaea;
            color: #d63031;
        }
        .no-events {
            text-align: center;
            padding: 40px;
            color: #636e72;
        }

        /* Mobile Responsive Styles */
        @media (max-width: 768px) {
            body {
                padding: 10px;
            }
            .header {
                flex-direction: column;
                padding: 20px;
                gap: 15px;
                text-align: center;
            }
            .header-left {
                flex-direction: column;
                gap: 10px;
            }
            .logo {
                width: 50px;
                height: 50px;
                font-size: 24px;
            }
            h1 {
                font-size: 20px;
            }
            .subtitle {
                font-size: 12px;
            }
            .header-right {
                flex-direction: column;
                width: 100%;
                gap: 10px;
            }
            .alarm-box {
                padding: 10px 15px;
                width: 100%;
                justify-content: center;
            }
            .alarm-indicator {
                width: 18px;
                height: 18px;
            }
            .alarm-text {
                font-size: 12px;
            }
            .connection-badge {
                padding: 10px 15px;
                width: 100%;
                justify-content: center;
            }
            .pumps-container {
                grid-template-columns: 1fr;
                gap: 15px;
            }
            .pump-card {
                padding: 15px;
            }
            .pump-card-header {
                flex-direction: column;
                gap: 10px;
                align-items: flex-start;
            }
            .pump-title {
                font-size: 14px;
            }
            .pump-status-badge {
                padding: 6px 12px;
                font-size: 10px;
            }
            .metrics-row {
                grid-template-columns: 1fr;
                gap: 10px;
            }
            .metric-box {
                padding: 12px;
            }
            .metric-value {
                font-size: 24px;
            }
            .metric-unit {
                font-size: 11px;
            }
            .status-grid {
                flex-direction: row;
                gap: 8px;
            }
            .status-box {
                padding: 8px;
                flex-direction: column;
                gap: 4px;
            }
            .status-indicator {
                width: 10px;
                height: 10px;
            }
            .status-text {
                font-size: 9px;
            }
            .nav-btn {
                width: 100%;
                justify-content: center;
            }
            .filter-row {
                flex-direction: column;
            }
            .filter-group {
                width: 100%;
            }
            .filter-group input, .filter-group select {
                width: 100%;
            }
            .btn {
                width: 100%;
                justify-content: center;
            }
            .health-grid {
                grid-template-columns: 1fr;
            }
            .events-table {
                font-size: 11px;
            }
            .events-table th, .events-table td {
                padding: 8px;
            }
        }

        @media (max-width: 480px) {
            body {
                padding: 8px;
            }
            .header {
                padding: 15px;
                border-radius: 12px;
                margin-bottom: 15px;
            }
            h1 {
                font-size: 18px;
            }
            .pump-card {
                border-radius: 10px;
                padding: 12px;
            }
            .pump-title {
                font-size: 12px;
            }
            .metric-value {
                font-size: 22px;
            }
            .progress-bar {
                height: 6px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-left">
                <div class="logo">💧</div>
                <div>
                    <h1>Challawa Pump Station</h1>
                    <div class="subtitle">Real-time Monitoring & Control System</div>
                </div>
            </div>
            <div class="header-right">
                <button class="nav-btn active" id="dashboardBtn" onclick="showDashboard()">
                    📊 Dashboard
                </button>
                <button class="nav-btn" id="reportsBtn" onclick="showReports()">
                    📋 Reports
                </button>
                <div class="alarm-box">
                    <div class="alarm-indicator" id="alarmIndicator"></div>
                    <div class="alarm-text">System Alarm</div>
                </div>
                <div class="connection-badge">
                    <div class="status-dot" id="connectionDot"></div>
                    <div>
                        <div class="connection-text" id="connectionText">Disconnected</div>
                        <div class="timestamp" id="timestamp">--:--:--</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Dashboard Container -->
        <div class="dashboard-container" id="dashboardContainer">
            <div class="pumps-container">
                <!-- PUMP 1 -->
                <div class="pump-card">
                <div class="pump-card-header">
                    <div class="pump-title">LINE 7 MIXER</div>
                    <div class="pump-status-badge" id="p1StatusBadge">Unknown</div>
                </div>
                
                <div class="metrics-row">
                    <div class="metric-box">
                        <div class="metric-label">Pressure</div>
                        <div class="metric-value-row">
                            <span class="metric-value" id="p1Pressure">0.00</span>
                            <span class="metric-unit">Bar</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill pressure" id="p1PressureBar" style="width: 0%"></div>
                        </div>
                        <div class="progress-labels"><span>0</span><span>10</span></div>
                    </div>
                    
                    <div class="metric-box setpoint-box">
                        <div class="metric-label">Set Point</div>
                        <div class="metric-value-row">
                            <span class="metric-value" id="p1Setpoint">0.00</span>
                            <span class="metric-unit">Bar</span>
                        </div>
                    </div>
                    
                    <div class="metric-box">
                        <div class="metric-label">Speed</div>
                        <div class="metric-value-row">
                            <span class="metric-value" id="p1Speed">0.0</span>
                            <span class="metric-unit">Hz</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill speed" id="p1SpeedBar" style="width: 0%"></div>
                        </div>
                        <div class="progress-labels"><span>0</span><span>50</span></div>
                    </div>
                </div>

                <div class="status-grid">
                    <div class="status-box ready" id="p1ReadyBox">
                        <div class="status-indicator"></div>
                        <div class="status-text">Ready</div>
                    </div>
                    <div class="status-box running" id="p1RunningBox">
                        <div class="status-indicator"></div>
                        <div class="status-text">Running</div>
                    </div>
                    <div class="status-box trip" id="p1TripBox">
                        <div class="status-indicator"></div>
                        <div class="status-text">Trip</div>
                    </div>
                </div>
            </div>

            <!-- PUMP 2 -->
            <div class="pump-card">
                <div class="pump-card-header">
                    <div class="pump-title">LINE 7 AHU/SYRUP ROOM</div>
                    <div class="pump-status-badge" id="p2StatusBadge">Unknown</div>
                </div>
                
                <div class="metrics-row">
                    <div class="metric-box">
                        <div class="metric-label">Pressure</div>
                        <div class="metric-value-row">
                            <span class="metric-value" id="p2Pressure">0.00</span>
                            <span class="metric-unit">Bar</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill pressure" id="p2PressureBar" style="width: 0%"></div>
                        </div>
                        <div class="progress-labels"><span>0</span><span>10</span></div>
                    </div>
                    
                    <div class="metric-box setpoint-box">
                        <div class="metric-label">Set Point</div>
                        <div class="metric-value-row">
                            <span class="metric-value" id="p2Setpoint">0.00</span>
                            <span class="metric-unit">Bar</span>
                        </div>
                    </div>
                    
                    <div class="metric-box">
                        <div class="metric-label">Speed</div>
                        <div class="metric-value-row">
                            <span class="metric-value" id="p2Speed">0.0</span>
                            <span class="metric-unit">Hz</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill speed" id="p2SpeedBar" style="width: 0%"></div>
                        </div>
                        <div class="progress-labels"><span>0</span><span>50</span></div>
                    </div>
                </div>

                <div class="status-grid">
                    <div class="status-box ready" id="p2ReadyBox">
                        <div class="status-indicator"></div>
                        <div class="status-text">Ready</div>
                    </div>
                    <div class="status-box running" id="p2RunningBox">
                        <div class="status-indicator"></div>
                        <div class="status-text">Running</div>
                    </div>
                    <div class="status-box trip" id="p2TripBox">
                        <div class="status-indicator"></div>
                        <div class="status-text">Trip</div>
                    </div>
                </div>
            </div>
            </div>
        </div>

        <!-- Reports Container -->
        <div class="reports-container" id="reportsContainer">
            <div class="reports-header">
                <div class="reports-title">📋 Trip Event Reports & Pump Health</div>
                <div class="filter-row">
                    <div class="filter-group">
                        <label>Start Date</label>
                        <input type="date" id="startDate">
                    </div>
                    <div class="filter-group">
                        <label>End Date</label>
                        <input type="date" id="endDate">
                    </div>
                    <div class="filter-group">
                        <label>Pump</label>
                        <select id="pumpFilter">
                            <option value="">All Pumps</option>
                            <option value="1">LINE 7 MIXER</option>
                            <option value="2">LINE 7 AHU/SYRUP ROOM</option>
                        </select>
                    </div>
                    <button class="btn btn-primary" onclick="queryReports()">
                        🔍 Query Reports
                    </button>
                    <button class="btn btn-success" onclick="downloadReport()">
                        📥 Download PDF
                    </button>
                </div>
            </div>

            <div class="health-grid" id="healthGrid">
                <!-- Health cards will be populated dynamically -->
            </div>

            <div class="events-card">
                <h3 style="margin-bottom: 15px; color: #2d3436;">📜 Trip Event Log</h3>
                <div style="overflow-x: auto;">
                    <table class="events-table">
                        <thead>
                            <tr>
                                <th>Date/Time</th>
                                <th>Pump</th>
                                <th>Event</th>
                                <th>Pressure</th>
                                <th>Speed</th>
                                <th>Description</th>
                            </tr>
                        </thead>
                        <tbody id="eventsTableBody">
                            <tr><td colspan="6" class="no-events">No trip events recorded</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Socket.IO with reconnection and fallback
        const socket = io({
            reconnection: true,
            reconnectionAttempts: Infinity,
            reconnectionDelay: 1000,
            reconnectionDelayMax: 5000,
            timeout: 20000,
            transports: ['polling', 'websocket']
        });

        let socketConnected = false;
        let pollingInterval = null;

        // Start fallback polling if socket fails
        function startPolling() {
            if (pollingInterval) return;
            console.log('Starting fallback polling...');
            pollingInterval = setInterval(() => {
                fetch('/api/status')
                    .then(r => r.json())
                    .then(updateStatus)
                    .catch(e => console.error('Polling error:', e));
            }, 1000);
        }

        function stopPolling() {
            if (pollingInterval) {
                console.log('Stopping fallback polling');
                clearInterval(pollingInterval);
                pollingInterval = null;
            }
        }

        socket.on('connect', () => {
            console.log('Socket connected');
            socketConnected = true;
            stopPolling();
        });

        socket.on('disconnect', () => {
            console.log('Socket disconnected');
            socketConnected = false;
            // Start polling as fallback after 2 seconds
            setTimeout(() => {
                if (!socketConnected) startPolling();
            }, 2000);
        });

        socket.on('connect_error', (error) => {
            console.log('Socket connection error:', error);
            if (!pollingInterval) startPolling();
        });

        function updateTimestamp() {
            const now = new Date();
            const time = now.toLocaleTimeString('en-US', { hour12: false });
            document.getElementById('timestamp').textContent = time;
        }
        setInterval(updateTimestamp, 1000);
        updateTimestamp();

        function updateProgressBar(elementId, value, max) {
            const percent = Math.min(Math.max((value / max) * 100, 0), 100);
            const bar = document.getElementById(elementId);
            if (bar) bar.style.width = percent + '%';
        }

        function updatePump(pumpNum, pumpData) {
            const prefix = 'p' + pumpNum;
            
            // Update pressure (0-10 bar)
            document.getElementById(prefix + 'Pressure').textContent = pumpData.pressure.toFixed(2);
            updateProgressBar(prefix + 'PressureBar', pumpData.pressure, 10);
            
            // Update setpoint
            document.getElementById(prefix + 'Setpoint').textContent = pumpData.pressure_setpoint.toFixed(2);
            
            // Update speed (0-50 Hz)
            document.getElementById(prefix + 'Speed').textContent = pumpData.speed.toFixed(1);
            updateProgressBar(prefix + 'SpeedBar', pumpData.speed, 50);
            
            // Update status badge
            const badge = document.getElementById(prefix + 'StatusBadge');
            badge.textContent = pumpData.status;
            badge.className = 'pump-status-badge ' + pumpData.status.toLowerCase();
            
            // Update status boxes
            const readyBox = document.getElementById(prefix + 'ReadyBox');
            const runningBox = document.getElementById(prefix + 'RunningBox');
            const tripBox = document.getElementById(prefix + 'TripBox');
            
            readyBox.classList.toggle('active', pumpData.ready);
            runningBox.classList.toggle('active', pumpData.running);
            tripBox.classList.toggle('active', pumpData.trip);
        }

        function updateStatus(data) {
            // Update connection status
            const dot = document.getElementById('connectionDot');
            const text = document.getElementById('connectionText');
            if (data.connected) {
                dot.classList.add('connected');
                text.textContent = 'Connected';
            } else {
                dot.classList.remove('connected');
                text.textContent = 'Disconnected';
            }

            // Update alarm indicator
            const alarm = document.getElementById('alarmIndicator');
            alarm.classList.toggle('active', data.alarm);

            // Update both pumps
            updatePump(1, data.pump1);
            updatePump(2, data.pump2);
        }

        socket.on('pump_data', updateStatus);

        // Initial fetch
        fetch('/api/status')
            .then(r => r.json())
            .then(updateStatus);

        // Navigation functions
        function showDashboard() {
            document.getElementById('dashboardContainer').classList.remove('hidden');
            document.getElementById('reportsContainer').classList.remove('active');
            document.getElementById('dashboardBtn').classList.add('active');
            document.getElementById('reportsBtn').classList.remove('active');
        }

        function showReports() {
            document.getElementById('dashboardContainer').classList.add('hidden');
            document.getElementById('reportsContainer').classList.add('active');
            document.getElementById('dashboardBtn').classList.remove('active');
            document.getElementById('reportsBtn').classList.add('active');
            loadHealthData();
            queryReports();
        }

        // Load pump health data
        function loadHealthData() {
            fetch('/api/reports/health')
                .then(r => r.json())
                .then(data => {
                    const grid = document.getElementById('healthGrid');
                    grid.innerHTML = data.map(pump => {
                        const healthClass = pump.health_score >= 80 ? 'good' : (pump.health_score >= 50 ? 'warning' : 'critical');
                        return `
                            <div class="health-card">
                                <div class="health-card-header">
                                    <div class="health-card-title">${pump.pump_name}</div>
                                    <div class="health-score ${healthClass}">${pump.health_score}%</div>
                                </div>
                                <div class="health-stats">
                                    <div class="health-stat">
                                        <div class="health-stat-label">Total Trips</div>
                                        <div class="health-stat-value">${pump.total_trips}</div>
                                    </div>
                                    <div class="health-stat">
                                        <div class="health-stat-label">Last Trip</div>
                                        <div class="health-stat-value">${pump.last_trip ? new Date(pump.last_trip).toLocaleDateString() : 'Never'}</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    }).join('');
                });
        }

        // Query and display events
        function queryReports() {
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;
            const pumpId = document.getElementById('pumpFilter').value;
            
            let url = '/api/reports/events?';
            if (startDate) url += `start_date=${startDate}&`;
            if (endDate) url += `end_date=${endDate}&`;
            if (pumpId) url += `pump_id=${pumpId}&`;
            
            fetch(url)
                .then(r => r.json())
                .then(events => {
                    const tbody = document.getElementById('eventsTableBody');
                    if (events.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="6" class="no-events">No trip events found for selected filters</td></tr>';
                        return;
                    }
                    tbody.innerHTML = events.map(e => `
                        <tr>
                            <td>${e.timestamp}</td>
                            <td>${e.pump_name}</td>
                            <td><span class="event-badge trip">${e.event_type}</span></td>
                            <td>${e.pressure.toFixed(2)} Bar</td>
                            <td>${e.speed.toFixed(1)} Hz</td>
                            <td>${e.description}</td>
                        </tr>
                    `).join('');
                });
        }

        // Download report
        function downloadReport() {
            const startDate = document.getElementById('startDate').value;
            const endDate = document.getElementById('endDate').value;
            const pumpId = document.getElementById('pumpFilter').value;
            
            let url = '/api/reports/download?';
            if (startDate) url += `start_date=${startDate}&`;
            if (endDate) url += `end_date=${endDate}&`;
            if (pumpId) url += `pump_id=${pumpId}&`;
            
            // Open in new window for printing/saving as PDF
            window.open(url, '_blank');
        }

        // Set default dates
        const today = new Date();
        const lastMonth = new Date();
        lastMonth.setMonth(lastMonth.getMonth() - 1);
        document.getElementById('endDate').value = today.toISOString().split('T')[0];
        document.getElementById('startDate').value = lastMonth.toISOString().split('T')[0];
    </script>
</body>
</html>
'''

if __name__ == '__main__':
    # Initialize database
    init_database()
    print("✓ Database initialized")
    
    # Start monitoring
    monitor.start()
    
    try:
        print("\n" + "="*60)
        print("  🌊 CHALLAWA PUMP MONITORING DASHBOARD")
        print("="*60)
        print(f"  PLC Address: {PLC_IP}")
        print(f"  Web Interface: http://localhost:{WEB_PORT}")
        print(f"  Monitoring: Pump 1 & Pump 2")
        print(f"  Update Rate: {CYCLE_TIME}s")
        print(f"  Database: {DATABASE_PATH}")
        print("="*60 + "\n")
        
        socketio.run(app, host='0.0.0.0', port=WEB_PORT, debug=False)
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        monitor.stop()