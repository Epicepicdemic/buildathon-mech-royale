# Build stage: compute the world tables from the arena (NumPy is used only here).
FROM python:3.13.9-slim AS build
WORKDIR /bot
RUN pip install --no-cache-dir numpy==2.3.5
COPY tools/build_world.py ./tools/build_world.py
COPY arena ./arena
RUN python tools/build_world.py arena worlddata/world.bin

# Runtime: standard library only.
FROM python:3.13.9-slim
WORKDIR /bot
COPY --from=build /bot/worlddata/world.bin ./worlddata/world.bin
COPY bot.py mechfight.py ./
COPY mech ./mech
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
