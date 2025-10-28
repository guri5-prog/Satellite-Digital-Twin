import redis
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


CACHE_KEY = "satellite_positions_v2" # Same as in our worker
try:
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    redis_client.ping()
    print("API: Successfully connected to Redis cache.")
except redis.exceptions.ConnectionError as e:
    print(f"API Error: Could not connect to Redis: {e}")
    redis_client = None

# --- FastAPI Application ---
app = FastAPI(
    title="Satellite Digital Twin API",
    description="API for tracking satellite positions (Cached).",
    version="1.1" # Bumped version
)

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/v1/satellites")
async def get_satellite_positions_cached():
    """
    Fetches the latest calculated satellite positions directly from the
    Redis cache. This endpoint does NOT perform any calculations.
    """
    if not redis_client:
        raise HTTPException(status_code=503, detail="Cache service unavailable")

    try:
        # 1. Try to get from Cache
        cached_data = redis_client.get(CACHE_KEY)
        
        if cached_data:
            # print("Cache HIT")
            # Data is stored as a JSON string, so we parse it
            return json.loads(cached_data)
        else:
            # print("Cache MISS")
            # If cache is empty, it means the worker hasn't run yet
            # or is in the middle of a cycle.
            raise HTTPException(status_code=503, 
                detail="Satellite data is currently being calculated. Please try again in a moment.")

    except Exception as e:
        print(f"API Error: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving data from cache")

# Run with:
# uvicorn main:app --reload