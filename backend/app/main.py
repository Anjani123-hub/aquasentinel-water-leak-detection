from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Literal
from io import BytesIO, StringIO
from datetime import datetime, timezone, timedelta
import json
import csv
import os
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from .agents import run_pipeline
from .store import connect, log_event, rows_as_dicts

app = FastAPI(title="AquaSentinel Water Operations API", version="1.0.0", description="Decision-support prototype; does not control production infrastructure.")
cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_methods=["*"], allow_headers=["*"])

DEMO = {"inflow": {"DMA-A": 420, "DMA-B": 510, "DMA-C": 350}, "demand": {"DMA-A": 405, "DMA-B": 390, "DMA-C": 342}, "pressure": {"DMA-A": 4.2, "DMA-B": 2.8, "DMA-C": 4.0}, "readings": [
 {"sensor_id":"FLOW-DMA-A","zone":"DMA-A","type":"flow","timestamp":"2026-10-03T02:00:00Z","value":420,"unit":"m3/h"},
 {"sensor_id":"FLOW-DMA-B","zone":"DMA-B","type":"flow","timestamp":"2026-10-03T02:00:00Z","value":510,"unit":"m3/h"},
 {"sensor_id":"FLOW-DMA-C","zone":"DMA-C","type":"flow","timestamp":"2026-10-03T02:00:00Z","value":350,"unit":"m3/h"},
 {"sensor_id":"PRESSURE-DMA-A","zone":"DMA-A","type":"pressure","timestamp":"2026-10-03T02:00:00Z","value":4.2,"unit":"bar"},
 {"sensor_id":"PRESSURE-DMA-B","zone":"DMA-B","type":"pressure","timestamp":"2026-10-03T02:00:00Z","value":2.8,"unit":"bar"},
 {"sensor_id":"PRESSURE-DMA-C","zone":"DMA-C","type":"pressure","timestamp":"2026-10-03T02:00:00Z","value":4.0,"unit":"bar"},
 {"sensor_id":"PRESSURE-DMA-B-FAULT","zone":"DMA-B","type":"pressure","timestamp":"2026-10-03T02:00:00Z","value":-3,"unit":"bar"}]}
class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""

class AssetInput(BaseModel):
    id: str
    type: str
    name: str
    zone: str = ""
    from_node: str | None = None
    to_node: str | None = None
    metadata: dict = {}

class TeamInput(BaseModel):
    id: str
    name: str
    skills: list[str]
    area: str = ""
    equipment: list[str] = []
    available: bool = True

class ReadingInput(BaseModel):
    sensor_id: str
    zone: str
    type: Literal["flow", "pressure", "tank_level"]
    timestamp: str
    value: float
    unit: str

class ActionInput(BaseModel):
    leak_id: str
    team_id: str | None = None
    status: Literal["planned", "approved", "dispatched", "on_site", "repair_recorded", "verified", "cancelled"]
    observation: str = ""

class SimulationInput(BaseModel):
    scenario: Literal["normal", "leak", "repair", "sensor_fault"] = "leak"

@app.get("/api/health")
def health(): return {"status":"ok", "service":"AquaSentinel API"}

def current_demo():
    scenario={**DEMO,"inflow":dict(DEMO["inflow"]),"pressure":dict(DEMO["pressure"]),"demand":dict(DEMO["demand"]),"readings":list(DEMO["readings"])}
    with connect() as db: stored=rows_as_dicts(db,"SELECT * FROM readings ORDER BY timestamp DESC,id DESC")
    latest={}
    for r in stored:
        latest.setdefault(r["sensor_id"],r)
    for r in latest.values():
        scenario["readings"].append({"sensor_id":r["sensor_id"],"zone":r["zone"],"type":r["type"],"timestamp":r["timestamp"],"value":r["value"],"unit":r["unit"],"quality":r["quality"]})
        if r["quality"]=="valid":
            if r["type"]=="flow": scenario["inflow"][r["zone"]]=r["value"]
            elif r["type"]=="pressure": scenario["pressure"][r["zone"]]=r["value"]
    return scenario

