"""Our own mech, parsed from `obs.self` into convenient fields."""
import math


def _snap(v, eps=1e-3):
    v = float(v or 0.0)
    return 0.0 if v < eps else v


class Weapon:
    __slots__ = ("slot", "kind", "cooldown", "ammo", "reload", "charge", "stats")

    def __init__(self, slot, w):
        self.slot = slot
        self.kind = w.get("kind")
        # timers can come back as float residue (6.9e-17) instead of 0: snap them
        self.cooldown = _snap(w.get("cooldown", 0.0))
        self.ammo = w.get("ammo")
        self.reload = _snap(w.get("reload", 0.0))
        self.charge = _snap(w.get("charge", 0.0))
        self.stats = w.get("stats") or {}

    def stat(self, key, default):
        v = self.stats.get(key)
        return default if v is None else float(v)


class Me:
    def __init__(self, obs, cfg):
        s = obs["self"]
        self.t = float(obs.get("t", 0.0))
        self.tick = obs.get("tick")
        self.id = s.get("id")
        self.pos = [float(v) for v in s["pos"]]
        self.vel = [float(v) for v in s.get("vel", (0, 0, 0))]
        a = s.get("aim") or (0.0, 0.0)
        self.yaw, self.pitch = float(a[0]), float(a[1])
        self.hp = float(s.get("hp", 0.0))
        self.max_hp = float(s.get("max_hp", 1.0)) or 1.0
        self.energy = float(s.get("energy", 0.0))
        self.max_energy = float(s.get("max_energy", 100.0)) or 100.0
        self.grounded = bool(s.get("grounded", True))
        self.cubes = int(s.get("cubes", 0) or 0)
        self.upgrades = list(s.get("upgrades") or [])
        self.in_zone = bool(s.get("in_zone", True))
        st = s.get("stats") or {}
        mech = cfg.get("mech", {})

        def g(k, d):
            v = st.get(k, mech.get(k, d))
            return d if v is None else float(v)

        self.stats = st
        self.radius = g("radius", 2.5)
        self.height = g("height", 9.0)
        self.eye_h = g("eye_height", 8.0)
        self.muzzle_h = g("muzzle_height", 7.0)
        self.walk = g("walk_speed", 11.7)
        self.boost_speed = g("boost_speed", 19.5)
        self.turn_rate = g("turn_rate", 4.0)
        self.pitch_rate = g("pitch_rate", 3.0)
        self.min_pitch = g("min_pitch", -0.7)
        self.max_pitch = g("max_pitch", 1.3)
        self.armor = g("armor", 0.0)
        self.regen_delay = g("regen_delay", 5.0)
        self.regen = g("regen_per_s", 45.0)
        self.weapons = []
        self.by_kind = {}
        for i, w in enumerate(s.get("weapons") or []):
            if w:
                wp = Weapon(i, w)
                self.weapons.append(wp)
                self.by_kind[wp.kind] = wp
        self.slots_free = [i for i, w in enumerate(s.get("weapons") or [None, None]) if not w]

    @property
    def xy(self):
        return (self.pos[0], self.pos[1])

    @property
    def eye(self):
        return (self.pos[0], self.pos[1], self.pos[2] + self.eye_h)

    @property
    def muzzle(self):
        return (self.pos[0], self.pos[1], self.pos[2] + self.muzzle_h)

    @property
    def hp_frac(self):
        return self.hp / self.max_hp

    def has(self, kind):
        return kind in self.by_kind

    def speed(self):
        return math.hypot(self.vel[0], self.vel[1])
