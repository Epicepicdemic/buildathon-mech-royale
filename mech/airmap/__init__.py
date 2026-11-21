"""Precomputed roof and jet-route tables (analysis/mech/air, built and tested separately; ours).

`AirMap` answers in microseconds: which roofs we can reach from here and for how much energy,
the wall hop that breaks sight to a pursuer, where stepping off a roof lands, roof edge firing
spots, and which roof stays inside a closing last circle longest. It needs NumPy at run time.
"""
from .airmap import AirMap  # noqa: F401
