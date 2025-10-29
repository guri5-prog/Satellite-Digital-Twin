import asyncio
import asyncpg
import redis
import json
from datetime import datetime, timedelta, timezone
from skyfield.api import EarthSatellite, load
from sgp4.api import Satrec, jday
import math
from datetime import datetime

# --- Global Constants ---
PREDICT_SECONDS = 90 * 60        # 90 minutes total orbit prediction
LOOKBACK_SECONDS = 5 * 60        # 🔧 Start 5 minutes in the past
SAMPLE_INTERVAL = 30             # seconds between samples
CACHE_KEY = "satellite_positions_v2"
CACHE_TTL_SECONDS = 60           # refresh every minute

# --- Database Configuration ---
DB_CONFIG = {
    "database": "satellite_db",
    "user": "postgres",
    "password": "gshekhar81461",
    "host": "localhost",
    "port": "5432"
}

def compute_realtime_position(line1, line2):
    """Compute current lat, lon, alt from TLE using SGP4."""
    try:
        sat = Satrec.twoline2rv(line1, line2)
        now = datetime.utcnow()
        jd, fr = jday(now.year, now.month, now.day, now.hour, now.minute, now.second + now.microsecond * 1e-6)
        e, r, v = sat.sgp4(jd, fr)

        if e != 0:
            return None

        x, y, z = r  # km in TEME frame
        r_mag = math.sqrt(x**2 + y**2 + z**2)
        lat = math.degrees(math.asin(z / r_mag))
        lon = math.degrees(math.atan2(y, x))
        alt_km = r_mag - 6371.0  # Earth's mean radius

        return {"lat": lat, "lon": lon, "alt_km": alt_km}
    except Exception as ex:
        print(f"SGP4 Error: {ex}")
        return None


# --- Redis Configuration ---
try:
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    redis_client.ping()
    print("WORKER: Successfully connected to Redis cache.")
except redis.exceptions.ConnectionError as e:
    print(f"WORKER Error: Could not connect to Redis: {e}")
    redis_client = None

# --- Skyfield Loader ---
ts = load.timescale()

# ==============================================================
#  Satellite Position + Orbit Prediction Logic
# ==============================================================

def calculate_satellite_position(line1: str, line2: str):
    """
    Calculates current satellite position.
    Returns dict with ECI (x, y, z) km and geodetic lat/lon/alt.
    """
    try:
        t = ts.now()
        sat = EarthSatellite(line1, line2)

        # Earth-Centered Inertial coordinates
        eci = sat.at(t).position.km

        # Geographic subpoint
        sub = sat.at(t).subpoint()
        return {
            "eci_pos": tuple(eci),
            "geo_pos": (
                sub.latitude.degrees,
                sub.longitude.degrees,
                sub.elevation.km
            )
        }
    except Exception as e:
        print(f"Error calculating position: {e}")
        return None


