import asyncio
import logging
import os
import random
import string
import time
import json
from typing import Optional, Dict, List
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import redis.asyncio as redis
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================
# ДАННЫЕ
# ==========================
CHAMPIONS = [
    "Ahri", "Akali", "Alistar", "Amumu", "Annie", "Ashe", "Aurelion Sol",
    "Azir", "Bard", "Blitzcrank", "Brand", "Braum", "Caitlyn", "Camille",
    "Cassiopeia", "Darius", "Diana", "Draven", "Ekko", "Elise", "Evelynn",
    "Ezreal", "Fiora", "Fizz", "Galio", "Garen", "Gnar", "Gragas", "Graves",
    "Irelia", "Ivern", "Janna", "Jarvan IV", "Jax", "Jhin", "Jinx", "Kai'Sa",
    "Karma", "Karthus", "Kassadin", "Katarina", "Kayle", "Kayn", "Kennen",
    "Kha'Zix", "Kindred", "Kled", "Leblanc", "Lee Sin", "Leona", "Lissandra",
    "Lucian", "Lulu", "Lux", "Malphite", "Malzahar", "Maokai", "Master Yi",
    "Miss Fortune", "Mordekaiser", "Morgana", "Nami", "Nasus", "Nautilus",
    "Nidalee", "Nocturne", "Nunu & Willump", "Olaf", "Orianna", "Ornn",
    "Pantheon", "Poppy", "Pyke", "Quinn", "Rakan", "Rammus", "Rek'Sai",
    "Renekton", "Rengar", "Riven", "Rumble", "Ryze", "Samira", "Sejuani",
    "Senna", "Seraphine", "Shaco", "Shen", "Shyvana", "Singed", "Sion",
    "Sivir", "Skarner", "Sona", "Soraka", "Swain", "Sylas", "Syndra",
    "Tahm Kench", "Taliyah", "Talon", "Taric", "Teemo", "Thresh", "Tristana",
    "Trundle", "Tryndamere", "Twisted Fate", "Twitch", "Udyr", "Urgot",
    "Varus", "Vayne", "Veigar", "Vel'Koz", "Vi", "Viego", "Viktor",
    "Vladimir", "Volibear", "Warwick", "Wukong", "Xayah", "Xerath", "Xin Zhao",
    "Yasuo", "Yone", "Yorick", "Yuumi", "Zac", "Zed", "Ziggs", "Zilean",
    "Zoe", "Zyra",
]
MIN_PLAYERS = 3
SPY_CARD = "Шпион"

# ==========================
# МОДЕЛИ ДАННЫХ (Pydantic для легкой сериализации в Redis)
# ==========================
class Player(BaseModel):
    user_id: str
    name: str

class Lobby(BaseModel):
    code: str
    host_id: str
    players: Dict[str, Player] = Field(default_factory=dict)
    status: str = "lobby"  # lobby | playing | voting | results
    common_champion: Optional[str] = None
    spy_champion: Optional[str] = None
    spy_id: Optional[str] = None
    order: List[str] = Field(default_factory=list)
    votes: Dict[str, str] = Field(default_factory=dict)  # voter_id -> target_id
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

# ==========================
# REDIS КЛИЕНТ (Upstash)
# ==========================
REDIS_URL = os.environ.get("UPSTASH_REDIS_URL", "redis://localhost:6379")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

async def get_lobby(code: str) -> Optional[Lobby]:
    data = await redis_client.get(f"lobby:{code}")
    if data:
        return Lobby.model_validate_json(data)
    return None

async def save_lobby(lobby: Lobby):
    lobby.updated_at = time.time()
    # Сохраняем с TTL 24 часа (86400 секунд), чтобы не засорять Redis
    await redis_client.set(f"lobby:{lobby.code}", lobby.model_dump_json(), ex=86400)

async def delete_lobby(code: str):
    await redis_client.delete(f"lobby:{code}")

# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================
def gen_code() -> str:
    chars = string.ascii_uppercase + string.digits
    # В реальном мире лучше проверять и в Redis, но для 5 символов коллизии крайне редки
    return "".join(random.choices(chars, k=5))

def require_player(lobby: Lobby, user_id: str) -> Player:
    player = lobby.players.get(user_id)
    if not player:
        raise HTTPException(status_code=403, detail="Вы не в этом лобби")
    return player

def require_host(lobby: Lobby, user_id: str):
    if lobby.host_id != user_id:
        raise HTTPException(status_code=403, detail="Действие доступно только хосту")