@app.get("/api/network")
def network():
    with connect() as db:
        assets=rows_as_dicts(db,"SELECT * FROM assets ORDER BY type,id")
        teams=rows_as_dicts(db,"SELECT * FROM teams ORDER BY id")
    scenario=current_demo()
    clean_assets=[{"id":a["id"],"type":a["type"],"name":a["name"],"zone":a["zone"],"from":a["from_node"],"to":a["to_node"],"metadata":json.loads(a["metadata"])} for a in assets]
    return {"zones":[{"id":z,"inflow_m3h":f,"demand_m3h":scenario["demand"][z],"pressure_bar":scenario["pressure"][z]} for z,f in scenario["inflow"].items()], "assets":clean_assets, "teams":[{**t,"skills":json.loads(t["skills"]),"equipment":json.loads(t["equipment"]),"available":bool(t["available"])} for t in teams]}

@app.get("/api/demo")
def demo(): return run_pipeline(current_demo())

@app.post("/api/simulate")
def simulate(body: SimulationInput):
    profiles={
        "normal":{"inflow":{"DMA-A":420,"DMA-B":390,"DMA-C":350},"pressure":{"DMA-A":4.2,"DMA-B":4.0,"DMA-C":4.0}},
        "leak":{"inflow":{"DMA-A":420,"DMA-B":510,"DMA-C":350},"pressure":{"DMA-A":4.2,"DMA-B":2.8,"DMA-C":4.0}},
        "repair":{"inflow":{"DMA-A":420,"DMA-B":390,"DMA-C":350},"pressure":{"DMA-A":4.2,"DMA-B":4.0,"DMA-C":4.0}},
        "sensor_fault":{"inflow":{"DMA-A":420,"DMA-B":390,"DMA-C":350},"pressure":{"DMA-A":4.2,"DMA-B":-3.0,"DMA-C":4.0}},
    }
    now=datetime.now(timezone.utc).isoformat(); readings=[]
    for zone,value in profiles[body.scenario]["inflow"].items(): readings.append({"sensor_id":f"FLOW-{zone}","zone":zone,"type":"flow","timestamp":now,"value":value,"unit":"m3/h"})
    for zone,value in profiles[body.scenario]["pressure"].items(): readings.append({"sensor_id":f"PRESSURE-{zone}","zone":zone,"type":"pressure","timestamp":now,"value":value,"unit":"bar"})
    with connect() as db:
        for r in readings:
            quality="valid" if r["value"]>=0 else "invalid"
            db.execute("INSERT INTO readings(sensor_id,zone,type,timestamp,value,unit,quality) VALUES(?,?,?,?,?,?,?)",(r["sensor_id"],r["zone"],r["type"],r["timestamp"],r["value"],r["unit"],quality))
        log_event(db,"simulated_sensor_batch",{"scenario":body.scenario,"readings":readings})
    workflow=run_pipeline(current_demo())
    with connect() as db: log_event(db,"dynamic_reassessment",workflow)
    return {"scenario":body.scenario,"readings":readings,"workflow":workflow}

@app.post("/api/reassess")
def reassess(payload: dict):
    merged = {**current_demo(), **payload}
    try:
        result=run_pipeline(merged)
        with connect() as db: log_event(db,"reassessment",result)
        return result
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/decision/{leak_id}")
def decision(leak_id: str, body: Decision):
    result={"leak_id": leak_id, "decision": body.decision, "note": body.note, "workflow_state": "inspection_approved" if body.decision == "approve" else "hypothesis_rejected", "recorded_by": "operator"}
    with connect() as db:
        db.execute("INSERT INTO decisions(leak_id,decision,note) VALUES(?,?,?)",(leak_id,body.decision,body.note)); log_event(db,"operator_decision",result)
    return result

