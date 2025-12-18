from flask import Flask, render_template, jsonify, request, Response
import threading
import random
import datetime
import pandas as pd
import io
import base64
import matplotlib.pyplot as plt

app = Flask(__name__)

# =====================================================
# CONFIG
# =====================================================
NUM_COUNTERS = 10
TICK_SECONDS = 1

CATEGORIES = {
    "Groceries": 5,
    "Produce": 4,
    "Bakery": 3,
    "Electronics": 12,
    "Frozen": 6,
    "Pharmacy": 7
}

# =====================================================
# COUNTER PROFILES
# =====================================================
def generate_counter_profiles(n):
    specs = ["General", "Produce", "Speedy", "Electronics", "Bakery", "Frozen"]
    profiles = []
    for i in range(n):
        spec = random.choice(specs)
        multipliers = {c: 1.0 for c in CATEGORIES}

        if spec == "Speedy":
            for c in multipliers:
                multipliers[c] = 0.85
        elif spec in multipliers:
            multipliers[spec] = 0.7

        profiles.append({
            "id": i + 1,
            "name": f"Counter {i + 1}",
            "spec": spec,
            "multipliers": multipliers
        })
    return profiles

COUNTER_PROFILES = generate_counter_profiles(NUM_COUNTERS)

# =====================================================
# STATE
# =====================================================
state_lock = threading.Lock()
counters = {str(i + 1): [] for i in range(NUM_COUNTERS)}
paused = {str(i + 1): False for i in range(NUM_COUNTERS)}
pending = []
logs = []
next_trolley_id = 1
last_assignment = {}

# =====================================================
# HELPERS
# =====================================================
def compute_service_time(trolley, profile):
    total = 8
    for it in trolley["items"]:
        base = CATEGORIES[it["category"]]
        total += base * profile["multipliers"][it["category"]] * it["qty"]
    return round(total, 1)

def expected_finish(queue, trolley, profile):
    return sum(t.get("remaining_seconds", t["service_time"]) for t in queue) + \
           compute_service_time(trolley, profile)

def count_items(trolley):
    return sum(it["qty"] for it in trolley["items"])

def make_random_trolley(tid):
    cats = random.sample(list(CATEGORIES.keys()), random.randint(1, 4))
    return {
        "id": tid,
        "items": [{"category": c, "qty": random.randint(1, 5)} for c in cats],
        "created_at": datetime.datetime.now().isoformat()
    }

# =====================================================
# LOGGING
# =====================================================
def log_assignment(trolley, counter_id, service_time, est_finish, assigned_at):
    logs.append({
        "trolley_id": trolley["id"],
        "created_at": trolley["created_at"],
        "assigned_at": assigned_at,
        "counter_id": counter_id,
        "service_time": service_time,
        "est_finish_seconds": est_finish,
        "items_in_cart": count_items(trolley),
        "completed_at": None
    })

def log_completion(tid, completed_at):
    for r in logs:
        if r["trolley_id"] == tid:
            r["completed_at"] = completed_at
            return

