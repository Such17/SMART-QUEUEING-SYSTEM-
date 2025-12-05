# app.py
from flask import Flask, render_template, jsonify, request
import threading
import random
import datetime

app = Flask(__name__)

# ---------------- Configuration ----------------
NUM_COUNTERS = 10

CATEGORIES = {
    "Groceries": 5.0,
    "Produce": 4.0,
    "Bakery": 3.0,
    "Electronics": 12.0,
    "Frozen": 6.0,
    "Pharmacy": 7.0
}

TICK_SECONDS = 1.0  # how many simulated seconds per tick (client calls /tick once per second when auto on)

# ---------------- Counter profiles ----------------
def generate_counter_profiles(n):
    specs = ["General", "Produce", "Speedy", "Electronics", "Bakery", "Frozen"]
    profiles = []
    for i in range(n):
        spec = random.choice(specs)
        multipliers = {c: 1.0 for c in CATEGORIES}
        if spec == "Produce":
            multipliers["Produce"] = 0.7
            multipliers["Groceries"] = 0.9
        elif spec == "Speedy":
            for c in multipliers:
                multipliers[c] = 0.85
        elif spec == "Electronics":
            multipliers["Electronics"] = 0.6
            multipliers["Pharmacy"] = 0.9
        elif spec == "Bakery":
            multipliers["Bakery"] = 0.6
            multipliers["Produce"] = 0.9
        elif spec == "Frozen":
            multipliers["Frozen"] = 0.75
        profiles.append({
            "id": i+1,
            "name": f"Counter {i+1}",
            "spec": spec,
            "multipliers": multipliers
        })
    return profiles

COUNTER_PROFILES = generate_counter_profiles(NUM_COUNTERS)

# ---------------- State ----------------
state_lock = threading.Lock()
counters = {str(p["id"]): [] for p in COUNTER_PROFILES}  # queues per counter
pending = []  # pending trolley list
paused = {str(p["id"]): False for p in COUNTER_PROFILES}  # paused flags per counter
next_trolley_id = 1
last_assignment = {}
last_completed = {}

# ---------------- Helpers ----------------
def compute_service_time(trolley, profile):
    total = 0.0
    for it in trolley["items"]:
        base = CATEGORIES.get(it["category"], 5.0)
        mult = profile["multipliers"].get(it["category"], 1.0)
        total += base * mult * it["qty"]
    total += 8.0  # fixed overhead per trolley
    return float(total)

def expected_finish_if_assigned(queue, trolley, profile):
    wait = 0.0
    for t in queue:
        rem = t.get("remaining_seconds")
        st = t.get("service_time") or compute_service_time(t, profile)
        wait += (rem if rem is not None else st)
    wait += compute_service_time(trolley, profile)
    return wait

def make_random_trolley(tid):
    cats = random.sample(list(CATEGORIES.keys()), k=random.randint(1, 4))
    items = [{"category": c, "qty": random.randint(1, 6)} for c in cats]
    return {"id": tid, "items": items, "created_at": datetime.datetime.now().isoformat()}

def normalize_counters():
    global counters, paused
    for p in COUNTER_PROFILES:
        if str(p["id"]) not in counters:
            counters[str(p["id"])] = []
        if str(p["id"]) not in paused:
            paused[str(p["id"])] = False

# ---------------- Routes / API ----------------

@app.route('/')
def index():
    normalize_counters()
    # pass NUM_COUNTERS and profiles to the template
    return render_template('index.html',
                           num_counters=NUM_COUNTERS,
                           profiles=COUNTER_PROFILES)

@app.route('/state')
def get_state():
    normalize_counters()
    with state_lock:
        cpy_counters = {k: [t.copy() for t in v] for k, v in counters.items()}
        cpy_pending = [p.copy() for p in pending]
        return jsonify({
            "counters": cpy_counters,
            "pending": cpy_pending,
            "last_assignment": last_assignment.copy() if last_assignment else {},
            "last_completed": last_completed.copy() if last_completed else {},
            "profiles": COUNTER_PROFILES,
            "paused": paused
        })

@app.route('/add_random', methods=['POST'])
def add_random():
    global next_trolley_id
    with state_lock:
        tid = next_trolley_id
        t = make_random_trolley(tid)
        pending.append(t)
        next_trolley_id += 1
    return ('', 204)