# ==========================
# СХЕМЫ ЗАПРОСОВ
# ==========================
class CreateLobbyRequest(BaseModel):
    user_id: str
    name: str

class JoinLobbyRequest(BaseModel):
    code: str
    user_id: str
    name: str

class CodeUserRequest(BaseModel):
    code: str
    user_id: str

class VoteRequest(BaseModel):
    code: str
    user_id: str
    target_id: str  # может быть "skip"

# ==========================
# СЕРИАЛИЗАЦИЯ СОСТОЯНИЯ
# ==========================
def serialize_state(lobby: Lobby, user_id: str) -> dict:
    players = [
        {"user_id": p.user_id, "name": p.name, "is_host": p.user_id == lobby.host_id}
        for p in lobby.players.values()
    ]
    data = {
        "code": lobby.code,
        "status": lobby.status,
        "host_id": lobby.host_id,
        "you": user_id,
        "players": players,
        "min_players": MIN_PLAYERS,
    }

    if lobby.status in ("playing", "voting", "results"):
        is_spy = user_id == lobby.spy_id
        data["card"] = SPY_CARD if is_spy else lobby.common_champion
        data["is_spy"] = is_spy
        data["order"] = [
            {"user_id": uid, "name": lobby.players[uid].name}
            for uid in lobby.order
            if uid in lobby.players
        ]

    if lobby.status in ("voting", "results"):
        data["votes_cast"] = list(lobby.votes.keys())
        data["votes_total"] = len(lobby.players)
        data["your_vote"] = lobby.votes.get(user_id)

    if lobby.status == "results":
        tally: Dict[str, int] = {}
        for target in lobby.votes.values():
            tally[target] = tally.get(target, 0) + 1

        candidates = {k: v for k, v in tally.items() if k != "skip" and k in lobby.players}
        accused_id = None
        if candidates:
            max_votes = max(candidates.values())
            top = [k for k, v in candidates.items() if v == max_votes]
            if len(top) == 1:
                accused_id = top[0]

        data["results"] = {
            "spy_id": lobby.spy_id,
            "spy_name": lobby.players[lobby.spy_id].name if lobby.spy_id in lobby.players else "—",
            "common_champion": lobby.common_champion,
            "spy_champion": lobby.spy_champion,
            "tally": [
                {
                    "user_id": uid,
                    "name": lobby.players[uid].name if uid in lobby.players else "—",
                    "votes": cnt,
                }
                for uid, cnt in tally.items()
                if uid != "skip"
            ] + ([{"user_id": "skip", "name": "Пропустили", "votes": tally.get("skip", 0)}] if tally.get("skip") else []),
            "accused_id": accused_id,
            "accused_name": lobby.players[accused_id].name if accused_id in lobby.players else None,
            "players_caught_spy": accused_id == lobby.spy_id,
        }

    return data

# ==========================
# FASTAPI APP
# ==========================
app = FastAPI(title="Spy: LoL Edition")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/create")
async def create_lobby(req: CreateLobbyRequest):
    code = gen_code()
    lobby = Lobby(code=code, host_id=req.user_id)
    lobby.players[req.user_id] = Player(user_id=req.user_id, name=req.name or "Игрок")
    await save_lobby(lobby)
    return serialize_state(lobby, req.user_id)

