FROM python:3.13.9-slim AS build
WORKDIR /bot
RUN pip install --no-cache-dir numpy==2.3.5
COPY build_map.py ./
COPY arena ./arena
RUN python build_map.py
FROM python:3.13.9-slim
WORKDIR /bot
COPY --from=build /bot/map.dat ./map.dat
COPY bot.py navigation.py mechfight.py ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
