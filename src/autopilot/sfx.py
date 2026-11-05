"""Synthesized sound-effects library (no downloads, no licensing questions).

Every effect is generated with numpy from a seed, so each video gets its own *variant* of a
whoosh, pop or impact (different length, sweep, pitch and texture) while keeping the same
family sound. `pack(seed)` picks the variants for one video; render.py places them on the
timeline: whooshes on transitions, a soft impact under the opening, pops on number reveals,
a swoosh + ticks when an info card cascades in, and a riser → impact → shimmer on the end card.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000


def _noise(n: int, rng: np.random.Generator, colour: str) -> np.ndarray:
    white = rng.standard_normal(n)
    if colour == "white":
        return white
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = f[1]
    spec /= np.sqrt(f) if colour == "pink" else f        # pink 1/f, brown 1/f²
    out = np.fft.irfft(spec, n)
    return out / (np.abs(out).max() + 1e-9)


def _sweep_filter(x: np.ndarray, f0: float, f1: float, q: float = 1.2) -> np.ndarray:
    """Time-varying band-pass (state-variable filter) sweeping f0 → f1 (log)."""
    n = len(x)
    fc = np.exp(np.linspace(np.log(f0), np.log(f1), n))
    out = np.empty(n)
    low = band = 0.0
    damp = 1 / q
    fcoef = 2 * np.sin(np.pi * fc / SR)
    for i in range(n):
        f = fcoef[i]
        high = x[i] - low - damp * band
        band += f * high
        low += f * band
        out[i] = band
    return out


def _env(n: int, attack: float, release_curve: float = 2.0, peak: float = 0.6) -> np.ndarray:
    t = np.linspace(0, 1, n)
    rise = np.clip(t / max(peak, 1e-3), 0, 1) ** attack
    fall = np.clip((1 - t) / max(1 - peak, 1e-3), 0, 1) ** release_curve
    return np.minimum(rise, fall)


def _norm(x: np.ndarray, peak: float = 0.9) -> np.ndarray:
    return x / (np.abs(x).max() + 1e-9) * peak


# ── generators ───────────────────────────────────────────────
def whoosh(rng: np.random.Generator) -> np.ndarray:
    dur = rng.uniform(0.45, 0.8)
    n = int(dur * SR)
    up = rng.random() < 0.6
    f0, f1 = (rng.uniform(250, 500), rng.uniform(2500, 5500)) if up else (rng.uniform(3000, 5000), rng.uniform(300, 600))
    q = rng.uniform(1.6, 2.6)
    x = _sweep_filter(_noise(n, rng, rng.choice(["white", "pink"])), f0, f1, q)
    x = _sweep_filter(_norm(x), f0, f1, q)        # second pass: 24 dB/oct, a clearly moving "air" band
    x *= _env(n, rng.uniform(1.5, 3.0), rng.uniform(1.5, 3.0), rng.uniform(0.5, 0.7))
    if rng.random() < 0.5:   # airy "flutter" on some variants
        x *= 1 + 0.25 * np.sin(2 * np.pi * rng.uniform(18, 32) * np.arange(n) / SR)
    return _norm(x)


def swoosh(rng: np.random.Generator) -> np.ndarray:
    """Short, darker swoosh for cards and lower-thirds."""
    dur = rng.uniform(0.28, 0.4)
    n = int(dur * SR)
    x = _sweep_filter(_noise(n, rng, "pink"), rng.uniform(1800, 2600), rng.uniform(250, 400), 1.0)
    return _norm(x * _env(n, 1.5, 2.5, 0.35))


def impact(rng: np.random.Generator) -> np.ndarray:
    """Cinematic low hit: pitched sine drop + noise transient + short room tail."""
    dur = rng.uniform(0.9, 1.4)
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = rng.uniform(55, 80) * np.exp(-t * rng.uniform(2.5, 5)) + rng.uniform(32, 42)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * rng.uniform(3.5, 5.5))
    click = _noise(n, rng, "white") * np.exp(-t * 60) * 0.5
    tail = _sweep_filter(_noise(n, rng, "brown"), 900, 120, 0.7) * np.exp(-t * 4) * 0.35
    return _norm(body + click + tail)


def riser(rng: np.random.Generator, dur: float = 1.1) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = np.exp(np.linspace(np.log(rng.uniform(180, 260)), np.log(rng.uniform(1100, 1600)), n))
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.35
    air = _sweep_filter(_noise(n, rng, "white"), 400, 7000, 1.0)
    env = (t / dur) ** 2.2
    x = (tone + air) * env
    x[-int(0.02 * SR):] *= np.linspace(1, 0, int(0.02 * SR))
    return _norm(x)


def pop(rng: np.random.Generator) -> np.ndarray:
    dur = 0.12
    n = int(dur * SR)
    t = np.arange(n) / SR
    f0 = rng.uniform(520, 760)
    f = f0 * (1 + 0.6 * np.exp(-t * 60))
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * rng.uniform(28, 40))
    x[: int(0.002 * SR)] *= np.linspace(0, 1, int(0.002 * SR))
    return _norm(x)


def tick(rng: np.random.Generator) -> np.ndarray:
    n = int(0.035 * SR)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * rng.uniform(2200, 3200) * t) * np.exp(-t * 220) + _noise(n, rng, "white") * np.exp(-t * 400) * 0.3
    return _norm(x)


def shimmer(rng: np.random.Generator) -> np.ndarray:
    dur = rng.uniform(1.2, 1.8)
    n = int(dur * SR)
    t = np.arange(n) / SR
    base = rng.choice([1318.5, 1567.98, 1760.0])
    x = sum(np.sin(2 * np.pi * base * m * (1 + rng.uniform(-0.003, 0.003)) * t + rng.uniform(0, 6)) / (k + 1)
            for k, m in enumerate((1, 1.5, 2, 3)))
    x *= np.exp(-t * 2.8) * (1 - np.exp(-t * 80))
    return _norm(x)


GENERATORS = {"whoosh": whoosh, "swoosh": swoosh, "impact": impact, "riser": riser,
              "pop": pop, "tick": tick, "shimmer": shimmer}
# mix levels relative to the voice (dB)
LEVELS = {"whoosh": -19, "swoosh": -22, "impact": -15, "riser": -21, "pop": -18, "tick": -24, "shimmer": -20}


def _write(path: Path, x: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


class Pack:
    """The SFX variants for one video. `get("whoosh")` rotates through 3 whoosh variants."""

    def __init__(self, seed: int, out_dir: Path, variants: int = 3):
        self.seed, self.dir, self.n = seed, out_dir, variants
        self._turn: dict[str, int] = {}

    def get(self, kind: str) -> tuple[Path, float]:
        k = self._turn.get(kind, 0)
        self._turn[kind] = k + 1
        v = k % (self.n if kind in ("whoosh", "swoosh", "pop", "tick") else 1)
        path = self.dir / f"{kind}_{self.seed}_{v}.wav"
        if not path.exists():
            rng = np.random.default_rng([self.seed, sum(map(ord, kind)), v])
            _write(path, GENERATORS[kind](rng))
        return path, LEVELS[kind]


def pack(seed: int, out_dir: Path) -> Pack:
    return Pack(seed, out_dir)
