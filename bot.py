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
            self.ok = True
            log("map loaded", self.gnx, self.gny, "block", sum(blk),
                "bldg", sum(1 for v in self.top if v > -1e8))
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
        self.zone_walking = False

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
        outside = d_center > zr - 3
        # safe spot: inside the next circle, toward us
        if nr > 15:
            dx, dy = norm2(pos[0] - nc[0], pos[1] - nc[1])
            safe_r = nr * 0.7
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

        # zone pressure
        silent_speed = self.step_speed * 0.92  # m/s, below the step-sound threshold
        silent_frac = min(1.0, silent_speed / max(0.1, walk_speed))
        zone_vec = (0.0, 0.0)
        zone_urgency = 0.0
        if outside:
            zone_vec = norm2(zc[0] - pos[0], zc[1] - pos[1])
            zone_urgency = 2.0
        elif not in_next:
            zone_vec = norm2(safe[0] - pos[0], safe[1] - pos[1])
            dist_need = dist2(pos, safe)
            deadline = (shrink_start + shrink_end) * 0.5 if t < shrink_start else shrink_end
            avail = max(1.0, deadline - t)
            need_speed = dist_need / avail
            if need_speed > walk_speed * 0.6:
                zone_urgency = 1.5
            elif need_speed > silent_speed * 0.85:
                zone_urgency = 1.0
            elif need_speed > 2.7 or (self.zone_walking and need_speed > 1.8):
                zone_urgency = 0.4  # walk silently
            # else: plenty of time -> stay in cover (urgency 0)
        self.zone_walking = 0 < zone_urgency
        # edge proximity of the current circle (moving circle)
        if not outside and d_center > zr - 25 and zone_urgency < 1.0:
            zone_vec = norm2(zc[0] - pos[0], zc[1] - pos[1])
            zone_urgency = 1.0

        threat_close = [m for m in vis_enemies if m["_d"] < 200]
        n_threat = len(threat_close)

        # placement matters, not kills: only take fights we can't avoid
        avoid_target = None
        if target is not None and target.get("kind") != "npc":
            d = target["_d"]
            is_att = recently_hit and target.get("id") == self.last_hit_by
            engage = d < 60 or (is_att and d < 100 and hp_frac > 0.45 and n_threat <= 1)
            if not engage:
                avoid_target = target
                target = None

        if avoid_target is not None and zone_urgency < 1.5:
            # break line of sight: move behind a building, then sit still
            mode = "hide_from"
            threats = [m for m in vis_enemies if m["_d"] < 200][:2]
            seen = any(m["_los"] for m in threats)
            if seen or recently_hit:
                hp_ = self.find_hide(pos, [m["pos"] for m in threats], zc, zr, t)
                if hp_ is not None:
                    lx, ly = hp_[0] - pos[0], hp_[1] - pos[1]
                    if math.hypot(lx, ly) > 2.5:
                        mvx, mvy = norm2(lx, ly)
                        speed = 1.0
                        boost = recently_hit and energy > 35
                else:
                    ex, ey = norm2(avoid_target["pos"][0] - pos[0], avoid_target["pos"][1] - pos[1])
                    mvx, mvy = -ex - ey * self.strafe_dir, -ey + ex * self.strafe_dir
                    speed = 1.0
            if zone_urgency >= 1.0:
                mvx += zone_vec[0] * 1.5 * zone_urgency
                mvy += zone_vec[1] * 1.5 * zone_urgency
                speed = max(speed, 1.0)
        elif target is not None and target.get("kind") != "npc":
            # combat movement
            mode = "fight"
            d = target["_d"]
            ex, ey = norm2(target["pos"][0] - pos[0], target["pos"][1] - pos[1])
            px, py = -ey * self.strafe_dir, ex * self.strafe_dir
            if t > self.strafe_switch:
                self.strafe_dir = -self.strafe_dir
                self.strafe_switch = t + random.uniform(0.8, 2.0)
            their_frac = target.get("hp", 1400) / max(1, target.get("max_hp", 1400))
            outmatched = n_threat >= 2 or (hp_frac < 0.4 and their_frac > hp_frac + 0.1)
            if outmatched:
                mode = "retreat"
                mvx, mvy = -ex * 1.0 + px * 0.5, -ey * 1.0 + py * 0.5
                speed = 1.0
                boost = energy > 30 and hp_frac < 0.5
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
        elif recently_hit and self.last_hit_bearing is not None:
            # hit by something we can't see/shoot: get out of its line
            mode = "evade"
            b = self.last_hit_bearing
            ex, ey = math.cos(b), math.sin(b)
            px, py = -ey * self.strafe_dir, ex * self.strafe_dir
            mvx, mvy = -ex * 0.8 + px, -ey * 0.8 + py
            speed = 1.0
            boost = energy > 40 and hp_frac < 0.6
            if zone_urgency >= 1.0:
                mvx += zone_vec[0] * 1.5 * zone_urgency
                mvy += zone_vec[1] * 1.5 * zone_urgency
        else:
            # loot / position
            loot = None
            if not vis_enemies or min(m["_d"] for m in vis_enemies) > 140:
                loot = self.pick_loot(o, pos, weapons, zc, zr)
            if loot is not None and zone_urgency < 1.5:
                mode = "loot"
                kind, obj = loot
                lx, ly = obj["pos"][0] - pos[0], obj["pos"][1] - pos[1]
                mvx, mvy = norm2(lx, ly)
                d = math.hypot(lx, ly)
                speed = 1.0 if d > 6 else 0.5
                if kind == "pickup" and d < 3.8 and abs(obj["pos"][2] - pos[2]) < 4:
                    action["take"] = obj["id"]
            elif zone_urgency > 0:
                mode = "zone"
                mvx, mvy = zone_vec
                if zone_urgency >= 2.0:
                    speed, boost = 1.0, energy > 20
                elif zone_urgency >= 1.5:
                    speed = 1.0
                elif zone_urgency >= 1.0:
                    speed = 1.0
                else:
                    speed = silent_frac  # silent walk
            else:
                mode = "hide"
                ct = self.find_cover(pos, zc, zr, nc, nr, t)
                if ct is not None:
                    lx, ly = ct[0] - pos[0], ct[1] - pos[1]
                    d = math.hypot(lx, ly)
                    if d > 4:
                        mvx, mvy = norm2(lx, ly)
                        speed = silent_frac
        # repulsion: NPCs, supply drops, close enemies we're not fighting
        rx, ry = 0.0, 0.0
        for n_ in npcs:
            d = dist2(pos, n_["pos"])
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
        if mode in ("hide", "zone", "loot") and (rx or ry):
            mvx += rx
            mvy += ry
            if speed < silent_frac:
                speed = silent_frac
        elif mode in ("fight", "retreat", "evade", "hide_from"):
            mvx += rx * 0.5
            mvy += ry * 0.5

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
        elif avoid_target is not None:
            # face it (front armor) without firing / making noise
            ap = avoid_target["pos"]
            aim_cmd = [math.atan2(ap[1] - pos[1], ap[0] - pos[0]), 0.0]
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
        f = min(1.0, f + 3.0 / max(1e-6, (s1 - s0)))
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

    def find_hide(self, pos, threat_pos, zc, zr, t):
        """Nearest walkable point that no threat can see. Cached 0.5 s."""
        if getattr(self, "_hide_t", -99) > t - 0.5 and self._hide is not None:
            return self._hide
        m = self.map
        self._hide_t = t
        self._hide = None
        if not m.ok or not threat_pos:
            return None
        best, bs = None, 1e9
        for r in (8, 15, 24, 36):
            for k in range(12):
                a = k * math.pi / 6
                x = pos[0] + r * math.cos(a)
                y = pos[1] + r * math.sin(a)
                if m.blocked(x, y) or dist2((x, y), zc) > zr - 10:
                    continue
                gz = m.ground(x, y) + 4.5
                nvis = 0
                closer = 0.0
                for tp in threat_pos:
                    if m.los((tp[0], tp[1], tp[2] + 8.0), (x, y, gz), 3.0):
                        nvis += 1
                    if dist2((x, y), tp) < dist2(pos, tp) - 5:
                        closer += 1
                s = nvis * 100 + closer * 20 + r
                if s < bs:
                    bs, best = s, (x, y)
            if best is not None and bs < 100:
                break
        if best is not None and bs >= 100 * len(threat_pos):
            best = None  # nowhere hides us from anyone
        self._hide = best
        return best

    def find_cover(self, pos, zc, zr, nc, nr, t):
        """Find a spot next to a building, inside the safe area. Cached ~2 s."""
        d_nc = dist2(pos, nc)

        def ok(p):
            # inside the current zone, and in the next circle or not farther from it
            return dist2(p, zc) < zr - 20 and (dist2(p, nc) < max(nr * 0.85, 10) or dist2(p, nc) <= d_nc + 5)

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

    def pick_loot(self, o, pos, weapons, zc, zr):
        kinds = [w.get("kind") for w in weapons if w]
        empty = any(w is None for w in weapons[:2]) if len(weapons) >= 2 else len(weapons) < 2
        best = None
        bscore = -1e9
        pref = {"laser": 3.0, "mortar": 2.5, "lightning": 2.0, "gun": 0.0}
        if empty:
            for p in o.get("pickups", []) or []:
                k = p.get("kind")
                if k in kinds or pref.get(k, 0) <= 0:
                    continue
                d = dist2(pos, p["pos"])
                if d > 70 or dist2(p["pos"], zc) > zr - 15:
                    continue
                if abs(p["pos"][2] - pos[2]) > 12:
                    continue
                s = pref[k] * 20 - d
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
