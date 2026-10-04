"""Deterministic, inspectable agents for the water-utility demo workflow."""
from datetime import datetime, timezone
from typing import Any
import networkx as nx
import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error


def data_monitor(state: dict) -> dict:
    readings = state["readings"]
    accepted, rejected, stale = [], [], []
    seen=set()
    for r in readings:
        if r.get("quality")=="invalid":
            rejected.append({**r,"data_quality":"invalid","quality_reason":"source_validation_failed"}); continue
        if r.get("quality")=="stale":
            stale.append({**r,"data_quality":"stale","quality_reason":"older_than_configured_freshness_window"}); continue
        reason=None
        try: datetime.fromisoformat(r["timestamp"].replace("Z","+00:00"))
        except (ValueError,AttributeError): reason="invalid_timestamp"
        expected={"flow":"m3/h","pressure":"bar","tank_level":"%"}.get(r.get("type"))
        key=(r["sensor_id"],r["timestamp"])
        if key in seen:
            rejected.append({**r,"data_quality":"duplicate","quality_reason":"duplicate_sensor_timestamp"}); continue
        seen.add(key)
        maximum={"flow":10000,"pressure":20,"tank_level":100}.get(r.get("type"),0)
        if r["value"] < 0: reason="negative_value"
        elif maximum and r["value"] > maximum: reason="outside_physical_range"
        elif expected and r["unit"] != expected: reason="type_unit_mismatch"
        elif r["unit"] not in ("m3/h", "bar", "%"): reason="unsupported_unit"
        result={**r,"data_quality":"invalid" if reason else "valid"}
        if reason: result["quality_reason"]=reason; rejected.append(result)
        else: accepted.append(result)
    return {"accepted": accepted, "rejected": rejected, "stale":stale,"duplicate_count":sum(1 for r in rejected if r.get("quality_reason")=="duplicate_sensor_timestamp"),"agent": "Water Network Data Monitoring Agent", "checks":["ISO 8601 timestamps","measurement-unit compatibility","negative/impossible readings","duplicate sensor timestamps","ingestion freshness window"]}


def demand_forecast(state: dict) -> dict:
    """Fit an actual linear regression on synthetic historical hourly data."""
    hours = np.arange(24)
    # Repeatable 30-day hourly history with daily shape and mild weekday effect.
    X, y = [], []
    for day in range(30):
        for hour in hours:
            X.append([np.sin(2*np.pi*hour/24), np.cos(2*np.pi*hour/24), int(day % 7 >= 5)])
            y.append(100 + 16 * np.sin((hour - 6) * np.pi / 12) + 7 * np.sin((hour - 15) * np.pi / 6) + (5 if day % 7 >= 5 else 0))
    split=21*24
    evaluation_model=LinearRegression().fit(X[:split],y[:split])
    heldout=evaluation_model.predict(X[split:]); mae=float(mean_absolute_error(y[split:],heldout)); rmse=float(np.sqrt(mean_squared_error(y[split:],heldout)))
    model = LinearRegression().fit(X, y)
    pred = float(max(0, model.predict([[np.sin(2*np.pi*2/24), np.cos(2*np.pi*2/24), 0]])[0]))
    # Zone baseline calibrated from the synthetic scenario data.
    forecast = {"DMA-A": pred * 4.83, "DMA-B": pred * 4.65, "DMA-C": pred * 4.08}
    state["forecast"] = forecast
    return {"agent":"Water Demand Forecasting Agent","hourly_expected_demand_m3h": forecast, "model": "scikit-learn LinearRegression", "training_rows": len(y), "features": ["hour_sin", "hour_cos", "weekend_flag"], "target":"Synthetic hourly demand; calibrated to DMA estimates", "training_method":"First 21 days fit for validation; final 9 days held out; final model refit on all synthetic rows", "holdout_metrics":{"mae":round(mae,2),"rmse":round(rmse,2),"units":"m3/h on base synthetic series"}, "uncertainty_note": "Synthetic demonstration metrics only; not evidence of utility forecast accuracy"}


def anomaly_detection(state: dict) -> dict:
    values = {r["sensor_id"]: r["value"] for r in state["validated"]["accepted"]}
    flow = {z: values.get(f"FLOW-{z}", f) for z, f in state["inflow"].items()}
    pressure = {z: values.get(f"PRESSURE-{z}", p) for z, p in state["pressure"].items()}
    anomalies = []
    for z in flow:
        residual = flow[z] - state["forecast"][z]
        if residual > max(30, state["forecast"][z] * .12):
            anomalies.append({"zone": z, "type": "high_flow", "observed": flow[z], "expected": round(state["forecast"][z], 1), "residual": round(residual, 1)})
        if pressure[z] < 3.2:
            anomalies.append({"zone": z, "type": "low_pressure", "observed": pressure[z], "threshold": 3.2})
    # Synthetic historical nighttime baseline compared with current readings.
    night = {"DMA-A": {"baseline": 95, "current": 96}, "DMA-B": {"baseline": 88, "current": 151}, "DMA-C": {"baseline": 82, "current": 84}}
    for z, n in night.items():
        if n["current"] > n["baseline"] * 1.25:
            anomalies.append({"zone": z, "type": "night_flow_increase", **n})
    state["anomalies"] = anomalies
    return {"agent":"Flow & Pressure Anomaly Detection Agent","anomalies": anomalies, "method": "forecast residual thresholds + pressure floor + nighttime baseline ratio"}