@app.route('/add_manual', methods=['POST'])
def add_manual():
    global next_trolley_id
    data = request.get_json() or {}
    tid_val = data.get('tid')
    items_text = data.get('items', '')
    lines = [ln.strip() for ln in items_text.splitlines() if ln.strip()]
    if not lines:
        return jsonify({"message":"Provide at least one item line (Category:qty)."}), 400
    items = []
    for ln in lines:
        if ':' in ln:
            cat, qty = ln.split(':', 1)
            cat = cat.strip()
            try:
                qty = max(1, int(qty.strip()))
            except:
                qty = 1
            matches = [c for c in CATEGORIES if c.lower().startswith(cat.lower())]
            if matches:
                cat = matches[0]
            elif cat not in CATEGORIES:
                cat = random.choice(list(CATEGORIES.keys()))
            items.append({"category": cat, "qty": qty})
        else:
            items.append({"category": random.choice(list(CATEGORIES.keys())), "qty": 1})
    with state_lock:
        if tid_val:
            try:
                tid = int(tid_val)
                if tid >= next_trolley_id:
                    next_trolley_id = tid + 1
            except:
                tid = next_trolley_id
                next_trolley_id += 1
        else:
            tid = next_trolley_id
            next_trolley_id += 1
        trolley = {"id": tid, "items": items, "created_at": datetime.datetime.now().isoformat()}
        pending.append(trolley)
    return jsonify({"message": f"Added Trolley {tid}: {', '.join([f'{it['category']} x{it['qty']}' for it in items])}"}), 200

@app.route('/assign_next', methods=['POST'])
def assign_next():
    global last_assignment
    with state_lock:
        if not pending:
            last_assignment = {"message": "No pending trolleys"}
            return ('', 204)
        trolley = pending.pop(0)
        best_cid = None
        best_val = None
        for profile in COUNTER_PROFILES:
            cid = str(profile['id'])
            q = counters.get(cid, [])
            val = expected_finish_if_assigned(q, trolley, profile)
            if best_val is None or val < best_val:
                best_val = val
                best_cid = cid
        chosen_profile = next(p for p in COUNTER_PROFILES if str(p['id']) == best_cid)
        st = compute_service_time(trolley, chosen_profile)
        assigned = trolley.copy()
        assigned['service_time'] = round(st, 1)
        assigned['remaining_seconds'] = round(st, 1)
        counters[best_cid] = counters.get(best_cid, []) + [assigned]
        last_assignment = {
            "trolley_id": assigned['id'],
            "counter_id": int(best_cid),
            "assignment_time": datetime.datetime.now().isoformat(),
            "est_finish_seconds": round(best_val, 1)
        }
    return ('', 204)

@app.route('/finish/<int:counter_id>', methods=['POST'])
def finish_head(counter_id):
    global last_completed
    cid = str(counter_id)
    with state_lock:
        q = counters.get(cid, [])
        if not q:
            return ('', 204)
        finished = q.pop(0)
        counters[cid] = q
        last_completed = {
            "trolley_id": finished['id'],
            "counter_id": counter_id,
            "completed_at": datetime.datetime.now().isoformat()
        }
    return ('', 204)

@app.route('/toggle_pause/<int:counter_id>', methods=['POST'])
def toggle_pause(counter_id):
    cid = str(counter_id)
    with state_lock:
        current = paused.get(cid, False)
        paused[cid] = not current
    return ('', 204)

@app.route('/tick', methods=['POST'])
def tick():
    global last_completed
    with state_lock:
        for p in COUNTER_PROFILES:
            cid = str(p['id'])
            if paused.get(cid, False):
                continue
            q = counters.get(cid, [])
            if not q:
                continue
            head = q[0]
            rem = float(head.get('remaining_seconds', head.get('service_time', 0.0)))
            rem -= TICK_SECONDS
            if rem <= 0:
                finished = q.pop(0)
                last_completed = {
                    "trolley_id": finished['id'],
                    "counter_id": int(cid),
                    "completed_at": datetime.datetime.now().isoformat()
                }
                counters[cid] = q
            else:
                q[0]['remaining_seconds'] = round(rem, 1)
                counters[cid] = q
    return ('', 204)

if __name__ == '__main__':
    print("Starting Mart Counter Routing Flask app on http://127.0.0.1:5000")
    app.run(debug=True)
