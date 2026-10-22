import asyncio
import json
import re
from playwright.async_api import async_playwright

async def get_altindex_toplist():
  async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto("https://altindex.com/toplist")

    # Wait for the stock listing table to load
    await page.wait_for_selector("table")

    # Extract row elements representing stocks
    rows = await page.locator("table tbody tr").all()

    stocks_list = []
    for row in rows:
      cells = await row.locator("td").all_text_contents()
      if len(cells) >= 4:
        # Clean cell inputs
        raw_company_info = cells[1].strip().replace('\n', ' ')
        raw_price_info = cells[2].strip().replace('\n', ' ')
        ai_score = cells[3].strip()

        # Parse Company and Ticker (e.g., "Agnico Eagle MinesAEM" -> "Agnico Eagle Mines", "AEM")
        ticker_match = re.search(r'([A-Z]+)$', raw_company_info)
        if ticker_match:
            ticker = ticker_match.group(1)
            company_name = raw_company_info[:-len(ticker)].strip()
        else:
            company_name = raw_company_info
            ticker = ""

        # Parse Price and Change (e.g., "$203.553.1%" -> "$203.55", "3.1%")
        # Uses regex to isolate decimal dollar amount (up to 2 decimal places) and any trailing percentage
        price_match = re.search(r'^(\$[0-9,]+\.[0-9]{2})(.*)$', raw_price_info)
        if price_match:
            price = price_match.group(1)
            change = price_match.group(2).strip()
        else:
            # Fallback for stocks below $1 or whole numbers
            price_match_fallback = re.search(r'^(\$[0-9.,]+?)([-+]?[0-9.]+\%)$', raw_price_info)
            if price_match_fallback:
                price = price_match_fallback.group(1)
                change = price_match_fallback.group(2)
            else:
                price = raw_price_info
                change = ""

        stocks_list.append({
            "company_name": company_name,
            "ticker": ticker,
            "price": price,
            "change": change,
            "ai_score": int(ai_score) if ai_score.isdigit() else ai_score
        })

    await browser.close()
    return json.dumps(stocks_list, indent=2)

# Run the function and print the JSON output
json_output = asyncio.run(get_altindex_toplist())

with open("top_altindex.json", "w") as f:
    f.write(json_output)