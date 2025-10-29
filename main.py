from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from sgp4.api import Satrec, jday
from datetime import datetime
import math
import redis
import json

# --- Constants ---
CACHE_KEY = "satellite_positions_v2"   # used by worker.py
LIVE_CACHE_KEY = "live_satellite_positions"

app = FastAPI(title="Satellite API", version="2.1")

# --- CORS for local frontend ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- PostgreSQL Config ---
DB_CONFIG = {
    "dbname": "satellite_db",
    "user": "postgres",
    "password": "gshekhar81461",
    "host": "localhost",
    "port": "5432",
}

# --- Redis Config ---
try:
    redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    redis_client.ping()
    print("✅ API: Connected to Redis successfully.")
except redis.exceptions.ConnectionError:
    redis_client = None
    print("⚠️ API Warning: Redis not connected. Proceeding without cache.")


# --- Helper: Compute current lat/lon/alt using SGP4 ---
def get_current_position(line1: str, line2: str):
    """Compute real-time satellite position (lat, lon, alt_km)."""
    try:
        sat = Satrec.twoline2rv(line1, line2)
        now = datetime.utcnow()
        jd, fr = jday(
            now.year, now.month, now.day,
            now.hour, now.minute, now.second + now.microsecond * 1e-6
        )
        e, r, v = sat.sgp4(jd, fr)
        if e != 0:
            return None

        x, y, z = r  # km
        r_mag = math.sqrt(x**2 + y**2 + z**2)
        lat = math.degrees(math.asin(z / r_mag))
        lon = math.degrees(math.atan2(y, x))
        alt_km = r_mag - 6371.0  # Earth's mean radius (km)
        return {"lat": lat, "lon": lon, "alt_km": alt_km}
    except Exception as e:
        print("SGP4 error:", e)
        return None


# --- API: Serve Satellite Data (with Redis + Fallback) ---
@app.get("/api/v1/satellites")
async def get_satellite_positions():
    """
    Returns live orbital positions + predicted samples (if available).
    Prefer Redis cache from worker.py; fall back to fresh DB + SGP4 computation.
    """
    try:
        # ✅ Step 1: Try cached orbit samples from Redis (preferred)
        if redis_client:
            cached = redis_client.get(CACHE_KEY)
            if cached:
                print("Using cached satellite data from Redis.")
                return json.loads(cached)

        # ⚙️ Step 2: Fall back to compute live positions from PostgreSQL
        print("Redis empty — computing live positions from DB.")
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            SELECT s.name, s.norad_cat_id, t.line1, t.line2
            FROM satellites s
            JOIN (
                SELECT satellite_id, line1, line2, epoch,
                ROW_NUMBER() OVER (PARTITION BY satellite_id ORDER BY epoch DESC) AS rn
                FROM tles
            ) t ON s.id = t.satellite_id
            WHERE t.rn = 1;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        satellites = []
        for name, norad_id, line1, line2 in rows:
            pos = get_current_position(line1, line2)
            if pos:
                satellites.append({
                    "name": name,
                    "norad_id": norad_id,
                    "lat": pos["lat"],
                    "lon": pos["lon"],
                    "alt_km": pos["alt_km"],
                    "samples": [  # minimal single-point fallback
                        {
                            "t": datetime.utcnow().isoformat(),
                            "lat": pos["lat"],
                            "lon": pos["lon"],
                            "alt_km": pos["alt_km"]
                        }
                    ]
                })

        result = {"satellites": satellites}

        # 🧠 Cache result briefly in Redis
        if redis_client:
            redis_client.set(LIVE_CACHE_KEY, json.dumps(result), ex=10)

        return result

    except Exception as e:
        print("API Error:", e)
        return {"error": str(e)}
