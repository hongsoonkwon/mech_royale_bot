#!/usr/bin/env python3
"""Mech Royale bot (mechfight/1). Standard library only.

Strategy: survive. Placement is scored, kills are not.
- Stay inside the zone early, with a margin, and move silently (< step_speed) when safe.
- Avoid NPC drones and supply drops (where fights happen).
- Pick up weapons and cubes only when no enemy is visible.
- When attacked: face the attacker (front armor), shoot back with lead,
  strafe, and retreat when outmatched.
"""
import json
import math
import os
import random
import sys
import time
from array import array

TWO_PI = 2.0 * math.pi


def log(*a):
    try:
        print(*a, file=sys.stderr, flush=True)
    except Exception:
        pass


def wrap(a):
    while a > math.pi:
        a -= TWO_PI
    while a < -math.pi:
        a += TWO_PI
    return a


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm2(x, y):
    d = math.hypot(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


def cfg(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


# ---------------------------------------------------------------------------
# Map: terrain heights + building occupancy grid
# ---------------------------------------------------------------------------
class WorldMap:
    def __init__(self):
        self.ok = False
        self.res = 2.0
        self.gx0 = self.gy0 = 0.0
        self.gnx = self.gny = 0
        self.top = None   # max building top per cell (-1e9 = free)
        self.base = None  # min building base per cell
        self.block = None  # 1 = can't walk through
        self.t_ok = False

    def load(self, hello, deadline):
        try:
            mdir = cfg(hello, "map", "dir", default="/arena")
            files = cfg(hello, "map", "files", default=["v2/world.json", "v2/terrain.i16"])
            wpath = tpath = None
            for f in files:
                if f.endswith(".json"):
                    wpath = os.path.join(mdir, f)
                elif f.endswith(".i16"):
                    tpath = os.path.join(mdir, f)
            if wpath is None:
                wpath = os.path.join(mdir, "v2/world.json")
            with open(wpath, "r") as fh:
                world = json.load(fh)
            ter = world.get("terrain", {}) or {}
            self.tnx = int(ter.get("nx", 1070))
            self.tny = int(ter.get("ny", 724))
            self.tminx = float(ter.get("minX", ter.get("minx", -535)))
            self.tminy = float(ter.get("minY", ter.get("miny", -362)))
            self.tstep = float(ter.get("step", 1.0))
            try:
                if tpath and os.path.exists(tpath):
                    h = array("h")
                    with open(tpath, "rb") as fh:
                        h.frombytes(fh.read())
                    if sys.byteorder != "little":
                        h.byteswap()
                    if len(h) >= self.tnx * self.tny:
                        self.terrain = h
                        self.t_ok = True
            except Exception as e:
                log("terrain load failed", e)

            self.gx0 = self.tminx
            self.gy0 = self.tminy
            self.gnx = int(math.ceil(self.tnx * self.tstep / self.res)) + 1
            self.gny = int(math.ceil(self.tny * self.tstep / self.res)) + 1
            n = self.gnx * self.gny
            self.top = array("f", [-1e9]) * n
            self.base = array("f", [1e9]) * n
            res = self.res
            for p in world.get("prisms", []) or []:
                if time.time() > deadline:
                    log("map load: deadline hit, partial grid")
                    break
                ring = p.get("ring") or []
                if len(ring) < 6:
                    continue
                top = float(p.get("top", 0))
                base = float(p.get("base", -1e3))
                xs = ring[0::2]
                ys = ring[1::2]
                m = min(len(xs), len(ys))
                xs = xs[:m]
                ys = ys[:m]
                j0 = max(0, int((min(ys) - self.gy0) / res))
                j1 = min(self.gny - 1, int((max(ys) - self.gy0) / res))
                edges = [(xs[k], ys[k], xs[(k + 1) % m], ys[(k + 1) % m]) for k in range(m)]
                for j in range(j0, j1 + 1):
                    yc = self.gy0 + (j + 0.5) * res
                    xi = []
                    for (ax, ay, bx, by) in edges:
                        if (ay <= yc < by) or (by <= yc < ay):
                            xi.append(ax + (yc - ay) * (bx - ax) / (by - ay))
                    if len(xi) < 2:
                        continue
                    xi.sort()
                    row = j * self.gnx
                    for k in range(0, len(xi) - 1, 2):
                        i0 = max(0, int(math.floor((xi[k] - self.gx0) / res - 0.5)))
                        i1 = min(self.gnx - 1, int(math.floor((xi[k + 1] - self.gx0) / res + 0.5)))
                        for i in range(i0, i1 + 1):
                            c = row + i
                            if top > self.top[c]:
                                self.top[c] = top
                            if base < self.base[c]:
                                self.base[c] = base
            # walk-blocking flags
            blk = bytearray(n)
            for j in range(self.gny):
                yc = self.gy0 + (j + 0.5) * res
                row = j * self.gnx
                for i in range(self.gnx):
                    c = row + i
                    t = self.top[c]
                    if t < -1e8:
                        continue
                    g = self.ground(self.gx0 + (i + 0.5) * res, yc)
                    # bridge with clearance is passable; low lips are steppable
                    if self.base[c] > g + 9.5:
                        continue
                    if t > g + 1.5:
                        blk[c] = 1
            self.block = blk
            # openness: on a coarse 8 m grid, how many of 12 sightlines run 30 m without a wall
            oc = 8.0
            self.oc = oc
            self.onx = int(self.gnx * res / oc) + 1
            self.ony = int(self.gny * res / oc) + 1
            dirs = [(math.cos(k * math.pi / 6), math.sin(k * math.pi / 6)) for k in range(12)]
            opn = bytearray(self.onx * self.ony)
            for oj in range(self.ony):
                y = self.gy0 + (oj + 0.5) * oc
                for oi in range(self.onx):
                    x = self.gx0 + (oi + 0.5) * oc
                    cnt = 0
                    for (cx, cy) in dirs:
                        free = True
                        for dd in (6.0, 12.0, 20.0, 30.0):
                            c2 = self.cell(x + cx * dd, y + cy * dd)
                            if c2 >= 0 and blk[c2]:
                                free = False
                                break
                        if free:
                            cnt += 1
                    opn[oj * self.onx + oi] = cnt
            self.open = opn
            # 4 m navigation grid: blocked if any 2 m cell in it is blocked
            f = max(1, int(round(4.0 / res)))
            self.nf = f
            self.nres = res * f
            self.nnx = self.gnx // f + 1
            self.nny = self.gny // f + 1
            nb = bytearray(self.nnx * self.nny)
            for j in range(self.gny):
                row = j * self.gnx
                nrow = (j // f) * self.nnx
                for i in range(self.gnx):
                    if blk[row + i]:
                        nb[nrow + i // f] = 1
            self.nblock = nb
            self.ok = True
            log("map loaded", self.gnx, self.gny)
        except Exception as e:
            log("map load failed", repr(e))
            self.ok = False

    def ground(self, x, y):
        if not self.t_ok:
            return 0.0
        i = int((x - self.tminx) / self.tstep)
        j = int((y - self.tminy) / self.tstep)
        if i < 0 or j < 0 or i >= self.tnx or j >= self.tny:
            return 0.0
        return self.terrain[j * self.tnx + i] / 100.0

    def cell(self, x, y):
        i = int((x - self.gx0) / self.res)
        j = int((y - self.gy0) / self.res)
        if i < 0 or j < 0 or i >= self.gnx or j >= self.gny:
            return -1
        return j * self.gnx + i

    def blocked(self, x, y, feet_z=None):
        if not self.ok:
            return False
        c = self.cell(x, y)
        if c < 0:
            return True  # map edge
        if not self.block[c]:
            return False
        if feet_z is not None and feet_z > self.top[c] - 0.5:
            return False  # we're on/above that roof
        return True

    def los(self, a, b, step=2.0):
        """a, b: (x, y, z). True if no building cell intersects the segment."""
        if not self.ok:
            return True
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        L = math.hypot(dx, dy)
        n = int(L / step)
        if n <= 1:
            return True
        for k in range(1, n):
            f = k / n
            c = self.cell(a[0] + dx * f, a[1] + dy * f)
            if c < 0:
                continue
            t = self.top[c]
            if t < -1e8:
                continue
            z = a[2] + dz * f
            if self.base[c] < z < t:
                return False
        return True

    def openness(self, x, y):
        """0 (enclosed) .. 1 (open plaza)."""
        if not self.ok or getattr(self, "open", None) is None:
            return 0.5
        oi = int((x - self.gx0) / self.oc)
        oj = int((y - self.gy0) / self.oc)
        if oi < 0 or oj < 0 or oi >= self.onx or oj >= self.ony:
            return 1.0
        return self.open[oj * self.onx + oi] / 12.0

    def path_clear(self, a, b):
        """Walkable straight line between a and b (2 m samples)."""
        if not self.ok:
            return True
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = int(math.hypot(dx, dy) / 2.0)
        for k in range(1, n + 1):
            f = k / max(1, n)
            if self.blocked(a[0] + dx * f, a[1] + dy * f):
                return False
        return True

    def find_path(self, a, b, max_nodes=2500):
        """A* on the 4 m grid. Returns world waypoints (partial path toward b if not reached)."""
        if not self.ok or getattr(self, "nblock", None) is None:
            return None
        R, nx, ny, nb = self.nres, self.nnx, self.nny, self.nblock

        def cel(p):
            return int((p[0] - self.gx0) / R), int((p[1] - self.gy0) / R)

        sx, sy = cel(a)
        gx, gy = cel(b)
        if not (0 <= sx < nx and 0 <= sy < ny and 0 <= gx < nx and 0 <= gy < ny):
            return None
        if nb[gy * nx + gx]:
            found = False
            for rr in range(1, 5):
                for ddx in range(-rr, rr + 1):
                    for ddy in (-rr, rr) if abs(ddx) != rr else range(-rr, rr + 1):
                        x2, y2 = gx + ddx, gy + ddy
                        if 0 <= x2 < nx and 0 <= y2 < ny and not nb[y2 * nx + x2]:
                            gx, gy, found = x2, y2, True
                            break
                    if found:
                        break
                if found:
                    break
        start, goal = sy * nx + sx, gy * nx + gx
        import heapq
        D2 = 1.4142

        def h(c):
            ddx, ddy = abs(c % nx - gx), abs(c // nx - gy)
            return max(ddx, ddy) + 0.4142 * min(ddx, ddy)

        openh = [(h(start), 0.0, start)]
        gcost = {start: 0.0}
        came = {}
        best, besth = start, h(start)
        nodes = 0
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, D2), (1, -1, D2), (-1, 1, D2), (-1, -1, D2))
        while openh and nodes < max_nodes:
            f_, gc, c = heapq.heappop(openh)
            if gc > gcost.get(c, 1e18):
                continue
            nodes += 1
            if c == goal:
                best = c
                break
            hc = f_ - gc
            if hc < besth:
                besth, best = hc, c
            cx, cy = c % nx, c // nx
            for ddx, ddy, w in nbrs:
                x2, y2 = cx + ddx, cy + ddy
                if not (0 <= x2 < nx and 0 <= y2 < ny):
                    continue
                c2 = y2 * nx + x2
                if nb[c2] and c2 != start:
                    continue
                if ddx and ddy and (nb[cy * nx + x2] or nb[y2 * nx + cx]):
                    continue  # no corner cutting
                g2 = gc + w
                if g2 < gcost.get(c2, 1e18):
                    gcost[c2] = g2
                    came[c2] = c
                    heapq.heappush(openh, (g2 + h(c2), g2, c2))
        path = []
        c = best
        while c != start and c in came:
            path.append((self.gx0 + (c % nx + 0.5) * R, self.gy0 + (c // nx + 0.5) * R))
            c = came[c]
        path.reverse()
        return path

    def near_wall(self, x, y, r=8.0):
        if not self.ok:
            return False
        for ang in range(0, 8):
            a = ang * math.pi / 4
            if self.blocked(x + r * math.cos(a), y + r * math.sin(a)):
                return True
        return False


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.config = hello.get("config", {}) or {}
        self.slot = hello.get("slot", 0)
        random.seed(hello.get("seed", 0))
        self.map = WorldMap()
        self.map.load(hello, time.time() + 12.0)
        mech = self.config.get("mech", {}) or {}
        self.step_speed = float(cfg(self.config, "sound", "step_speed", default=5.0))
        self.min_pitch = float(mech.get("min_pitch", -0.6))
        self.max_pitch = float(mech.get("max_pitch", 1.2))
        self.mortar_g = float(cfg(self.config, "weapons", "mortar", "gravity", default=22.0))
        self.last_pos = None
        self.stuck_t = 0.0
        self.unstick_until = -1.0
        self.unstick_dir = 1
        self.strafe_dir = random.choice([-1, 1])
        self.strafe_switch = 0.0
        self.threat = {}  # id -> (t, bearing, pos or None)
        self.last_hit_t = -99.0
        self.last_hit_bearing = None
        self.last_hit_by = None
        self.scan_yaw = None
        self.sound_look = None  # (t, bearing)
        self.cover_target = None
        self.cover_t = -99.0
        self.prev_t = 0.0
        self.recent_mech_seen = {}  # id -> t
        self.goal = None  # (kind, id, [x, y], t_set)
        self.enemy_pos = {}  # id -> (t, x, y)
        self.last_combat_t = -99.0
        self.reloc_done = True
        self._rel, self._rel_t = None, -99.0
        self.enemy_info = {}  # id -> (t, x, y, weapons, hp_frac)
        self.seen_pickups = {}  # id -> (t, kind, x, y)
        self._pp = (-99.0, None, None)
        self._path, self._path_goal, self._path_t = None, None, -99.0
        self._pp_prev = {}  # key -> (t, (x, y))  previous choice, for hysteresis
        self._zs = (-99.0, None)
        self._swap = {}  # pickup id -> slot to drop when taking it

    # ------------------------------------------------------------------
    def act(self, o):
        t = o.get("t", 0.0)
        me = o.get("self", {}) or {}
        pos = me.get("pos", [0, 0, 0])
        vel = me.get("vel", [0, 0, 0])
        aim = me.get("aim", [0, 0])
        hp = me.get("hp", 1)
        max_hp = me.get("max_hp", 1400) or 1400
        energy = me.get("energy", 0)
        grounded = me.get("grounded", True)
        stats = me.get("stats", {}) or {}
        walk_speed = float(stats.get("walk_speed", 11.7) or 11.7)
        muzzle_h = float(stats.get("muzzle_height", 7.0) or 7.0)
        eye_h = float(stats.get("eye_height", 8.0) or 8.0)
        weapons = me.get("weapons", []) or []
        hp_frac = hp / max_hp

        action = {"tick": o.get("tick")}
        self.shoot_obj = None
        self.shoot_kind = None

        # ---- events
        for ev in o.get("events", []) or []:
            if ev.get("type") == "damaged" and ev.get("by") is not None:
                self.last_hit_t = t
                self.last_hit_bearing = ev.get("bearing")
                self.last_hit_by = ev.get("by")

        # ---- upgrade offer
        offer = o.get("offer")
        if offer and offer.get("choices"):
            action["pick"] = self.pick_upgrade(offer["choices"])

        # ---- visible things
        enemies = []
        npcs = []
        for m in o.get("mechs", []) or []:
            if m.get("kind") == "npc":
                npcs.append(m)
            elif m.get("id") != me.get("id"):
                enemies.append(m)
                self.recent_mech_seen[m.get("id")] = t

        eye = (pos[0], pos[1], pos[2] + eye_h)
        muzzle = (pos[0], pos[1], pos[2] + muzzle_h)

        def tcenter(m):
            s = 1.0 + 0.05 * (m.get("cubes", 0) or 0)
            h = 4.5 * s if m.get("kind") != "npc" else 1.75
            p = m.get("pos", [0, 0, 0])
            return (p[0], p[1], p[2] + h)

        vis_enemies = []
        for m in enemies:
            d = dist2(pos, m["pos"])
            c = tcenter(m)
            m["_d"] = d
            m["_los"] = self.map.los(muzzle, c)
            vis_enemies.append(m)
        vis_enemies.sort(key=lambda m: m["_d"])

        # ---- choose target
        target = None
        recently_hit = (t - self.last_hit_t) < 3.0
        attacker = None
        if recently_hit and self.last_hit_by is not None:
            for m in vis_enemies + npcs:
                if m.get("id") == self.last_hit_by:
                    attacker = m
        cands = [m for m in vis_enemies if m["_los"] and m["_d"] < 230]
        if attacker is not None and (attacker.get("kind") == "npc" or attacker in cands):
            target = attacker
        elif cands:
            # prefer the weakest nearby
            target = min(cands, key=lambda m: m["_d"] * (0.5 + (m.get("hp", 1400) / max(1, m.get("max_hp", 1400)))))

        # ---- zone
        zone = o.get("zone", {}) or {}
        zc, zr = self.zone_now(zone, t)
        nc = zone.get("next_center", zc) or zc
        nr = float(zone.get("next_radius", zr) or 0.0)
        d_center = dist2(pos, zc)
        # endgame: the last circles leave nowhere to hide; staying out is certain death
        endgame = nr <= 25 or (o.get("alive", 16) <= 3 and nr <= 70)
        outside = d_center > zr - 3
        # safe spot: inside the next circle, toward us
        if nr > 15:
            dx, dy = norm2(pos[0] - nc[0], pos[1] - nc[1])
            safe_r = nr * 0.55
            safe = (nc[0] + dx * safe_r, nc[1] + dy * safe_r)
            in_next = dist2(pos, nc) < nr * 0.8
        else:
            safe = (nc[0], nc[1])
            in_next = dist2(pos, nc) < max(nr * 0.8, 4)
        shrink_start = float(zone.get("shrink_start", t + 60) or 0)
        shrink_end = float(zone.get("shrink_end", t + 90) or 0)

        # ---- movement intent
        mvx, mvy = 0.0, 0.0
        speed = 0.0  # 0..1 fraction of walk
        boost = False
        mode = "idle"

        # zone: ride the (moving) edge instead of walking into the middle, where
        # everyone else meets. zone_now() already looks a few seconds ahead.
        zone_vec = (0.0, 0.0)
        zone_urgency = 0.0
        if outside:
            zone_vec = norm2(zc[0] - pos[0], zc[1] - pos[1])
            zone_urgency = 2.0
        elif d_center > zr - 14:
            zone_vec = norm2(zc[0] - pos[0], zc[1] - pos[1])
            zone_urgency = 1.0

        threat_close = [m for m in vis_enemies if m["_d"] < 200]
        n_threat = len(threat_close)
        for m in vis_enemies:
            self.enemy_pos[m.get("id")] = (t, m["pos"][0], m["pos"][1])
            self.enemy_info[m.get("id")] = (t, m["pos"][0], m["pos"][1], list(m.get("weapons") or []),
                                            m.get("hp", 1400) / max(1, m.get("max_hp", 1400)))
        for pk in o.get("pickups", []) or []:
            self.seen_pickups[pk.get("id")] = (t, pk.get("kind"), pk["pos"][0], pk["pos"][1])
        for ev in o.get("events", []) or []:
            if ev.get("type") == "took":
                self.seen_pickups.pop(ev.get("id"), None)
        my_kinds = [w.get("kind") for w in weapons if w]
        has_laser = "laser" in my_kinds
        threats3 = self.threat_list(pos, t)
        want = self.want_weapon(pos, my_kinds, weapons, zc, zr, t)
        hold_fire = False
        nav_dest = None
        recent_threats = [(x, y) for (et, x, y) in self.enemy_pos.values() if t - et < 15]
        if t > self.strafe_switch:
            self.strafe_dir = -self.strafe_dir
            self.strafe_switch = t + random.uniform(1.5, 3.0)

        # ---- loot goal memory: keep a target even when we look away from it
        g = self.goal
        if g is not None:
            gk, gid, gp, gt = g
            seen = (o.get("pickups") or []) + (o.get("crates") or []) + npcs
            ids = set()
            for x in seen:
                ids.add(x.get("id"))
                if x.get("id") == gid:
                    gp[0], gp[1] = x["pos"][0], x["pos"][1]
            gd = dist2(pos, gp)
            gone = gid not in ids and gd < (12 if gk == "pickup" else 40)
            took = any(ev.get("type") == "took" for ev in o.get("events", []) or [])
            if t - gt > 25 or gone or took or dist2(gp, zc) > zr - 10:
                self.goal = g = None
        # accept some damage to finish a worthwhile errand when nobody is close
        goal_lock = (g is not None and hp_frac > 0.5 and zone_urgency < 1.5
                     and all(m["_d"] > 60 for m in vis_enemies))

        def toward(p, strafe=0.0):
            ux, uy = norm2(p[0] - pos[0], p[1] - pos[1])
            return ux - uy * self.strafe_dir * strafe, uy + ux * self.strafe_dir * strafe

        fighting = target is not None and target.get("kind") != "npc"
        evading = (not fighting) and recently_hit and self.last_hit_bearing is not None
        if fighting:
            # combat: torso shoots, legs go somewhere useful
            mode = "fight"
            self.last_combat_t, self.reloc_done = t, False
            d = target["_d"]
            ex, ey = norm2(target["pos"][0] - pos[0], target["pos"][1] - pos[1])
            px, py = -ey * self.strafe_dir, ex * self.strafe_dir
            their_frac = target.get("hp", 1400) / max(1, target.get("max_hp", 1400))
            outmatched = (n_threat >= 2 or (hp_frac < 0.4 and their_frac > hp_frac + 0.1)) and not endgame
            outgunned = "laser" in (target.get("weapons") or []) and not has_laser and d > 60 and not endgame
            if outgunned:
                # a laser out-trades our gun at range: break line of sight, go arm up, come back
                mode = "escape"
                dest = self.pick_position(pos, threats3, zc, zr, nc, nr, t, want=want, key="escape")
                mvx, mvy = toward(dest, 0.2) if dest is not None else (-ex, -ey)
                nav_dest = dest
                speed = 1.0
                boost = energy > 30
                hold_fire = d > 150
            elif goal_lock:
                mode = "loot_fight"
                mvx, mvy = toward(g[2], 0.3)
                nav_dest = g[2]
                speed = 1.0
            elif outmatched:
                mode = "retreat"
                dest = self.pick_position(pos, threats3, zc, zr, nc, nr, t, want=want, key="retreat")
                nav_dest = dest
                if dest is not None:
                    mvx, mvy = toward(dest, 0.3)
                else:
                    mvx, mvy = -ex + px * 0.5, -ey + py * 0.5
                speed = 1.0
                boost = energy > 30 and hp_frac < 0.5
            elif endgame and dist2(pos, nc) > max(nr * 0.6, 6):
                # push into the last circle: strafe while closing in, shooting back
                mode = "push"
                mvx, mvy = toward(nc, 0.6)
                nav_dest = (nc[0], nc[1])
                speed = 0.7
            else:
                back = 0.0
                if d < 35:
                    back = -0.8
                elif d > 140:
                    back = 0.3
                mvx, mvy = px + ex * back, py + ey * back
                speed = 1.0
            if zone_urgency >= 1.0:
                mvx += zone_vec[0] * 1.5 * zone_urgency
                mvy += zone_vec[1] * 1.5 * zone_urgency
        elif evading:
            # hit by something we can't see/shoot: keep the errand, or break off to cover
            self.last_combat_t, self.reloc_done = t, False
            b = self.last_hit_bearing
            ex, ey = math.cos(b), math.sin(b)
            if goal_lock:
                mode = "loot_fight"
                mvx, mvy = toward(g[2], 0.3)
                nav_dest = g[2]
            elif endgame:
                mode = "push"
                mvx, mvy = toward(nc, 0.6)
                nav_dest = (nc[0], nc[1])
            else:
                mode = "evade"
                th = threats3 or [(pos[0] + ex * 80, pos[1] + ey * 80, 1.5)]
                dest = self.pick_position(pos, th, zc, zr, nc, nr, t, want=want, key="evade")
                nav_dest = dest
                if dest is not None:
                    mvx, mvy = toward(dest, 0.4)
                else:
                    mvx, mvy = -ex * 0.8 - ey * self.strafe_dir, -ey * 0.8 + ex * self.strafe_dir
            speed = 1.0
            boost = energy > 40 and hp_frac < 0.6
            if zone_urgency >= 1.0:
                mvx += zone_vec[0] * 1.5 * zone_urgency
                mvy += zone_vec[1] * 1.5 * zone_urgency
        elif t - self.last_combat_t < 12 and not self.reloc_done and zone_urgency < 1.5:
            # after a fight: get away from whoever is left, then heal / loot
            th = list(threats3)
            if not th and self.last_hit_bearing is not None:
                th = [(pos[0] + 80 * math.cos(self.last_hit_bearing), pos[1] + 80 * math.sin(self.last_hit_bearing), 1.5)]
            dest = self.pick_position(pos, th, zc, zr, nc, nr, t, want=want, key="relocate") if th else None
            if dest is not None and dist2(pos, dest) > 4:
                mode = "relocate"
                mvx, mvy = toward(dest)
                nav_dest = dest
                speed = 1.0
            else:
                self.reloc_done = True
        if mode == "idle" and not fighting and not evading and zone_urgency < 1.5:
            prey = self.pick_prey(pos, t, hp_frac, has_laser, my_kinds)
            if prey is not None:
                mode = "hunt"
                others = [th for th in threats3 if dist2(th, prey) > 5]
                dest = self.pick_position(pos, others, zc, zr, nc, nr, t, hunt=prey, key="hunt")
                if dest is not None:
                    mvx, mvy = toward(dest)
                    nav_dest = dest
                    speed = 1.0
        if mode == "idle" and not fighting and not evading:
            # loot / position
            loot = None
            if not vis_enemies or min(m["_d"] for m in vis_enemies) > 140 or goal_lock:
                loot = self.pick_loot(o, pos, weapons, zc, zr, t, hp_frac, npcs)
            if loot is not None:
                if g is None or g[1] != loot[1].get("id"):
                    self.goal = g = (loot[0], loot[1].get("id"), [loot[1]["pos"][0], loot[1]["pos"][1]], t)
            elif g is not None:
                loot = (g[0], {"id": g[1], "pos": [g[2][0], g[2][1], pos[2]]})
            if loot is not None and zone_urgency < 1.5:
                mode = "loot"
                kind, obj = loot
                lx, ly = obj["pos"][0] - pos[0], obj["pos"][1] - pos[1]
                mvx, mvy = norm2(lx, ly)
                d = math.hypot(lx, ly)
                speed = 1.0 if d > 6 else 0.5
                nav_dest = (obj["pos"][0], obj["pos"][1])
                if kind in ("crate", "npc"):
                    # shoot it from mid range, then walk in for the drops
                    op = obj["pos"]
                    c = (op[0], op[1], op[2] + (0.8 if kind == "crate" else 1.75))
                    if d < 70 and self.map.los(muzzle, c):
                        self.shoot_obj = obj
                        self.shoot_kind = kind
                        if d < 30:
                            mvx, mvy = -mvy * self.strafe_dir, mvx * self.strafe_dir
                            speed = 0.6
                if kind == "pickup" and d < 3.8 and abs(obj["pos"][2] - pos[2]) < 4:
                    action["take"] = obj["id"]
                    if obj["id"] in self._swap:
                        action["drop"] = self._swap[obj["id"]]
            elif zone_urgency >= 2.0:
                mode = "zone"
                mvx, mvy = zone_vec
                nav_dest = (zc[0], zc[1])
                speed = 1.0
                boost = energy > 20
            elif nr > 4:
                # take good ground inside the next circle early, away from known enemies
                spot = self.pick_zone_spot(pos, threats3, zc, zr, nc, nr, t)
                mode = "hide"
                if spot is not None and dist2(pos, spot) > 4:
                    mode = "position"
                    mvx, mvy = toward(spot)
                    nav_dest = spot
                    speed = 0.6 if (endgame and threats3) else 1.0
            else:
                mode = "hide"
                ct = self.find_cover(pos, zc, zr, nc, nr, t)
                if ct is not None:
                    lx, ly = ct[0] - pos[0], ct[1] - pos[1]
                    d = math.hypot(lx, ly)
                    if d > 4:
                        mvx, mvy = norm2(lx, ly)
                        nav_dest = ct
                        speed = 1.0
        # repulsion: NPCs, supply drops, close enemies we're not fighting
        rx, ry = 0.0, 0.0
        for n_ in npcs:
            d = dist2(pos, n_["pos"])
            if self.shoot_obj is n_:
                continue
            if d < 70:
                ux, uy = norm2(pos[0] - n_["pos"][0], pos[1] - n_["pos"][1])
                w = (70 - d) / 70 * 1.5
                rx += ux * w
                ry += uy * w
        for dr in o.get("drops", []) or []:
            p = dr.get("pos")
            if not p:
                continue
            d = dist2(pos, p)
            if d < 60 and dr.get("lands_in", 99) < 15:
                ux, uy = norm2(pos[0] - p[0], pos[1] - p[1])
                w = (60 - d) / 60
                rx += ux * w
                ry += uy * w
        if mode in ("hide", "zone", "loot", "relocate", "hunt", "position") and (rx or ry):
            mvx += rx
            mvy += ry
            if speed < 0.38:
                speed = 0.38
        elif mode in ("fight", "retreat", "evade", "loot_fight", "escape", "push"):
            mvx += rx * 0.5
            mvy += ry * 0.5

        if zone_urgency >= 2.0:
            nav_dest = (zc[0], zc[1])  # outside the zone: getting back in comes first
        if nav_dest is not None and (mvx or mvy) and speed > 0:
            nd = self.navigate(pos, nav_dest, t)
            if nd is not None:
                mvx, mvy = nd

        # ---- obstacle steering + stuck handling
        jump = False
        if (mvx or mvy) and speed > 0:
            dx, dy = norm2(mvx, mvy)
            dx, dy = self.steer(pos, dx, dy)
            # stuck detection
            hv = math.hypot(vel[0], vel[1])
            if self.last_pos is not None and hv < 1.0 and grounded:
                self.stuck_t += t - self.prev_t
            else:
                self.stuck_t = max(0.0, self.stuck_t - (t - self.prev_t))
            if self.stuck_t > 0.8:
                self.unstick_until = t + 1.2
                self.unstick_dir = -self.unstick_dir
                self.stuck_t = 0.0
            if t < self.unstick_until:
                a = math.atan2(dy, dx) + self.unstick_dir * math.pi / 2
                dx, dy = math.cos(a), math.sin(a)
                if grounded and energy > 25:
                    jump = True
                if not grounded and energy > 15:
                    boost = True
            action["move"] = [round(dx * speed, 3), round(dy * speed, 3)]
        else:
            self.stuck_t = 0.0
            action["move"] = [0, 0]
        if boost:
            action["boost"] = True
        if jump:
            action["jump"] = True

        # ---- aim + fire
        fire = [False, False]
        aim_cmd = None
        if target is not None:
            aim_cmd, fire = self.combat(target, weapons, muzzle, me, aim, energy)
            if hold_fire:
                fire = [False, False]
        elif self.shoot_obj is not None:
            op = self.shoot_obj["pos"]
            if self.shoot_kind == "crate":
                fake = {"pos": [op[0], op[1], op[2] - 0.95], "vel": [0, 0, 0], "kind": "npc"}
            else:
                fake = self.shoot_obj
            aim_cmd, fire = self.combat(fake, weapons, muzzle, me, aim, energy)
        elif recently_hit and self.last_hit_bearing is not None:
            aim_cmd = [self.last_hit_bearing, 0.0]
        else:
            # look toward recent noises, else at remembered enemies, else scan
            look = None
            for s in o.get("sounds", []) or []:
                k = s.get("kind")
                if k in ("gun", "step", "boost", "laser_charge", "lightning", "jet", "laser", "mortar", "reload"):
                    if s.get("dist") in ("near", "mid"):
                        look = s.get("bearing")
                        self.sound_look = (t, look)
                        break
            if look is None and self.sound_look and t - self.sound_look[0] < 2.5:
                look = self.sound_look[1]
            if look is None and vis_enemies:
                m = vis_enemies[0]
                look = math.atan2(m["pos"][1] - pos[1], m["pos"][0] - pos[0])
            if look is None:
                if self.scan_yaw is None:
                    self.scan_yaw = aim[0]
                # sweep; face away from the wall we hide by when possible
                self.scan_yaw = wrap(self.scan_yaw + 0.12)
                look = self.scan_yaw
            aim_cmd = [look, 0.0]
            # keep gun loaded during quiet time
            for i, w in enumerate(weapons):
                if w and w.get("kind") == "gun" and w.get("ammo", 12) < 6 and w.get("reload", 0) == 0:
                    if not vis_enemies:
                        action["reload"] = True
            # release a charging laser if no target
        action["aim"] = [round(aim_cmd[0], 4), round(max(self.min_pitch, min(self.max_pitch, aim_cmd[1])), 4)]
        action["fire"] = fire

        self.last_pos = pos
        self.prev_t = t
        return action

    # ------------------------------------------------------------------
    def zone_now(self, zone, t):
        c = zone.get("center", [0, 0]) or [0, 0]
        r = float(zone.get("radius", 500) or 0)
        nc = zone.get("next_center", c) or c
        nr = float(zone.get("next_radius", r) or 0)
        s0 = zone.get("shrink_start")
        s1 = zone.get("shrink_end")
        if s0 is None or s1 is None or t <= s0:
            return c, r
        if t >= s1:
            return nc, nr
        f = (t - s0) / max(1e-6, (s1 - s0))
        # look a bit ahead so we keep a margin
        f = min(1.0, f + 4.0 / max(1e-6, (s1 - s0)))
        return [c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f], r + (nr - r) * f

    def steer(self, pos, dx, dy):
        m = self.map
        if not m.ok:
            return dx, dy
        base = math.atan2(dy, dx)
        best = None
        best_s = -1e9
        for k in range(16):
            off = (k // 2) * (math.pi / 8) * (1 if k % 2 == 0 else -1)
            if abs(off) > math.pi:
                continue
            a = base + off
            cx, cy = math.cos(a), math.sin(a)
            pen = 0.0
            for dist, w in ((3.5, 3.0), (6.0, 1.5), (10.0, 0.6)):
                if m.blocked(pos[0] + cx * dist, pos[1] + cy * dist, pos[2]):
                    pen += w
            s = math.cos(off) - pen
            if s > best_s:
                best_s, best = s, (cx, cy)
        return best if best else (dx, dy)

    def navigate(self, pos, dest, t):
        """Direction toward dest around buildings, or None when the straight line is walkable."""
        m = self.map
        if dest is None or not m.ok or m.path_clear(pos, dest):
            self._path = None
            return None
        if (self._path is None or self._path_goal is None or dist2(self._path_goal, dest) > 10
                or t - self._path_t > 3.0):
            self._path = m.find_path(pos, dest) or []
            self._path_goal, self._path_t = (dest[0], dest[1]), t
        path = self._path
        while path and dist2(pos, path[0]) < 4.0:
            path.pop(0)
        if not path:
            return None
        wp = path[0]
        for q in path[1:12]:
            if m.path_clear(pos, q):
                wp = q
            else:
                break
        return norm2(wp[0] - pos[0], wp[1] - pos[1])

    DANGER = {"laser": 2.0, "mortar": 1.5, "lightning": 1.2}

    def threat_list(self, pos, t):
        """Up to 3 known enemies (seen in the last 15 s) as (x, y, danger), most dangerous first."""
        out = []
        for i, (et, x, y, wl, hpf) in self.enemy_info.items():
            if t - et > 15:
                continue
            dg = max([1.0] + [self.DANGER.get(k, 1.0) for k in wl]) * (0.6 + 0.4 * hpf)
            d = dist2(pos, (x, y))
            out.append((dg * 200.0 / max(40.0, d), (x, y, dg)))
        out.sort(key=lambda v: -v[0])
        return [v[1] for v in out[:5]]

    def want_weapon(self, pos, my_kinds, weapons, zc, zr, t):
        """Nearest remembered weapon worth having (empty slot), or None."""
        full = not any(w is None for w in weapons[:2]) and len([w for w in weapons if w]) >= 2
        specials = [w.get("kind") for w in weapons[:2] if w and w.get("kind") != "gun"]
        cur_val = min([self.weapon_value(k, t) for k in specials], default=0.0)
        best, bs = None, -1e9
        for i, (pt, k, x, y) in list(self.seen_pickups.items()):
            if t - pt > 90:
                del self.seen_pickups[i]
                continue
            v = self.weapon_value(k, t)
            if k in my_kinds or v <= 0 or dist2((x, y), zc) > zr - 15:
                continue
            if full and v < cur_val + 0.8:
                continue
            d = dist2(pos, (x, y))
            if d > 180:
                continue
            sc = (v - (cur_val if full else 0.0)) * 30 - d * 0.5
            if sc > bs:
                bs, best = sc, (x, y)
        return best

    def pick_prey(self, pos, t, hp_frac, has_laser, my_kinds):
        """An isolated, weaker enemy worth picking off, or None."""
        if hp_frac < 0.75:
            return None
        info = [(i, v) for i, v in self.enemy_info.items() if t - v[0] < 8]
        best, bd = None, 1e9
        for i, (et, x, y, wl, hpf) in info:
            d = dist2(pos, (x, y))
            if d > 220:
                continue
            if any(j != i and dist2((x, y), (v[1], v[2])) < 120 for j, v in info):
                continue  # not alone: a third party would join
            weak = (wl == ["gun"] or not wl) or hpf < 0.5
            if "laser" in wl and not has_laser:
                weak = weak and hpf < 0.35
            if not weak or hpf > hp_frac + 0.1:
                continue
            if d < bd:
                bd, best = d, (x, y)
        return best

    def pick_position(self, pos, threats, zc, zr, nc, nr, t, want=None, hunt=None, key=""):
        """Score nearby spots: out of known enemies' sight (lasers count double), far from them,
        enclosed rather than open, reachable without crossing their sightlines; optionally
        toward a weapon we want or to ~70 m from a prey. Cached 1 s per purpose."""
        if self._pp[1] == key and t - self._pp[0] < 1.0:
            return self._pp[2]
        m = self.map
        if not m.ok:
            return None
        d_nc = dist2(pos, nc)
        best, bs = None, -1e18
        cands = [(pos[0], pos[1], 0.0)]
        radii = (25.0, 50.0, 80.0, 120.0) if (key in ("escape", "retreat", "evade") and len(threats) >= 2) else (25.0, 50.0, 80.0)
        for r in radii:
            for k in range(12):
                a = k * math.pi / 6 + (0.26 if r in (50.0, 120.0) else 0.0)
                cands.append((pos[0] + r * math.cos(a), pos[1] + r * math.sin(a), r))
        prev = self._pp_prev.get(key)
        if prev is not None and t - prev[0] < 4.0:
            cands.append((prev[1][0], prev[1][1], -1.0))
        for (x, y, r) in cands:
            stick = 12.0 if r < 0 else 0.0
            r = dist2(pos, (x, y)) if r < 0 else r
            if r > 0 and m.blocked(x, y):
                continue
            if dist2((x, y), zc) > zr - 15 or (dist2((x, y), nc) > max(nr, 10) and dist2((x, y), nc) > d_nc + 5):
                continue
            gz = m.ground(x, y)
            sc = stick - 0.25 * r - 40.0 * m.openness(x, y)
            mx, my = (pos[0] + x) * 0.5, (pos[1] + y) * 0.5
            for ti, (tx, ty, dg) in enumerate(threats):
                d = dist2((x, y), (tx, ty))
                sc += min(d, 250.0) * 0.15 * dg
                if d < 230 and ti < 3:
                    eye_t = (tx, ty, m.ground(tx, ty) + 8.0)
                    if m.los(eye_t, (x, y, gz + 4.5), 4.0):
                        sc -= 60.0 * dg * (1.5 if d < 100 else 1.0)
                    if r >= 50 and m.los(eye_t, (mx, my, m.ground(mx, my) + 4.5), 4.0):
                        sc -= 20.0 * dg
            if want is not None:
                sc -= 0.5 * dist2((x, y), want)
            if hunt is not None:
                sc -= 0.6 * abs(dist2((x, y), hunt) - 70.0)
            if sc > bs:
                bs, best = sc, (x, y)
        self._pp = (t, key, best)
        if best is not None:
            self._pp_prev[key] = (t, best)
        return best

    def pick_zone_spot(self, pos, threats, zc, zr, nc, nr, t):
        """Where to wait for the next circle: inside it (and the current one), enclosed, near a
        wall, out of known enemies' sight and far from them, not too far to walk. Cached 2 s,
        and the previous spot is kept unless something is clearly better."""
        if t - self._zs[0] < 2.0 and self._zs[1] is not None and dist2(self._zs[1], zc) < zr - 12:
            return self._zs[1]
        m = self.map
        if not m.ok:
            return None
        R = max(nr, 8.0)
        cands = []
        if dist2(pos, nc) < R * 0.85:
            cands.append((pos[0], pos[1], 8.0))
        for rf in (0.3, 0.55, 0.8):
            for k in range(16):
                a = k * math.pi / 8 + rf
                cands.append((nc[0] + R * rf * math.cos(a), nc[1] + R * rf * math.sin(a), 0.0))
        if self._zs[1] is not None:
            cands.append((self._zs[1][0], self._zs[1][1], 12.0))
        best, bs = None, -1e18
        for (x, y, bonus) in cands:
            if m.blocked(x, y) or dist2((x, y), zc) > zr - 15 or dist2((x, y), nc) > R * 0.9:
                continue
            gz = m.ground(x, y)
            sc = bonus - 40.0 * m.openness(x, y) - 0.12 * dist2(pos, (x, y))
            if m.near_wall(x, y, 6.0):
                sc += 15.0
            for ti, (tx, ty, dg) in enumerate(threats):
                d = dist2((x, y), (tx, ty))
                sc += min(d, 250.0) * 0.15 * dg
                if d < 230 and ti < 3 and m.los((tx, ty, m.ground(tx, ty) + 8.0), (x, y, gz + 4.5), 4.0):
                    sc -= 60.0 * dg * (1.5 if d < 100 else 1.0)
            if sc > bs:
                bs, best = sc, (x, y)
        self._zs = (t, best)
        return best

    BEATS = {"laser": "mortar", "mortar": "lightning", "lightning": "laser"}

    def weapon_value(self, k, t):
        """Base worth of a weapon, adjusted by what the enemies we've seen carry (triangle)."""
        base = {"laser": 3.0, "mortar": 2.5, "lightning": 2.0}.get(k, 0.0)
        if base <= 0:
            return 0.0
        lists = [v[3] for v in self.enemy_info.values() if t - v[0] < 120]
        if not lists:
            return base
        n = float(len(lists))
        good = sum(1 for wl in lists if self.BEATS[k] in wl) / n
        bad = sum(1 for wl in lists if any(self.BEATS.get(x) == k for x in wl)) / n
        return base + 1.5 * good - 1.5 * bad

    def pick_relocate(self, pos, threats, zc, zr, t):
        """Cover-ish spot 35-80 m away, far from known threats and out of their sight. Cached 1.5 s."""
        if self._rel is not None and t - self._rel_t < 1.5:
            return self._rel
        m = self.map
        if not m.ok:
            return None
        near_t = min(threats, key=lambda p: dist2(pos, p)) if threats else None
        best, bs = None, -1e9
        for r in (35, 55, 80):
            for k in range(12):
                a = k * math.pi / 6
                x = pos[0] + r * math.cos(a)
                y = pos[1] + r * math.sin(a)
                if m.blocked(x, y) or dist2((x, y), zc) > zr - 18:
                    continue
                dmin = min(dist2((x, y), p) for p in threats) if threats else 100.0
                s = dmin - 0.3 * r
                if m.near_wall(x, y, 6.0):
                    s += 25
                if near_t is not None:
                    gz = m.ground(x, y)
                    if not m.los((near_t[0], near_t[1], m.ground(near_t[0], near_t[1]) + 8.0), (x, y, gz + 4.5), 3.0):
                        s += 35
                if s > bs:
                    bs, best = s, (x, y)
        self._rel, self._rel_t = best, t
        return best

    def find_cover(self, pos, zc, zr, nc, nr, t):
        """Find a spot next to a building, inside the safe area. Cached ~2 s."""
        d_nc = dist2(pos, nc)

        def ok(p):
            # inside the current zone, and not drifting away from the next circle
            return dist2(p, zc) < zr - 18 and (dist2(p, nc) < max(nr, 10) or dist2(p, nc) <= d_nc + 5)

        if self.cover_target is not None and t - self.cover_t < 2.0:
            ct = self.cover_target
            if ok(ct):
                return ct
        m = self.map
        if not m.ok:
            return None
        if m.near_wall(pos[0], pos[1], 7.0) and ok(pos):
            self.cover_target = (pos[0], pos[1])
            self.cover_t = t
            return self.cover_target
        best = None
        bd = 1e9
        for r in (10, 20, 30, 45, 60, 80):
            for k in range(16):
                a = k * math.pi / 8
                x = pos[0] + r * math.cos(a)
                y = pos[1] + r * math.sin(a)
                if m.blocked(x, y):
                    continue
                if not ok((x, y)):
                    continue
                if m.near_wall(x, y, 6.0):
                    if r < bd:
                        bd, best = r, (x, y)
            if best is not None:
                break
        self.cover_target = best
        self.cover_t = t
        return best

    def pick_loot(self, o, pos, weapons, zc, zr, t=999.0, hp_frac=1.0, npcs=()):
        """Power up so we are not the weakest mech (house bots hunt the weakest first)."""
        kinds = [w.get("kind") for w in weapons if w]
        empty = any(w is None for w in weapons[:2]) if len(weapons) >= 2 else len(weapons) < 2
        best = None
        bscore = -1e9
        early = t < 150
        self._swap = {}
        specials = [(i, w.get("kind")) for i, w in enumerate(weapons[:2]) if w and w.get("kind") != "gun"]
        cur_val = min([self.weapon_value(k, t) for _, k in specials], default=0.0)
        drop_slot = min(specials, key=lambda v: self.weapon_value(v[1], t))[0] if specials else None
        if True:
            for p in o.get("pickups", []) or []:
                k = p.get("kind")
                v = self.weapon_value(k, t)
                if k in kinds or v <= 0:
                    continue
                if not empty:
                    if drop_slot is None or v < cur_val + 0.8:
                        continue  # not clearly better than what we'd drop
                    self._swap[p.get("id")] = drop_slot
                d = dist2(pos, p["pos"])
                if d > (150 if early else 80) or dist2(p["pos"], zc) > zr - 15:
                    continue
                if abs(p["pos"][2] - pos[2]) > 12:
                    continue
                s = (v if empty else v - cur_val) * 30 - d * 0.5  # a weapon beats a crate
                if s > bscore:
                    bscore, best = s, ("pickup", p)
        for c in o.get("cubes", []) or []:
            d = dist2(pos, c["pos"])
            if d > 45 or abs(c["pos"][2] - pos[2]) > 10:
                continue
            if dist2(c["pos"], zc) > zr - 15:
                continue
            s = 30 - d
            if s > bscore:
                bscore, best = s, ("cube", c)
        if hp_frac > 0.6:
            # crates: 1 cube + maybe a weapon
            for c in o.get("crates", []) or []:
                d = dist2(pos, c["pos"])
                if d > 90 or abs(c["pos"][2] - pos[2]) > 4 or dist2(c["pos"], zc) > zr - 15:
                    continue
                s = 25 - d * 0.5
                if s > bscore:
                    bscore, best = s, ("crate", c)
            # NPC drones: 2 cubes each, weak
            if early:
                for n_ in npcs:
                    d = dist2(pos, n_["pos"])
                    if d > 90 or dist2(n_["pos"], zc) > zr - 15:
                        continue
                    s = 22 - d * 0.5
                    if s > bscore:
                        bscore, best = s, ("npc", n_)
        return best

    def pick_upgrade(self, choices):
        order = ["armor", "hp", "health", "regen", "repair", "sensor", "speed", "move", "rate", "damage"]
        best_i, best_r = 0, 1e9
        for i, c in enumerate(choices):
            txt = (str(c.get("id", "")) + " " + str(c.get("name", "")) + " " + str(c.get("desc", ""))).lower()
            for r, kw in enumerate(order):
                if kw in txt and r < best_r:
                    best_r, best_i = r, i
        return best_i

    def combat(self, target, weapons, muzzle, me, aim, energy):
        tp = target.get("pos", [0, 0, 0])
        tv = target.get("vel", [0, 0, 0])
        s = 1.0 + 0.05 * (target.get("cubes", 0) or 0)
        h = 4.5 * s if target.get("kind") != "npc" else 1.75
        T = (tp[0], tp[1], tp[2] + h)
        rx, ry, rz = T[0] - muzzle[0], T[1] - muzzle[1], T[2] - muzzle[2]
        dist = math.sqrt(rx * rx + ry * ry + rz * rz)
        hd = math.hypot(rx, ry)
        direct_yaw = math.atan2(ry, rx)
        direct_pitch = math.atan2(rz, max(1e-6, hd))

        slots = {}
        for i, w in enumerate(weapons[:2]):
            if w:
                slots[w.get("kind")] = (i, w)

        # gun lead
        gun_speed = 252.0
        if "gun" in slots:
            gun_speed = float((slots["gun"][1].get("stats") or {}).get("speed", 252.0))
        tt = dist / gun_speed
        for _ in range(3):
            px, py, pz = rx + tv[0] * tt, ry + tv[1] * tt, rz + tv[2] * tt * 0.3
            tt = math.sqrt(px * px + py * py + pz * pz) / gun_speed
        lead_yaw = math.atan2(py, px)
        lead_pitch = math.atan2(pz, max(1e-6, math.hypot(px, py)))

        aim_cmd = [lead_yaw, lead_pitch]
        fire = [False, False]
        yaw_err_lead = abs(wrap(aim[0] - lead_yaw))
        tol = math.atan2(2.2 * s, max(1.0, dist)) + 0.01

        # mortar at long range if we have it and no gun line of sight preference
        use_mortar = "mortar" in slots and 30 < hd < 225
        if use_mortar and not ("laser" in slots and hd < 260) and ("gun" not in slots or hd > 140):
            i, w = slots["mortar"]
            v = float((w.get("stats") or {}).get("speed", 72.0))
            g = self.mortar_g
            # lead using approx flight time
            pitch = self.ballistic(v, g, hd, rz)
            if pitch is not None:
                ft = hd / max(1e-3, v * math.cos(pitch))
                lx, ly = rx + tv[0] * ft, ry + tv[1] * ft
                hd2 = math.hypot(lx, ly)
                p2 = self.ballistic(v, g, hd2, rz)
                if p2 is not None:
                    pitch = p2
                    yaw = math.atan2(ly, lx)
                    aim_cmd = [yaw, pitch]
                    if abs(wrap(aim[0] - yaw)) < 0.04 and abs(aim[1] - pitch) < 0.04 and w.get("cooldown", 0) <= 0.0:
                        fire[i] = True
                    return aim_cmd, fire

        for kind, (i, w) in slots.items():
            if kind == "gun":
                rng = float((w.get("stats") or {}).get("range", 230))
                if dist < rng and w.get("ammo", 1) > 0 and w.get("reload", 0) == 0 and yaw_err_lead < tol * 2.5:
                    fire[i] = True
            elif kind == "lightning":
                rng = float((w.get("stats") or {}).get("range", 45))
                if hd < rng - 2 and abs(wrap(aim[0] - direct_yaw)) < math.radians(12) and energy >= 8:
                    fire[i] = True
            elif kind == "laser":
                rng = float((w.get("stats") or {}).get("range", 280))
                charge = float(w.get("charge", 0) or 0)
                full = float((w.get("stats") or {}).get("charge", 0.75))
                err = abs(wrap(aim[0] - direct_yaw))
                if dist < rng:
                    if charge <= 0 and w.get("cooldown", 0) <= 0:
                        fire[i] = True  # start charging
                    elif 0 < charge < full * 0.9:
                        fire[i] = True  # keep charging
                    elif charge >= full * 0.9:
                        # release when on target
                        fire[i] = not (err < tol * 1.2)
        return aim_cmd, fire

    @staticmethod
    def ballistic(v, g, R, h):
        v2 = v * v
        disc = v2 * v2 - g * (g * R * R + 2 * h * v2)
        if disc < 0 or R < 1e-3:
            return None
        return math.atan((v2 - math.sqrt(disc)) / (g * R))


# ---------------------------------------------------------------------------
# I/O loop: answer only the newest observation (portable: Linux + Windows)
# ---------------------------------------------------------------------------
import queue
import threading

_out = sys.stdout.buffer


def send(obj):
    _out.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    _out.flush()


def _reader(q):
    inp = sys.stdin.buffer
    while True:
        ln = inp.readline()
        if not ln:
            q.put(None)
            return
        q.put(ln)


def main():
    q = queue.Queue()
    th = threading.Thread(target=_reader, args=(q,), daemon=True)
    th.start()
    bot = None
    while True:
        first = q.get()
        batch = [first]
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest_obs = None
        for ln in batch:
            if ln is None:
                return
            ln = ln.strip()
            if not ln:
                continue
            try:
                msg = json.loads(ln)
            except Exception:
                continue
            typ = msg.get("type")
            if typ == "hello":
                t0 = time.time()
                try:
                    bot = Bot(msg)
                except Exception as e:
                    log("init failed", repr(e))
                    bot = None
                send({"type": "ready", "name": "soonkwon-survivor"})
                log("ready in %.2fs" % (time.time() - t0))
            elif typ == "obs":
                latest_obs = msg
            elif typ == "end":
                log("end", msg)
                return
        if latest_obs is not None:
            try:
                if bot is None:
                    bot = Bot({"config": {}})
                a = bot.act(latest_obs)
            except Exception as e:
                log("act error", repr(e))
                a = {"tick": latest_obs.get("tick"), "move": [0, 0], "fire": [False, False]}
            send(a)


if __name__ == "__main__":
    main()
