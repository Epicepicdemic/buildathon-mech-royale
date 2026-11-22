import os

# the unit tests pin B2's behaviour: run them on mech/squad_settings.py defaults, not a variant file
os.environ.setdefault("MECH_SQUAD_DEFAULTS", "1")
