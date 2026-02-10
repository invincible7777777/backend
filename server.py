from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
try:
    from motor.motor_asyncio import AsyncIOMotorClient
except Exception:
    AsyncIOMotorClient = None
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timezone, time as dt_time
import json
import asyncio
import aiofiles
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except Exception:
    firebase_admin = None
    credentials = None
    firestore = None

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

class InMemoryCursor:
    def __init__(self, docs):
        self.docs = list(docs)
    def sort(self, field, order):
        reverse = order == -1
        self.docs.sort(key=lambda d: d.get(field), reverse=reverse)
        return self
    async def to_list(self, n):
        return self.docs[:n]

class InMemoryCollection:
    def __init__(self):
        self.docs = []
    async def insert_one(self, doc):
        self.docs.append(doc)
        return {}
    def find(self, query=None, projection=None):
        return InMemoryCursor(self.docs)
    async def find_one(self, filt, projection=None):
        for d in self.docs:
            ok = True
            for k, v in filt.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                return d
        return None
    async def update_one(self, filt, update):
        for i, d in enumerate(self.docs):
            ok = True
            for k, v in filt.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                set_fields = update.get('$set', {})
                self.docs[i] = {**d, **set_fields}
                return {"modified_count": 1}
        return {"modified_count": 0}

class InMemoryDB:
    def __init__(self):
        self.messages = InMemoryCollection()
        self.games = InMemoryCollection()
        self.polls = InMemoryCollection()

# MongoDB connection with fallback
mongo_url = os.environ.get('MONGO_URL')
db_name = os.environ.get('DB_NAME', 'potato_tomato')
if mongo_url and AsyncIOMotorClient:
    client_mongo = AsyncIOMotorClient(mongo_url)
    db = client_mongo[db_name]
else:
    client_mongo = None
    db = InMemoryDB()

# Firebase initialization with placeholder credentials
try:
    firebase_config = json.loads(os.environ.get('FIREBASE_CONFIG', '{}'))
    if firebase_config and firebase_admin and credentials and firestore:
        cred = credentials.Certificate(firebase_config)
        firebase_admin.initialize_app(cred)
        firestore_db = firestore.client()
    else:
        firestore_db = None
except Exception:
    firestore_db = None

# Create the main app
app = FastAPI()
api_router = APIRouter(prefix="/api")

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.typing_status: Dict[str, bool] = {}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[username] = websocket
        self.typing_status[username] = False

    def disconnect(self, username: str):
        if username in self.active_connections:
            del self.active_connections[username]
        if username in self.typing_status:
            del self.typing_status[username]

    async def send_personal_message(self, message: dict, username: str):
        if username in self.active_connections:
            try:
                await self.active_connections[username].send_json(message)
            except Exception:
                pass

    async def broadcast(self, message: dict, exclude: str = None):
        for username, connection in list(self.active_connections.items()):
            if username != exclude:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

manager = ConnectionManager()

# Models
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    success: bool
    username: str
    token: str

class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: str
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reactions: Dict[str, List[str]] = Field(default_factory=dict)
    type: str = "text"  # text, photo, sticker
    media_url: Optional[str] = None

class GameResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    game_type: str  # tictactoe, rps
    players: List[str]
    winner: Optional[str]
    state: Dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Poll(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    options: List[str]
    votes: Dict[str, str] = Field(default_factory=dict)  # username: option
    created_by: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# Hardcoded accounts
ACCOUNTS = {
    "potato": "1",
    "tomato": "1"
}

# Auto good morning/night scheduler
async def auto_messages():
    while True:
        now = datetime.now()
        current_time = now.time()
        
        # Good morning at 07:00
        if current_time.hour == 7 and current_time.minute == 0:
            message = Message(
                sender="system",
                text="Good morning my love! ☀️💕",
                type="text"
            )
            await save_message(message)
            await manager.broadcast({
                "type": "message",
                "data": message.model_dump(mode='json')
            })
        
        # Good night at 22:00
        if current_time.hour == 22 and current_time.minute == 0:
            message = Message(
                sender="system",
                text="Good night my love! 🌙💖 Sweet dreams!",
                type="text"
            )
            await save_message(message)
            await manager.broadcast({
                "type": "message",
                "data": message.model_dump(mode='json')
            })
        
        # Check every 60 seconds
        await asyncio.sleep(60)

# Helper functions
async def save_message(message: Message):
    """Save message to both MongoDB and Firebase"""
    doc = message.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    # Save to MongoDB
    await db.messages.insert_one(doc)
    
    # Save to Firebase if available
    if firestore_db:
        try:
            firestore_db.collection('messages').document(message.id).set(doc)
        except Exception as e:
            logging.error(f"Firebase save failed: {e}")

async def save_game_result(game: GameResult):
    """Save game result to both databases"""
    doc = game.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    await db.games.insert_one(doc)
    
    if firestore_db:
        try:
            firestore_db.collection('games').document(game.id).set(doc)
        except Exception as e:
            logging.error(f"Firebase save failed: {e}")

async def save_poll(poll: Poll):
    """Save poll to both databases"""
    doc = poll.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    await db.polls.insert_one(doc)
    
    if firestore_db:
        try:
            firestore_db.collection('polls').document(poll.id).set(doc)
        except Exception as e:
            logging.error(f"Firebase save failed: {e}")

# API Routes
@api_router.post("/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Login with username and password"""
    if req.username not in ACCOUNTS or ACCOUNTS[req.username] != req.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Simple token (in production, use JWT)
    token = f"{req.username}_{uuid.uuid4()}"
    
    return LoginResponse(
        success=True,
        username=req.username,
        token=token
    )

@api_router.get("/messages")
async def get_messages():
    """Get all messages from history"""
    messages = await db.messages.find({}, {"_id": 0}).sort("timestamp", 1).to_list(1000)
    
    # Convert timestamp strings back to datetime for consistency
    for msg in messages:
        if isinstance(msg.get('timestamp'), str):
            msg['timestamp'] = datetime.fromisoformat(msg['timestamp'])
    
    return messages

@api_router.post("/messages/{message_id}/react")
async def react_to_message(message_id: str, emoji: str, username: str):
    """Add reaction to a message"""
    # Update in MongoDB
    message = await db.messages.find_one({"id": message_id}, {"_id": 0})
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")
    
    reactions = message.get('reactions', {})
    if emoji not in reactions:
        reactions[emoji] = []
    
    if username not in reactions[emoji]:
        reactions[emoji].append(username)
    
    await db.messages.update_one(
        {"id": message_id},
        {"$set": {"reactions": reactions}}
    )
    
    # Update Firebase if available
    if firestore_db:
        try:
            firestore_db.collection('messages').document(message_id).update({'reactions': reactions})
        except Exception as e:
            logging.error(f"Firebase update failed: {e}")
    
    # Broadcast reaction update
    await manager.broadcast({
        "type": "reaction",
        "data": {
            "message_id": message_id,
            "emoji": emoji,
            "username": username,
            "reactions": reactions
        }
    })
    
    return {"success": True, "reactions": reactions}

@api_router.post("/upload/photo")
async def upload_photo(file: UploadFile = File(...), username: str = ""):
    """Upload a photo"""
    upload_dir = Path("/app/backend/uploads")
    upload_dir.mkdir(exist_ok=True)
    
    file_ext = Path(file.filename).suffix
    file_id = str(uuid.uuid4())
    file_path = upload_dir / f"{file_id}{file_ext}"
    
    # Save file
    async with aiofiles.open(file_path, 'wb') as f:
        content = await file.read()
        await f.write(content)
    
    # Return URL
    file_url = f"/api/uploads/{file_id}{file_ext}"
    return {"success": True, "url": file_url}

@api_router.get("/uploads/{filename}")
async def get_upload(filename: str):
    """Serve uploaded files"""
    file_path = Path("/app/backend/uploads") / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@api_router.get("/games")
async def get_games():
    """Get game history"""
    games = await db.games.find({}, {"_id": 0}).sort("timestamp", -1).to_list(100)
    return games

@api_router.get("/polls")
async def get_polls():
    """Get all polls"""
    polls = await db.polls.find({}, {"_id": 0}).sort("timestamp", -1).to_list(100)
    return polls

@api_router.post("/polls/{poll_id}/vote")
async def vote_poll(poll_id: str, option: str, username: str):
    """Vote on a poll"""
    poll = await db.polls.find_one({"id": poll_id}, {"_id": 0})
    if not poll:
        raise HTTPException(status_code=404, detail="Poll not found")
    
    votes = poll.get('votes', {})
    votes[username] = option
    
    await db.polls.update_one(
        {"id": poll_id},
        {"$set": {"votes": votes}}
    )
    
    if firestore_db:
        try:
            firestore_db.collection('polls').document(poll_id).update({'votes': votes})
        except Exception as e:
            logging.error(f"Firebase update failed: {e}")
    
    # Broadcast vote update
    await manager.broadcast({
        "type": "poll_update",
        "data": {
            "poll_id": poll_id,
            "votes": votes
        }
    })
    
    return {"success": True, "votes": votes}

# WebSocket endpoint
@api_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, username: str):
    """WebSocket for real-time chat"""
    await manager.connect(username, websocket)
    
    # Send connection confirmation
    await manager.send_personal_message({
        "type": "connected",
        "data": {"username": username}
    }, username)
    
    # Notify others
    await manager.broadcast({
        "type": "user_connected",
        "data": {"username": username}
    }, exclude=username)
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type')
            
            if msg_type == 'message':
                # New message
                message = Message(
                    sender=username,
                    text=data['text'],
                    type=data.get('message_type', 'text'),
                    media_url=data.get('media_url')
                )
                await save_message(message)
                
                # Broadcast to all
                await manager.broadcast({
                    "type": "message",
                    "data": message.model_dump(mode='json')
                })
            
            elif msg_type == 'typing':
                # Typing indicator
                manager.typing_status[username] = data['isTyping']
                await manager.broadcast({
                    "type": "typing",
                    "data": {
                        "username": username,
                        "isTyping": data['isTyping']
                    }
                }, exclude=username)
            
            elif msg_type == 'game_result':
                # Save game result
                game = GameResult(
                    game_type=data['game_type'],
                    players=data['players'],
                    winner=data.get('winner'),
                    state=data['state']
                )
                await save_game_result(game)
                
                # Broadcast game result
                await manager.broadcast({
                    "type": "game_result",
                    "data": game.model_dump(mode='json')
                })
            
            elif msg_type == 'poll_create':
                # Create new poll
                poll = Poll(
                    question=data['question'],
                    options=data['options'],
                    created_by=username
                )
                await save_poll(poll)
                
                # Broadcast new poll
                await manager.broadcast({
                    "type": "new_poll",
                    "data": poll.model_dump(mode='json')
                })
            
            elif msg_type == 'game_state':
                # Real-time game state update
                await manager.broadcast({
                    "type": "game_state",
                    "data": data
                }, exclude=username)
    
    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast({
            "type": "user_disconnected",
            "data": {"username": username}
        })

# Include router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Start auto-message scheduler
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_messages())

@app.on_event("shutdown")
async def shutdown_db_client():
    client_mongo.close()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