def compute_future_samples(line1: str, line2: str, predict_seconds=PREDICT_SECONDS, sample_interval=SAMPLE_INTERVAL):
    """
    🔧 FIXED: Predicts orbit samples starting from LOOKBACK_SECONDS in the past
    through PREDICT_SECONDS in the future.
    This ensures Cesium always has valid data at the current time.
    """
    samples = []
    now_dt_utc = datetime.now(timezone.utc)
    
    # 🔧 Start from the past to ensure current time is covered
    start_time = now_dt_utc - timedelta(seconds=LOOKBACK_SECONDS)
    total_duration = LOOKBACK_SECONDS + predict_seconds
    n = int(total_duration // sample_interval) + 1
    
    sat = EarthSatellite(line1, line2)

    for i in range(n):
        t_dt = start_time + timedelta(seconds=i * sample_interval)
        t_sf = ts.utc(
            t_dt.year, t_dt.month, t_dt.day,
            t_dt.hour, t_dt.minute, t_dt.second + t_dt.microsecond / 1e6
        )
        try:
            sub = sat.at(t_sf).subpoint()
            samples.append({
                "t": t_dt.isoformat(),
                "lat": sub.latitude.degrees,
                "lon": sub.longitude.degrees,
                "alt_km": sub.elevation.km
            })
        except Exception as e:
            print(f"Sample calculation error at time {t_dt}: {e}")
            continue
            
    return samples

# ==============================================================
#  Database Initialization - Check PostGIS Extension
# ==============================================================

async def ensure_postgis(pool):
    """
    Verify and enable PostGIS extension if needed.
    """
    try:
        async with pool.acquire() as conn:
            # Check if PostGIS is installed
            result = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM pg_available_extensions 
                    WHERE name = 'postgis'
                );
            """)
            
            if not result:
                print("WARNING: PostGIS extension is not available. Install it first:")
                print("  sudo apt-get install postgresql-postgis")
                return False
            
            # Enable PostGIS if not already enabled
            await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
            
            # Verify PostGIS is working
            version = await conn.fetchval("SELECT PostGIS_version();")
            print(f"PostGIS enabled: {version}")
            
            # Verify geopoint column exists
            column_exists = await conn.fetchval("""
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'satellites' 
                    AND column_name = 'geopoint'
                );
            """)
            
            if not column_exists:
                print("Creating geopoint column...")
                await conn.execute("""
                    ALTER TABLE satellites 
                    ADD COLUMN geopoint geometry(Point, 4326);
                """)
            
            return True
            
    except Exception as e:
        print(f"Error setting up PostGIS: {e}")
        return False

# ==============================================================
#  Async Worker Logic
# ==============================================================

async def fetch_and_calculate(pool):
    """
    Fetch latest TLEs, compute positions + predictions,
    update PostGIS, cache results in Redis.
    """
    print(f"[{datetime.now()}] Worker cycle starting: Fetching TLEs...")
    satellites_data = []
    postgis_update_args = []

    try:
        async with pool.acquire() as conn:
            satellites = await conn.fetch("""
                SELECT 
                    s.id as satellite_db_id, 
                    s.name, 
                    s.norad_cat_id, 
                    t.line1, 
                    t.line2
                FROM satellites s
                JOIN (
                    SELECT 
                        satellite_id, line1, line2,
                        ROW_NUMBER() OVER(PARTITION BY satellite_id ORDER BY epoch DESC) AS rn
                    FROM tles
                ) t ON s.id = t.satellite_id
                WHERE t.rn = 1;
            """)

        print(f"DB Fetch OK: Found {len(satellites)} satellites.")

        # --- Concurrently compute positions ---
        calculation_tasks = []
        for sat in satellites:
            task = asyncio.to_thread(calculate_satellite_position, sat['line1'], sat['line2'])
            calculation_tasks.append((sat, task))

        for sat, task in calculation_tasks:
            pos_data = await task
            if not pos_data:
                continue

            eci = pos_data["eci_pos"]
            lat, lon, alt = pos_data["geo_pos"]

            # Predict orbit samples (now includes past data)
            try:
                samples = compute_future_samples(sat['line1'], sat['line2'])
                print(f"Generated {len(samples)} samples for {sat['name']}")
            except Exception as e:
                print(f"Prediction error for {sat['name']}: {e}")
                samples = []

            satellites_data.append({
                "id": sat['satellite_db_id'],
                "name": sat['name'],
                "norad_id": sat['norad_cat_id'],
                "latitude": lat,
                "longitude": lon,
                "altitude": alt,
                "eci": eci,
                "samples": samples
            })

            # PostGIS update - explicitly cast to float
            postgis_update_args.append((
                float(lon),
                float(lat),
                sat['satellite_db_id']
            ))

        # --- Batch update PostGIS geopoints ---
        if postgis_update_args:
            async with pool.acquire() as conn:
                try:
                    await conn.executemany("""
                        UPDATE satellites 
                        SET geopoint = ST_SetSRID(
                            ST_MakePoint($1::double precision, $2::double precision), 
                            4326
                        )
                        WHERE id = $3
                    """, postgis_update_args)
                    print(f"PostGIS OK: Updated {len(postgis_update_args)} satellites.")
                except Exception as e:
                    print(f"WORKER Error updating PostGIS: {e}")
                    import traceback
                    traceback.print_exc()

        # --- Cache results in Redis ---
        if redis_client and satellites_data:
            json_data = json.dumps({"satellites": satellites_data})
            redis_client.set(CACHE_KEY, json_data, ex=CACHE_TTL_SECONDS)
            print("Cache Write OK: Updated satellite positions in Redis.")

        print(f"Cycle complete. Processed {len(satellites_data)} satellites.")

    except (Exception, asyncpg.PostgresError) as error:
        print(f"WORKER Error: {error}")
        import traceback
        traceback.print_exc()


async def main():
    """
    Initialize connection pool, then run worker cycles continuously.
    """
    pool = None
    try:
        pool = await asyncpg.create_pool(**DB_CONFIG)
        print("WORKER: Database pool created.")
        
        # Ensure PostGIS is set up
        postgis_ok = await ensure_postgis(pool)
        if not postgis_ok:
            print("WORKER Error: PostGIS setup failed. Continuing without PostGIS updates...")
        
    except Exception as e:
        print(f"WORKER Error: Could not create DB pool: {e}")
        return

    if not redis_client:
        print("WORKER Error: No Redis connection. Exiting.")
        return

    while True:
        await fetch_and_calculate(pool)
        await asyncio.sleep(CACHE_TTL_SECONDS)

# --- Entry Point ---
if __name__ == "__main__":
    asyncio.run(main())