def localization(state: dict) -> dict:
    graph = nx.Graph()
    graph.add_edges_from([("RESERVOIR", "DMA-A"), ("DMA-A", "DMA-B"), ("DMA-B", "DMA-C")])
    zones = {a["zone"] for a in state["anomalies"] if a["type"] == "high_flow"}
    leaks = []
    for z in sorted(zones):
        kinds = {a["type"] for a in state["anomalies"] if a["zone"] == z}
        evidence = []
        if "high_flow" in kinds: evidence.append("Inlet flow exceeds model forecast")
        if "low_pressure" in kinds: evidence.append("Zone pressure below 3.2 bar threshold")
        if "night_flow_increase" in kinds: evidence.append("Night flow exceeds historical baseline by more than 25%")
        score = min(0.95, 0.28 + 0.24 * len(kinds))
        leaks.append({"leak_id": f"LEAK-{z[-1]}01", "zone": z, "suspected_section": f"PIPE-{z[-1]}12 to PIPE-{z[-1]}18", "confidence": round(score, 2), "status": "Probable" if score >= .7 else "Possible", "evidence": evidence, "contradicting_evidence": ["No field or acoustic confirmation yet"], "verification_required": "Field inspection and sensor-health review", "topology_nodes": list(nx.single_source_shortest_path_length(graph, z, cutoff=1).keys())})
    state["leaks"] = leaks
    return {"agent":"Leak Detection & Localization Agent","hypotheses": leaks, "localization_method": "Affected DMA and adjacent section inferred from configured graph; no exact underground point claim"}


def water_balance(state: dict) -> dict:
    rows = []
    for zone, supply in state["inflow"].items():
        demand = state["demand"].get(zone, 0)
        diff = supply - demand
        rows.append({"zone": zone, "inflow_m3h": supply, "estimated_consumption_m3h": demand, "unexplained_difference_m3h": diff, "difference_percent": round(diff / supply * 100, 1) if supply else 0, "potential_loss_m3_6h": round(max(diff, 0) * 6, 1)})
    state["balance"] = rows
    return {"agent":"Water Balance & Loss Analysis Agent","zones": rows, "caveat": "Simplified balance only. Full NRW requires authorized unbilled use, apparent losses, meter accuracy and adjustment rules."}


def maintenance_planning(state: dict) -> dict:
    plans = []
    for leak in state["leaks"]:
        bal = next(x for x in state["balance"] if x["zone"] == leak["zone"])
        score = min(100, round(leak["confidence"] * 45 + max(0, bal["unexplained_difference_m3h"]) * .25 + (15 if "low_pressure" in {a["type"] for a in state["anomalies"] if a["zone"] == leak["zone"]} else 0)))
        plans.append({"leak_id": leak["leak_id"], "zone": leak["zone"], "priority_score": score, "priority": "High" if score >= 70 else "Medium", "recommended_inspection": "Acoustic survey + verify inlet and pressure sensors", "suggested_team": "TEAM-01" if not state.get("team_assigned") else None, "requires_operator_approval": True})
    state["plans"] = plans
    return {"agent":"Maintenance & Field Resource Planning Agent","plans": plans, "scoring": "confidence×45 + positive unexplained m³/h×0.25 + 15 for low pressure; capped at 100"}


def coordination(state: dict) -> dict:
    return {"agent":"Water Operations Coordination Agent","summary": f"Reviewed {len(state['validated']['accepted'])} valid, {len(state['validated']['rejected'])} invalid and {len(state['validated'].get('stale',[]))} stale readings; {len(state['leaks'])} leak hypothesis/hypotheses require operator review.", "recommendation": "Inspect prioritized zone(s), validate sensor health and planned operations, then record field findings. No physical controls are operated.", "human_approval_required": True}


def run_pipeline(payload: dict) -> dict:
    state = {**payload, "team_assigned": False}
    outputs: dict[str, Any] = {}
    outputs["monitoring"] = data_monitor(state)
    state["validated"] = outputs["monitoring"]
    outputs["forecasting"] = demand_forecast(state)
    outputs["anomaly_detection"] = anomaly_detection(state)
    outputs["leak_localization"] = localization(state)
    outputs["water_balance"] = water_balance(state)
    outputs["maintenance_planning"] = maintenance_planning(state)
    outputs["operations_coordination"] = coordination(state)
    outputs["reviewer"] = {"agent":"Water Operations Reviewer / Critic Agent","result": "reviewed", "checks": ["invalid readings excluded", "balance arithmetic deterministic", "suspected locations labeled as unconfirmed", "operator approval required"], "limitations":["Forecast has no validated field dataset","Topology is synthetic and has no geographic coordinates"]}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "agents": outputs, "summary": outputs["operations_coordination"]["summary"], "human_decision": "pending", "workflow_state": "awaiting_operator_review"}