@app.post("/api/join")
async def join_lobby(req: JoinLobbyRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    if lobby.status != "lobby":
        raise HTTPException(status_code=400, detail="Игра уже началась, присоединиться нельзя")

    if req.user_id not in lobby.players:
        lobby.players[req.user_id] = Player(user_id=req.user_id, name=req.name or "Игрок")
        await save_lobby(lobby)

    return serialize_state(lobby, req.user_id)

@app.get("/api/state/{code}")
async def get_state(code: str, user_id: str):
    lobby = await get_lobby(code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_player(lobby, user_id)
    return serialize_state(lobby, user_id)

@app.post("/api/start")
async def start_game(req: CodeUserRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_host(lobby, req.user_id)
    if lobby.status != "lobby":
        raise HTTPException(status_code=400, detail="Игра уже запущена")

    if len(lobby.players) < MIN_PLAYERS:
        raise HTTPException(status_code=400, detail=f"Нужно минимум {MIN_PLAYERS} игроков, сейчас {len(lobby.players)}")

    common, spy_champ = random.sample(CHAMPIONS, 2)
    lobby.common_champion = common
    lobby.spy_champion = spy_champ

    player_ids = list(lobby.players.keys())
    lobby.spy_id = random.choice(player_ids)

    order = player_ids[:]
    random.shuffle(order)
    if order[0] == lobby.spy_id and len(order) > 1:
        swap = random.randint(1, len(order) - 1)
        order[0], order[swap] = order[swap], order[0]
    lobby.order = order

    lobby.votes.clear()
    lobby.status = "playing"
    await save_lobby(lobby)

    return serialize_state(lobby, req.user_id)

@app.post("/api/begin_voting")
async def begin_voting(req: CodeUserRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_host(lobby, req.user_id)
    if lobby.status != "playing":
        raise HTTPException(status_code=400, detail="Сейчас не фаза обсуждения")

    lobby.status = "voting"
    await save_lobby(lobby)
    return serialize_state(lobby, req.user_id)

@app.post("/api/vote")
async def cast_vote(req: VoteRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_player(lobby, req.user_id)
    if lobby.status != "voting":
        raise HTTPException(status_code=400, detail="Сейчас не фаза голосования")

    target = req.target_id
    if target != "skip" and target not in lobby.players:
        raise HTTPException(status_code=400, detail="Некорректная цель голосования")

    lobby.votes[req.user_id] = target
    await save_lobby(lobby)

    if len(lobby.votes) >= len(lobby.players):
        lobby.status = "results"
        await save_lobby(lobby)

    return serialize_state(lobby, req.user_id)

@app.post("/api/reveal")
async def reveal_now(req: CodeUserRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_host(lobby, req.user_id)
    if lobby.status != "voting":
        raise HTTPException(status_code=400, detail="Сейчас не фаза голосования")

    for uid in lobby.players:
        lobby.votes.setdefault(uid, "skip")

    lobby.status = "results"
    await save_lobby(lobby)
    return serialize_state(lobby, req.user_id)

@app.post("/api/play_again")
async def play_again(req: CodeUserRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_host(lobby, req.user_id)
    if lobby.status != "results":
        raise HTTPException(status_code=400, detail="Раунд ещё не завершён")

    lobby.status = "lobby"
    lobby.common_champion = None
    lobby.spy_champion = None
    lobby.spy_id = None
    lobby.order = []
    lobby.votes.clear()
    await save_lobby(lobby)

    return serialize_state(lobby, req.user_id)

@app.post("/api/leave")
async def leave_lobby(req: CodeUserRequest):
    lobby = await get_lobby(req.code)
    if not lobby:
        raise HTTPException(status_code=404, detail="Лобби не найдено")
    require_player(lobby, req.user_id)
    if lobby.status != "lobby":
        raise HTTPException(status_code=400, detail="Нельзя выйти во время игры")

    del lobby.players[req.user_id]

    if not lobby.players:
        await delete_lobby(lobby.code)
        return {"ok": True, "lobby_deleted": True}

    if lobby.host_id == req.user_id:
        lobby.host_id = next(iter(lobby.players.keys()))

    await save_lobby(lobby)
    return {"ok": True, "lobby_deleted": False}

# ==========================
# СТАТИКА И TELEGRAM WEBHOOK (Serverless-friendly)
# ==========================
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    return FileResponse("static/index.html")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

@app.on_event("startup")
async def startup_event():
    if BOT_TOKEN and WEBAPP_URL:
        webhook_url = f"{WEBAPP_URL.rstrip('/')}/webhook"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                    json={"url": webhook_url}
                )
                if response.json().get("ok"):
                    logger.info(f"✅ Webhook успешно установлен на: {webhook_url}")
                else:
                    logger.error(f"❌ Ошибка установки webhook: {response.text}")
            except Exception as e:
                logger.error(f"❌ Исключение при установке webhook: {e}")
    else:
        logger.warning("⚠️ BOT_TOKEN или WEBAPP_URL не заданы. Бот не будет отвечать.")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Легковесный обработчик вебхуков для Vercel (заменяет тяжелый polling)"""
    if not BOT_TOKEN:
        return {"ok": True}
    
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    if "message" in update and "text" in update["message"]:
        text = update["message"]["text"].strip().lower()
        chat_id = update["message"]["chat"]["id"]
        
        if text in ["/start", "/play"]:
            payload = {
                "chat_id": chat_id,
                "text": "Привет! Это бот для игры в «Шпиона» по League of Legends.\n\nНажми кнопку ниже, чтобы открыть приложение: создай лобби или введи код, чтобы присоединиться к друзьям.",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "🎮 Открыть игру", "web_app": {"url": WEBAPP_URL}}
                    ]]
                }
            }
            async with httpx.AsyncClient() as client:
                await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
                
    return {"ok": True}