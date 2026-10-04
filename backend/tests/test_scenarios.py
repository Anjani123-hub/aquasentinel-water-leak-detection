"""Eight brief-aligned demonstration scenarios. Run from backend with pytest -q."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.agents.core import run_pipeline
from app.main import app

client=TestClient(app)

def scenario(flow=None,pressure=None,readings=None):
    return {"inflow":flow or {"DMA-A":420,"DMA-B":390,"DMA-C":350},
            "demand":{"DMA-A":405,"DMA-B":390,"DMA-C":342},
            "pressure":pressure or {"DMA-A":4.2,"DMA-B":4.0,"DMA-C":4.0},
            "readings":readings or []}

def analyze(payload): return run_pipeline(payload)

def test_tc01_normal_conditions_do_not_create_leak_alert():
    result=analyze(scenario())
    assert result["agents"]["leak_localization"]["hypotheses"]==[]

def test_tc02_sudden_flow_increase_is_flagged():
    result=analyze(scenario(flow={"DMA-A":420,"DMA-B":650,"DMA-C":350}))
    assert any(a["type"]=="high_flow" and a["zone"]=="DMA-B" for a in result["agents"]["anomaly_detection"]["anomalies"])

def test_tc03_downstream_pressure_drop_is_flagged():
    result=analyze(scenario(pressure={"DMA-A":4.0,"DMA-B":2.5,"DMA-C":4.0}))
    assert any(a["type"]=="low_pressure" and a["zone"]=="DMA-B" for a in result["agents"]["anomaly_detection"]["anomalies"])

def test_tc04_flow_and_pressure_create_correlated_hypothesis():
    result=analyze(scenario(flow={"DMA-A":420,"DMA-B":650,"DMA-C":350},pressure={"DMA-A":4.0,"DMA-B":2.5,"DMA-C":4.0}))
    hypotheses=result["agents"]["leak_localization"]["hypotheses"]
    assert hypotheses and hypotheses[0]["zone"]=="DMA-B"
    assert len(hypotheses[0]["evidence"])>=2

def test_tc05_persistent_night_flow_signal_is_reported():
    result=analyze(scenario(flow={"DMA-A":420,"DMA-B":650,"DMA-C":350}))
    assert any(a["type"]=="night_flow_increase" and a["zone"]=="DMA-B" for a in result["agents"]["anomaly_detection"]["anomalies"])

def test_tc06_impossible_pressure_is_rejected_from_validated_readings():
    bad={"sensor_id":"P-BAD","zone":"DMA-B","type":"pressure","timestamp":"2026-10-03T02:00:00Z","value":-3,"unit":"bar"}
    result=analyze(scenario(readings=[bad]))
    assert result["agents"]["monitoring"]["rejected"][0]["quality_reason"]=="negative_value"

def test_tc07_new_evidence_changes_reassessment_result():
    response=client.post("/api/reassess",json={"inflow":{"DMA-A":420,"DMA-B":650,"DMA-C":350},"pressure":{"DMA-A":4.0,"DMA-B":2.5,"DMA-C":4.0},"readings":[]})
    assert response.status_code==200
    body=response.json()
    assert body["workflow_state"]=="awaiting_operator_review"
    assert body["agents"]["leak_localization"]["hypotheses"][0]["confidence"]>0.5

def test_tc08_post_repair_comparison_reports_observed_improvement():
    response=client.post("/api/post-repair",json={"before_flow":150,"after_flow":92,"before_pressure":2.7,"after_pressure":4.0})
    assert response.status_code==200
    body=response.json()
    assert body["flow_reduction_percent"]==38.7
    assert body["pressure_change_bar"]==1.3
    assert body["observed_improvement"] is True
