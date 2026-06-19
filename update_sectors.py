import json
import os
from vnstock import Listing

SECTOR_FILE = "sectors.json"

def update_sector_info():
    """
    Fetches listing companies from vnstock and saves the sector mapping to sectors.json.
    Format: {"HPG": "Thép", "VCB": "Ngân hàng", ...}
    """
    print("🚀 Updating sector info...")
    try:
        # Fetch listing companies using Listing class
        # Try symbols_by_industries
        listing = Listing(source='VCI') 
        df = listing.symbols_by_industries()
        
        if df is None or df.empty:
            print("❌ Failed to fetch listing companies.")
            return

        # Debug: Print columns to see what we have
        print(f"Columns found: {df.columns.tolist()}")
        
        sector_map = {}
        
        # Iterate through rows
        for index, row in df.iterrows():
            # Adjust column names based on actual output
            ticker = str(row.get('symbol', '')).strip().upper()
            # Use icb_name as the main sector
            industry = str(row.get('icb_name', '')).strip()
            # Get company name
            name = str(row.get('organ_name', '')).strip()

            # Basic cleaning
            if ticker and industry and industry.lower() != 'nan':
                # Save as object with name and sector
                sector_map[ticker] = {
                    "sector": industry,
                    "name": name
                }
        
        # Save to JSON file
        with open(SECTOR_FILE, 'w', encoding='utf-8') as f:
            json.dump(sector_map, f, ensure_ascii=False, indent=2)
            
        print(f"✅ Sector info updated. Saved {len(sector_map)} tickers to {SECTOR_FILE}.")
        
    except Exception as e:
        print(f"❌ Error updating sector info: {e}")

if __name__ == "__main__":
    update_sector_info()
