# Challawa Pump Monitoring Dashboard

Real-time monitoring dashboard for Siemens S7-1200 PLC via Snap7.  
Monitors AHU FOR LINE 7 PRODUCTION HALL & SYRUP ROOM and LINE 7 MIXER.

## Features

- **Real-time Monitoring**: Live pressure, speed, and status updates via WebSocket
- **Trip Event Logging**: Automatic logging of pump trip events to SQLite database
- **Reports Page**: Query and filter trip events by date range and pump
- **Health Tracking**: Pump health scores based on trip history
- **PDF Export**: Download reports as HTML/PDF
- **Mobile Responsive**: Works on desktop, tablet, and mobile devices

## Requirements

- Python 3.8+
- Flask
- Flask-SocketIO
- python-snap7

## Installation

```bash
pip install flask flask-socketio python-snap7
```

## Configuration

Edit the following in `challawa_app.py`:

```python
PLC_IP = "192.168.200.20"  # Your PLC IP address
PLC_RACK = 0
PLC_SLOT = 1
DB_NUMBER = 39
WEB_PORT = 5050  # Web interface port
```

## Usage

```bash
python challawa_app.py
```

Access the dashboard at `http://localhost:5050`

## PLC Data Structure (DB39)

| Byte | Data Type | Description |
|------|-----------|-------------|
| 0.0 | Bool | System Alarm |
| 0.1 | Bool | Pump 1 Ready |
| 0.2 | Bool | Pump 1 Running |
| 0.3 | Bool | Pump 1 Trip |
| 2-5 | Real | Pump 1 Pressure |
| 6-9 | Real | Pump 1 Pressure Setpoint |
| 10-13 | Real | Pump 1 Speed |
| 14.0 | Bool | Pump 2 Running |
| 14.1 | Bool | Pump 2 Ready |
| 14.2 | Bool | Pump 2 Trip |
| 16-19 | Real | Pump 2 Pressure |
| 20-23 | Real | Pump 2 Pressure Setpoint |
| 24-27 | Real | Pump 2 Speed |

## License

MIT