@app.post("/api/post-repair")
def post_repair(body: dict):
    before_flow=float(body.get("before_flow", 150)); after_flow=float(body.get("after_flow", 92)); before_pressure=float(body.get("before_pressure", 2.7)); after_pressure=float(body.get("after_pressure", 4.0))
    result={"flow_change_m3h": round(after_flow-before_flow,1), "flow_reduction_percent": round((before_flow-after_flow)/before_flow*100,1) if before_flow else 0, "pressure_change_bar": round(after_pressure-before_pressure,2), "observed_improvement": after_flow < before_flow and after_pressure > before_pressure, "note": "Observed change only; authorized personnel must verify repair completion."}
    with connect() as db: log_event(db,"post_repair_comparison",{**body,**result})
    return result

@app.get("/api/assets")
def list_assets():
    with connect() as db: return rows_as_dicts(db,"SELECT * FROM assets ORDER BY type,id")

@app.post("/api/assets")
def create_asset(asset: AssetInput):
    with connect() as db:
        try:
            db.execute("INSERT INTO assets(id,type,name,zone,from_node,to_node,metadata) VALUES(?,?,?,?,?,?,?)",(asset.id,asset.type,asset.name,asset.zone,asset.from_node,asset.to_node,json.dumps(asset.metadata)))
        except Exception as e: raise HTTPException(status_code=409,detail=f"Could not create asset: {e}")
        log_event(db,"asset_created",asset.model_dump())
    return {"created":True,"asset":asset.model_dump()}

@app.put("/api/assets/{asset_id}")
def update_asset(asset_id: str, asset: AssetInput):
    if asset_id != asset.id: raise HTTPException(status_code=400,detail="Path ID and body ID must match")
    with connect() as db:
        cur=db.execute("UPDATE assets SET type=?,name=?,zone=?,from_node=?,to_node=?,metadata=? WHERE id=?",(asset.type,asset.name,asset.zone,asset.from_node,asset.to_node,json.dumps(asset.metadata),asset_id))
        if not cur.rowcount: raise HTTPException(status_code=404,detail="Asset not found")
        log_event(db,"asset_updated",asset.model_dump())
    return {"updated":True,"asset":asset.model_dump()}

@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: str):
    with connect() as db:
        cur=db.execute("DELETE FROM assets WHERE id=?",(asset_id,))
        if not cur.rowcount: raise HTTPException(status_code=404,detail="Asset not found")
        log_event(db,"asset_deleted",{"id":asset_id})
    return {"deleted":True,"id":asset_id}

@app.get("/api/teams")
def list_teams():
    with connect() as db: rows=rows_as_dicts(db,"SELECT * FROM teams ORDER BY id")
    return [{**t,"skills":json.loads(t["skills"]),"equipment":json.loads(t["equipment"]),"available":bool(t["available"])} for t in rows]

@app.post("/api/teams")
def create_team(team: TeamInput):
    with connect() as db:
        try: db.execute("INSERT INTO teams(id,name,skills,area,equipment,available) VALUES(?,?,?,?,?,?)",(team.id,team.name,json.dumps(team.skills),team.area,json.dumps(team.equipment),int(team.available)))
        except Exception as e: raise HTTPException(status_code=409,detail=f"Could not create team: {e}")
        log_event(db,"team_created",team.model_dump())
    return {"created":True,"team":team.model_dump()}

@app.post("/api/actions")
def create_action(action: ActionInput):
    with connect() as db:
        if action.team_id:
            team=db.execute("SELECT * FROM teams WHERE id=?",(action.team_id,)).fetchone()
            if not team: raise HTTPException(status_code=404,detail="Team not found")
            occupied=db.execute("SELECT id FROM actions WHERE team_id=? AND status IN ('approved','dispatched','on_site')",(action.team_id,)).fetchone()
            if not team["available"] or occupied: raise HTTPException(status_code=409,detail="Team is unavailable or already assigned")
        cur=db.execute("INSERT INTO actions(leak_id,team_id,status,observation) VALUES(?,?,?,?)",(action.leak_id,action.team_id,action.status,action.observation))
        action_id=cur.lastrowid
        if action.team_id and action.status in ("approved","dispatched","on_site"):
            db.execute("UPDATE teams SET available=0,assignment=? WHERE id=?",(action.leak_id,action.team_id))
        log_event(db,"field_action",{"action_id":action_id,**action.model_dump()})
    return {"action_id":action_id,**action.model_dump()}

