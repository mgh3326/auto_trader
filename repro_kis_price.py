import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from app.services.kis import KISClient
from app.core.config import settings

# Configure logging
logging.basicConfig(level=logging.INFO)

async def main():
    kis = KISClient()
    symbol = "CONY"
    
    print(f"Fetching price for {symbol}...")
    try:
        # Call the low-level inquire method to get the DataFrame
        df = await kis.inquire_overseas_price(symbol)
        print("\nDataFrame result:")
        print(df)
        
        if not df.empty:
            print(f"\nClose price: {df.iloc[0]['close']}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
