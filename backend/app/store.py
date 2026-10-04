"""Small SQLite store so demo reviews, readings, assets and field actions survive restarts."""
import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parents[1] / "water_ops.sqlite3"

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_store():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, zone TEXT, from_node TEXT, to_node TEXT, metadata TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, name TEXT NOT NULL, skills TEXT NOT NULL, area TEXT DEFAULT '', equipment TEXT DEFAULT '[]', available INTEGER NOT NULL DEFAULT 1, assignment TEXT);
        CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY AUTOINCREMENT, sensor_id TEXT NOT NULL, zone TEXT NOT NULL, type TEXT NOT NULL, timestamp TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, quality TEXT NOT NULL, UNIQUE(sensor_id,timestamp));
        CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, leak_id TEXT NOT NULL, decision TEXT NOT NULL, note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT, leak_id TEXT NOT NULL, team_id TEXT, status TEXT NOT NULL, observation TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        seed_assets=[("RES-01","reservoir","North Reservoir","DMA-A",None,None),("PIPE-A12","pipeline","Reservoir trunk","DMA-A","RES-01","J-A"),("PIPE-B12","pipeline","DMA-A transfer","DMA-B","J-A","J-B"),("PIPE-C12","pipeline","DMA-B transfer","DMA-C","J-B","J-C"),("FLOW-DMA-A","flow_meter","DMA-A inlet","DMA-A",None,None),("FLOW-DMA-B","flow_meter","DMA-B inlet","DMA-B",None,None),("FLOW-DMA-C","flow_meter","DMA-C inlet","DMA-C",None,None),("PRESSURE-DMA-A","pressure_sensor","DMA-A pressure","DMA-A",None,None),("PRESSURE-DMA-B","pressure_sensor","DMA-B pressure","DMA-B",None,None),("PRESSURE-DMA-C","pressure_sensor","DMA-C pressure","DMA-C",None,None)]
        for row in seed_assets: db.execute("INSERT OR IGNORE INTO assets(id,type,name,zone,from_node,to_node) VALUES(?,?,?,?,?,?)",row)
        db.execute("INSERT OR IGNORE INTO teams(id,name,skills,area,equipment,available) VALUES(?,?,?,?,?,1)",("TEAM-01","North Acoustic Team",json.dumps(["acoustic","pressure testing"]),"North District",json.dumps(["correlator","pressure logger"])))

def log_event(db, event_type, payload):
    db.execute("INSERT INTO events(event_type,payload) VALUES(?,?)",(event_type,json.dumps(payload,default=str)))

def rows_as_dicts(db, query, params=()):
    return [dict(r) for r in db.execute(query,params).fetchall()]

init_store()