@app.patch("/api/actions/{action_id}")
def update_action(action_id: int, action: ActionInput):
    with connect() as db:
        old=db.execute("SELECT * FROM actions WHERE id=?",(action_id,)).fetchone()
        if not old: raise HTTPException(status_code=404,detail="Action not found")
        if action.team_id and action.status in ("approved","dispatched","on_site") and old["status"] not in ("approved","dispatched","on_site"):
            team=db.execute("SELECT * FROM teams WHERE id=?",(action.team_id,)).fetchone()
            occupied=db.execute("SELECT id FROM actions WHERE team_id=? AND status IN ('approved','dispatched','on_site')",(action.team_id,)).fetchone()
            if not team or not team["available"] or occupied: raise HTTPException(status_code=409,detail="Team is unavailable or already assigned")
        db.execute("UPDATE actions SET status=?,observation=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(action.status,action.observation,action_id))
        if action.team_id and action.status in ("approved","dispatched","on_site"):
            db.execute("UPDATE teams SET available=0,assignment=? WHERE id=?",(action.leak_id,action.team_id))
        if old["team_id"] and action.status in ("repair_recorded","verified","cancelled"):
            db.execute("UPDATE teams SET available=1,assignment=NULL WHERE id=?",(old["team_id"],))
        log_event(db,"field_action_updated",{"action_id":action_id,**action.model_dump()})
        return dict(db.execute("SELECT * FROM actions WHERE id=?",(action_id,)).fetchone())

@app.get("/api/actions")
def list_actions():
    with connect() as db: return rows_as_dicts(db,"SELECT * FROM actions ORDER BY created_at DESC")

@app.get("/api/events")
def list_events(limit: int = 100):
    with connect() as db: events=rows_as_dicts(db,"SELECT * FROM events ORDER BY id DESC LIMIT ?",(max(1,min(limit,500)),))
    for e in events: e["payload"]=json.loads(e["payload"])
    return events

@app.post("/api/readings")
def add_reading(reading: ReadingInput):
    try:
        parsed=datetime.fromisoformat(reading.timestamp.replace("Z","+00:00"))
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    except ValueError: raise HTTPException(status_code=400,detail="timestamp must be ISO 8601")
    expected={"flow":"m3/h","pressure":"bar","tank_level":"%"}[reading.type]
    maximum={"flow":10000,"pressure":20,"tank_level":100}[reading.type]
    valid=0<=reading.value<=maximum and reading.unit==expected and parsed<=datetime.now(timezone.utc)+timedelta(minutes=5)
    quality="invalid" if not valid else ("stale" if (datetime.now(timezone.utc)-parsed).total_seconds()>1800 else "valid")
    with connect() as db:
        try: db.execute("INSERT INTO readings(sensor_id,zone,type,timestamp,value,unit,quality) VALUES(?,?,?,?,?,?,?)",(reading.sensor_id,reading.zone,reading.type,reading.timestamp,reading.value,reading.unit,quality))
        except Exception as e: raise HTTPException(status_code=409,detail=f"Duplicate reading or database error: {e}")
        log_event(db,"sensor_reading",{**reading.model_dump(),"quality":"valid" if valid else "invalid"})
    reassessment=run_pipeline(current_demo())
    with connect() as db: log_event(db,"dynamic_reassessment",reassessment)
    return {**reading.model_dump(),"data_quality":quality,"request_reassessment":True,"workflow":reassessment}

@app.get("/api/readings")
def list_readings(limit: int = 250):
    with connect() as db: return rows_as_dicts(db,"SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?",(max(1,min(limit,2000)),))

@app.get("/api/report")
def report():
    result=run_pipeline(current_demo())
    with connect() as db:
        actions=rows_as_dicts(db,"SELECT * FROM actions ORDER BY created_at DESC")
        decisions=rows_as_dicts(db,"SELECT * FROM decisions ORDER BY created_at DESC")
        readings=rows_as_dicts(db,"SELECT * FROM readings ORDER BY timestamp DESC LIMIT 250")
    return {"title":"AquaSentinel Water Network Operational Report", "period":"Current demo snapshot", "workflow":result, "network":network(), "field_actions":actions, "operator_decisions":decisions, "sensor_quality":{"invalid_count":sum(1 for r in readings if r["quality"]=="invalid"),"stale_count":sum(1 for r in readings if r["quality"]=="stale"),"readings_included":len(readings)}}

@app.get("/api/report.pdf")
def report_pdf():
    report_data=report(); result=report_data["workflow"]
    data=BytesIO(); pdf=canvas.Canvas(data, pagesize=letter); width,height=letter; y=height-52
    pdf.setTitle("AquaSentinel Water Network Report")
    pdf.setFont("Helvetica-Bold",16); pdf.drawString(48,y,"AquaSentinel | Water Network Report"); y-=28
    pdf.setFont("Helvetica",9); pdf.drawString(48,y,"Demo snapshot — 2026-10-03 02:00 UTC · Decision support only"); y-=24
    pdf.setFont("Helvetica-Bold",11); pdf.drawString(48,y,"Zone water balance"); y-=17
    pdf.setFont("Helvetica",9)
    for row in result["agents"]["water_balance"]["zones"]:
        pdf.drawString(48,y,f"{row['zone']}: inflow {row['inflow_m3h']} m3/h | demand {row['estimated_consumption_m3h']} m3/h | unexplained {row['unexplained_difference_m3h']} m3/h ({row['difference_percent']}%)"); y-=15
    y-=9; pdf.setFont("Helvetica-Bold",11); pdf.drawString(48,y,"Demand forecast"); y-=17; pdf.setFont("Helvetica",9)
    forecast=result["agents"]["forecasting"]
    for zone,value in forecast["hourly_expected_demand_m3h"].items(): pdf.drawString(48,y,f"{zone}: expected {value:.1f} m3/h"); y-=14
    pdf.drawString(48,y,f"Synthetic holdout MAE/RMSE: {forecast['holdout_metrics']['mae']} / {forecast['holdout_metrics']['rmse']} m3/h"); y-=22
    pdf.setFont("Helvetica-Bold",11); pdf.drawString(48,y,"Flow and pressure anomalies"); y-=17; pdf.setFont("Helvetica",9)
    for anomaly in result["agents"]["anomaly_detection"]["anomalies"]:
        if y<70: pdf.showPage(); y=height-52; pdf.setFont("Helvetica",9)
        pdf.drawString(48,y,f"{anomaly['zone']} · {anomaly['type']} · observed {anomaly.get('observed',anomaly.get('current',''))}"); y-=14
    y-=8; pdf.setFont("Helvetica-Bold",11); pdf.drawString(48,y,"Leak hypotheses and evidence"); y-=18; pdf.setFont("Helvetica",9)
    for leak in result["agents"]["leak_localization"]["hypotheses"]:
        for line in [f"{leak['leak_id']} · {leak['zone']} · {leak['status']} · heuristic confidence {leak['confidence']}",f"Suspected section: {leak['suspected_section']} (inferred; field confirmation required)",*(["Evidence: "+"; ".join(leak["evidence"])]),"Verification: "+leak["verification_required"]]:
            if y<70: pdf.showPage(); y=height-52; pdf.setFont("Helvetica",9)
            pdf.drawString(48,y,line[:115]); y-=14
    y-=8; pdf.setFont("Helvetica-Bold",11); pdf.drawString(48,y,"Operational recommendation"); y-=17; pdf.setFont("Helvetica",9)
    for line in [result["agents"]["operations_coordination"]["summary"],result["agents"]["operations_coordination"]["recommendation"],"Water balance is simplified; full NRW needs authorized unbilled use, apparent losses, meter accuracy and adjustment rules.","No physical infrastructure controls are operated by this prototype."]:
        if y<70: pdf.showPage(); y=height-52; pdf.setFont("Helvetica",9)
        pdf.drawString(48,y,line[:115]); y-=15
    y-=8; pdf.setFont("Helvetica-Bold",11); pdf.drawString(48,y,"Field actions and data quality"); y-=17; pdf.setFont("Helvetica",9)
    pdf.drawString(48,y,f"Stored readings: {report_data['sensor_quality']['readings_included']} · invalid {report_data['sensor_quality']['invalid_count']} · stale {report_data['sensor_quality']['stale_count']}"); y-=14
    for action in report_data["field_actions"][:8]:
        if y<70: pdf.showPage(); y=height-52; pdf.setFont("Helvetica",9)
        pdf.drawString(48,y,f"{action['leak_id']} · {action['team_id'] or 'unassigned'} · {action['status']} · {action['observation'][:50]}"); y-=14
    pdf.save(); data.seek(0)
    return StreamingResponse(data,media_type="application/pdf",headers={"Content-Disposition":"attachment; filename=aquasentinel-report.pdf"})

@app.post("/api/ingest/csv")
async def ingest_csv(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400,detail="Upload a .csv sensor file")
    try:
        content=(await file.read()).decode("utf-8-sig")
        rows=list(csv.DictReader(StringIO(content)))
        required={"sensor_id","zone","type","timestamp","value","unit"}
        if not rows or not required.issubset(rows[0]): raise ValueError("Required columns: sensor_id, zone, type, timestamp, value, unit")
        readings=[]; rejected=[]; seen=set()
        for row in rows:
            try:
                value=float(row["value"]); key=(row["sensor_id"],row["timestamp"])
                if key in seen: rejected.append({"row":row,"reason":"duplicate sensor timestamp"}); continue
                seen.add(key)
                expected={"flow":"m3/h","pressure":"bar","tank_level":"%"}.get(row["type"])
                maximum={"flow":10000,"pressure":20,"tank_level":100}.get(row["type"],0)
                if value<0 or not expected or value>maximum or row["unit"]!=expected:
                    rejected.append({"row":row,"reason":"negative value or unsupported/type-mismatched unit"}); continue
                parsed={**row,"value":value}
                readings.append(parsed)
            except (TypeError,ValueError): rejected.append({"row":row,"reason":"invalid numeric value"})
        stored=[]
        with connect() as db:
            for r in readings:
                valid=True
                try:
                    parsed=datetime.fromisoformat(r["timestamp"].replace("Z","+00:00"))
                    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
                    if db.execute("SELECT 1 FROM readings WHERE sensor_id=? AND timestamp=?",(r["sensor_id"],r["timestamp"])).fetchone():
                        rejected.append({"row":r,"reason":"duplicate sensor timestamp already stored"}); continue
                    quality="invalid" if parsed>datetime.now(timezone.utc)+timedelta(minutes=5) else ("stale" if (datetime.now(timezone.utc)-parsed).total_seconds()>1800 else "valid")
                    db.execute("INSERT INTO readings(sensor_id,zone,type,timestamp,value,unit,quality) VALUES(?,?,?,?,?,?,?)",(r["sensor_id"],r["zone"],r["type"],r["timestamp"],r["value"],r["unit"],quality)); stored.append({**r,"data_quality":quality})
                except ValueError: rejected.append({"row":r,"reason":"timestamp is not ISO 8601"})
            log_event(db,"csv_ingestion",{"accepted":len(stored),"rejected":len(rejected),"filename":file.filename})
        workflow=run_pipeline(current_demo())
        with connect() as db: log_event(db,"dynamic_reassessment",workflow)
        return {"accepted_count":len(stored),"rejected_count":len(rejected),"readings":stored,"rejected":rejected,"workflow":workflow,"note":"Validated readings are persisted and the workflow is reassessed."}
    except UnicodeDecodeError: raise HTTPException(status_code=400,detail="CSV must be UTF-8 encoded")
    except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