# =====================================================
# ANALYTICS HELPERS
# =====================================================
def get_logs_df():
    df = pd.DataFrame(logs)
    if df.empty:
        return df

    for col in ["created_at", "assigned_at", "completed_at"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    df["actual_service_seconds"] = (
        df["completed_at"] - df["assigned_at"]
    ).dt.total_seconds()

    df["wait_before_assignment_seconds"] = (
        df["assigned_at"] - df["created_at"]
    ).dt.total_seconds()

    df["total_time_in_mart_minutes"] = (
        df["completed_at"] - df["created_at"]
    ).dt.total_seconds() / 60

    return df.dropna()

def compute_kpis(df):
    if df.empty:
        return {"avg_wait": 0, "avg_mart_time": 0, "peak_hour": "-", "avg_util": 0}

    avg_wait = round(df["wait_before_assignment_seconds"].mean(), 1)
    avg_mart_time = round(df["total_time_in_mart_minutes"].mean(), 1)

    peak_hour = df["created_at"].dt.hour.value_counts().idxmax()

    total_time = (
        df["completed_at"].max() - df["created_at"].min()
    ).total_seconds()

    util = (
        df.groupby("counter_id")["actual_service_seconds"].sum()
        / total_time
    ) * 100

    avg_util = round(util.mean(), 1)

    return {
        "avg_wait": avg_wait,
        "avg_mart_time": avg_mart_time,
        "peak_hour": peak_hour,
        "avg_util": avg_util
    }

def get_avg_wait_by_counter():
    df = get_logs_df()
    if df.empty:
        return {}
    return (
        df.groupby("counter_id")["wait_before_assignment_seconds"]
        .mean()
        .round(1)
        .to_dict()
    )

# =====================================================
# ROUTES – LIVE SYSTEM
# =====================================================
@app.route("/")
def index():
    return render_template(
        "index.html",
        num_counters=NUM_COUNTERS,
        profiles=COUNTER_PROFILES
    )

@app.route("/state")
def state():
    with state_lock:
        return jsonify({
            "counters": counters,
            "pending": pending,
            "paused": paused,
            "last_assignment": last_assignment,
            "avg_wait": get_avg_wait_by_counter()
        })

@app.route("/add_random", methods=["POST"])
def add_random():
    global next_trolley_id
    with state_lock:
        pending.append(make_random_trolley(next_trolley_id))
        next_trolley_id += 1
    return ("", 204)

@app.route("/add_manual", methods=["POST"])
def add_manual():
    global next_trolley_id
    data = request.json
    items = []

    for line in data["items"].splitlines():
        if ":" in line:
            c, q = line.split(":")
            items.append({"category": c.strip(), "qty": int(q)})

    with state_lock:
        pending.append({
            "id": next_trolley_id,
            "items": items,
            "created_at": datetime.datetime.now().isoformat()
        })
        next_trolley_id += 1

    return jsonify({"message": "Trolley added"})

@app.route("/assign_next", methods=["POST"])
def assign_next():
    global last_assignment
    with state_lock:
        if not pending:
            return ("", 204)

        trolley = pending.pop(0)
        best = None

        for p in COUNTER_PROFILES:
            cid = str(p["id"])
            val = expected_finish(counters[cid], trolley, p)
            if best is None or val < best[1]:
                best = (cid, val, p)

        cid, est, profile = best
        st = compute_service_time(trolley, profile)

        trolley["service_time"] = st
        trolley["remaining_seconds"] = st
        counters[cid].append(trolley)

        ts = datetime.datetime.now().isoformat()
        last_assignment = {
            "trolley_id": trolley["id"],
            "counter_id": int(cid),
            "est_finish_seconds": est
        }

        log_assignment(trolley, int(cid), st, est, ts)

    return ("", 204)

@app.route("/finish/<int:cid>", methods=["POST"])
def finish(cid):
    with state_lock:
        if counters[str(cid)]:
            t = counters[str(cid)].pop(0)
            log_completion(t["id"], datetime.datetime.now().isoformat())
    return ("", 204)

@app.route("/toggle_pause/<int:cid>", methods=["POST"])
def toggle_pause(cid):
    with state_lock:
        paused[str(cid)] = not paused[str(cid)]
    return ("", 204)

@app.route("/tick", methods=["POST"])
def tick():
    with state_lock:
        for cid in counters:
            if paused[cid] or not counters[cid]:
                continue
            t = counters[cid][0]
            t["remaining_seconds"] -= TICK_SECONDS
            if t["remaining_seconds"] <= 0:
                counters[cid].pop(0)
                log_completion(t["id"], datetime.datetime.now().isoformat())
    return ("", 204)

# =====================================================
# CSV EXPORT
# =====================================================
@app.route("/export_logs")
def export_logs():
    df = pd.DataFrame(logs)
    for col in ["created_at", "assigned_at", "completed_at"]:
        df[col] = pd.to_datetime(df[col], errors="coerce") \
                    .dt.strftime("%d-%m-%Y %I:%M %p")

    df = df[
        [
            "trolley_id",
            "created_at",
            "assigned_at",
            "counter_id",
            "service_time",
            "est_finish_seconds",
            "items_in_cart",
            "completed_at"
        ]
    ]

    return Response(
        df.to_csv(index=False),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mart_logs.csv"}
    )

# =====================================================
# ANALYTICS DASHBOARD
# =====================================================
@app.route("/analytics")
def analytics():
    df = get_logs_df()
    if df.empty:
        return "<h3 style='padding:20px'>No analytics data yet</h3>"

    images = {}

    def save_fig(fig):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        buf.seek(0)
        out = base64.b64encode(buf.read()).decode()
        plt.close(fig)
        return out

    # Throughput
    fig, ax = plt.subplots()
    df.groupby("counter_id").size().plot(kind="bar", ax=ax)
    ax.set_title("Throughput per Counter")
    images["throughput"] = save_fig(fig)

    # Avg wait
    fig, ax = plt.subplots()
    df.groupby("counter_id")["wait_before_assignment_seconds"].mean().plot(kind="bar", ax=ax)
    ax.set_title("Average Wait Time")
    images["wait"] = save_fig(fig)

    # Items vs service
    fig, ax = plt.subplots()
    ax.scatter(df["items_in_cart"], df["actual_service_seconds"])
    ax.set_title("Items vs Service Time")
    images["items_vs_service"] = save_fig(fig)

    # Routing accuracy
    fig, ax = plt.subplots()
    ax.scatter(df["est_finish_seconds"], df["actual_service_seconds"])
    ax.set_title("Routing Accuracy")
    images["routing_accuracy"] = save_fig(fig)

    # Peak hours trend
    hourly = df["created_at"].dt.hour.value_counts().sort_index()
    fig, ax = plt.subplots()
    ax.plot(hourly.index, hourly.values, marker="o")
    ax.set_title("Peak Hours – Arrival Trend")
    ax.set_xlabel("Hour of Day")
    ax.set_ylabel("Trolleys")
    ax.set_xticks(range(0, 24))
    images["peak_hours"] = save_fig(fig)

    # Total time
    fig, ax = plt.subplots()
    ax.hist(df["total_time_in_mart_minutes"], bins=15)
    ax.set_title("Total Time in Mart")
    images["total_time"] = save_fig(fig)

    # Utilization
    total_time = (df["completed_at"].max() - df["created_at"].min()).total_seconds()
    util = df.groupby("counter_id")["actual_service_seconds"].sum() / total_time * 100
    fig, ax = plt.subplots()
    util.plot(kind="bar", ax=ax)
    ax.set_title("Counter Utilization (%)")
    images["utilization"] = save_fig(fig)

    # Avg service vs items
    fig, ax = plt.subplots()
    df.groupby("items_in_cart")["actual_service_seconds"].mean().plot(marker="o", ax=ax)
    ax.set_title("Avg Service Time vs Items")
    images["avg_service_by_items"] = save_fig(fig)

    kpis = compute_kpis(df)

    return render_template(
        "analytics.html",
        images=images,
        kpis=kpis
    )

# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    app.run(debug=True)
