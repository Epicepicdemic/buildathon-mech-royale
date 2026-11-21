# Mech Royale: Wilce

A Python mechfight/1 bot. It uses a conservative arena grid, measured exposure and nearby cover, zone routing, weapon leading, and cover-seeking health management. Its performance in live matches is unmeasured.

Build: `docker build --platform linux/amd64 -t mech-royale .`. The image generates `map.dat` from the included arena at build time. Run the kit check from this directory:

```
python3 check/check.py -- python3 -u bot.py
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech-royale
```

Dependencies: Python 3.13.9 standard library and NumPy 2.3.5 (map generation only). The included `mechfight.py`, arena and conformance fixtures are from the organiser's starter kit. License: MIT (see LICENSE).
