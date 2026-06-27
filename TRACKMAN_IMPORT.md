# Logging a Tee Box (Trackman) session

`launch_monitor.csv` is the **measured** source of truth. When it has data for a club,
the dashboard uses your real Trackman carry for that club's bag number (overrides the
Arccos-modeled estimate), and the **face-to-path** numbers light up the swing-pattern
coaching with hard data instead of an on-course estimate.

It's empty right now (just the header) — that's expected. Fill it after a session.

## How to fill it

One row per shot (preferred) or one row per club-average. Only `club` and `carry_yd` are
required; everything else is optional but recommended — especially the **path/face block**,
which is what diagnoses your hook.

### Column = Trackman parameter

| CSV column        | Trackman parameter | Notes |
|-------------------|--------------------|-------|
| `date`            | (session date)     | YYYY-MM-DD |
| `club`            | Club               | **Must match your bag names exactly**: `Driver`, `3 Wood`, `Hybrid`, `5 Iron` … `60 Wedge`, `Putter` |
| `carry_yd`        | Carry              | **Required.** Drives the bag number. |
| `total_yd`        | Total              | |
| `club_mph`        | Club Speed         | |
| `ball_mph`        | Ball Speed         | |
| `smash`           | Smash Factor       | |
| `launch_deg`      | Launch Angle       | vertical launch |
| `launch_dir_deg`  | Launch Direction   | + = right of target, − = left |
| `spin_rpm`        | Spin Rate          | |
| `spin_axis_deg`   | Spin Axis          | + = right tilt (fade), − = left tilt (**draw/hook**) |
| `attack_deg`      | Attack Angle       | + = up, − = down |
| `club_path_deg`   | Club Path          | + = in-to-out (right), − = out-to-in (left) |
| `face_angle_deg`  | Face Angle         | + = open (right), − = closed (left) |
| `face_to_path_deg`| Face to Path       | **the hook diagnostic** — + = open to path (fade/slice), − = closed to path (**draw/hook**) |
| `notes`           | —                  | anything (ball, conditions, "stock 7i") |

### What we expect to see for your hook
A consistent left miss across the bag should show up as **negative `face_to_path_deg`**
(face closed to path) and/or **negative `spin_axis_deg`**. The goal is to get
`face_to_path` toward **~0°**. Once a session is logged, the Swing-pattern card on the
Game-plan tab will print "Measured: face averages X° closed to the path" and your bag
numbers switch from modeled to measured.

### Example row
```
2026-07-12,7 Iron,168,176,86,123,1.43,17.8,1.2,6400,-3.1,-3.5,2.1,-2.4,-4.5,stock swing
```

### After you paste it in
Run the weekly refresh (or `golf-dashboard-refresh.cmd`), or rebuild directly:
```
$env:GOLF_INCLUDE_GPS="1"; .venv\Scripts\python dashboard\gen_tracker.py . docs\index.html
```
The build prefers your measured carries automatically and keeps the bag strictly
descending. Then tell me and I'll wire the per-club face-to-path into the by-club tab too.
