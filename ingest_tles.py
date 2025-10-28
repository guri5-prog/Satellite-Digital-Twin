import requests
import psycopg2
from datetime import datetime, timedelta

# --- Configuration ---
DB_CONFIG = {
    "dbname": "satellite_db",
    "user": "postgres",
    "password": "sanjibanipaul",
    "host": "localhost",
    "port": "5432"
}

# For our MVP, we'll track a list of ~20 well-known satellites by their NORAD ID
SATELLITE_NORAD_IDS = [
    25544,  # INTERNATIONAL SPACE STATION
    20580,  # HUBBLE SPACE TELESCOPE
    56261,  # STARLINK-4369
    56252,  # STARLINK-4360
    56249,  # STARLINK-4357
    27004,  # GOES 13
    23549,  # MGS
    43187,  # Sentinel-3B
    40911,  # Sentinel-3A
    39427,  # Sentinel-1A
    43678,  # Sentinel-6A
    25994,  # TERRA
    27424,  # AQUA
    28654,  # AURA
    28017,  # GPS BIIF-1 (PRN 01)
    40294,  # GPS BIIF-11 (PRN 03)
    39166,  # GPS BIIF-8 (PRN 06)
    41019,  # GPS BIIF-12 (PRN 08)
    25338,  # NOAA 15
    28654,  # NOAA 18
]

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={}&FORMAT=tle"

def parse_tle_epoch(tle_line1: str) -> datetime:
    """Converts the TLE epoch string to a timezone-aware datetime object."""
    epoch_str = tle_line1[18:32]
    year = int("20" + epoch_str[0:2])
    day_of_year = float(epoch_str[2:])
    # Create the datetime object by adding the fractional days to the start of the year
    return datetime(year, 1, 1) + timedelta(days=day_of_year - 1)

def main():
    """Fetches TLEs and stores them in the database."""
    conn = None
    try:
        # Connect to the PostgreSQL database
        print("Connecting to the database...")
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # Process each satellite
        for norad_id in SATELLITE_NORAD_IDS:
            print(f"Fetching TLE for NORAD ID: {norad_id}...")
            url = CELESTRAK_URL.format(norad_id)
            response = requests.get(url)

            if response.status_code == 200 and response.text.strip():
                lines = response.text.strip().splitlines()
                if len(lines) < 3:
                    print(f" > Warning: TLE data incomplete for NORAD ID {norad_id}")
                    print(f" > Raw response:\n{response.text}")
                    continue
                name = lines[0].strip()
                line1 = lines[1].strip()
                line2 = lines[2].strip()

                # --- 1. Insert or find the satellite ---
                # Check if satellite exists, if not, insert it
                cur.execute("INSERT INTO satellites (norad_cat_id, name) VALUES (%s, %s) ON CONFLICT (norad_cat_id) DO NOTHING;", (norad_id, name))
                # Get the satellite's primary key
                cur.execute("SELECT id FROM satellites WHERE norad_cat_id = %s;", (norad_id,))
                satellite_id = cur.fetchone()[0]

                # --- 2. Parse the epoch and insert the TLE ---
                epoch = parse_tle_epoch(line1)
                cur.execute(
                    """
                    INSERT INTO tles (satellite_id, epoch, line1, line2)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (satellite_id, epoch, line1, line2)
                )
                print(f"  > Successfully stored TLE for {name}")

            else:
                print(f"  > Failed to fetch TLE for NORAD ID: {norad_id}")

        # Commit the transaction
        conn.commit()
        print("\nData ingestion complete. All changes have been committed.")

    except (Exception, psycopg2.DatabaseError) as error:
        print(f"\nDatabase error: {error}")
        if conn:
            conn.rollback()
    finally:
        # Close the connection
        if conn:
            cur.close()
            conn.close()
            print("Database connection closed.")

if __name__ == "__main__":
    main()