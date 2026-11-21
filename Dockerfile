# Build stage: compute the world tables from the arena (NumPy is used only here).
FROM python:3.13.9-slim AS build
WORKDIR /bot
RUN pip install --no-cache-dir numpy==2.3.5
COPY tools/build_world.py ./tools/build_world.py
COPY arena ./arena
RUN python tools/build_world.py arena worlddata/world.bin

# Runtime: the bot is standard library apart from the air tables' query module (mech/airmap),
# which uses NumPy (pinned, installed system-wide; the bot runs as uid 65534).
FROM python:3.13.9-slim
RUN pip install --no-cache-dir numpy==2.3.5
WORKDIR /bot
COPY --from=build /bot/worlddata/world.bin ./worlddata/world.bin
COPY bot.py mechfight.py ./
COPY mech ./mech
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
