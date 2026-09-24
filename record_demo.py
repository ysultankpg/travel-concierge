import asyncio
import os
import time
from pathlib import Path
from playwright.async_api import async_playwright

FRONTEND_URL = "https://travel-concierge-frontend-732872429781.us-east1.run.app"
OUTPUT_DIR = Path("/config/.gemini/antigravity/brain/f05b57a6-bacd-4615-b990-135144c95f33")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            record_video_dir=str(OUTPUT_DIR),
            record_video_size={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        print(f"Navigating to {FRONTEND_URL}...")
        await page.goto(FRONTEND_URL, wait_until="networkidle")
        await asyncio.sleep(2)

        # First prompt: What the app does best
        prompt1 = "Find cultural destinations under $250 daily budget and get details for Kyoto."
        print(f"Sending Prompt 1: {prompt1}")
        await page.fill("#input", prompt1)
        await asyncio.sleep(1)
        await page.click("#form button[type='submit']")

        # Wait for agent response
        print("Waiting for response to Prompt 1...")
        await page.wait_for_selector(".msg.agent", timeout=60000)
        await asyncio.sleep(6)  # Pause to show response clearly in demo video

        # Second prompt: Rich prompt showing tool call, database lookup, generated image, and booking
        prompt2 = "Generate a travel photo for Kyoto pagoda, geocode Kyoto Station, and save a trip bookmark for user_123 for Kyoto in October 2026."
        print(f"Sending Prompt 2: {prompt2}")
        await page.fill("#input", prompt2)
        await asyncio.sleep(1)
        await page.click("#form button[type='submit']")

        print("Waiting for response to Prompt 2...")
        # Wait until two agent responses appear
        await page.wait_for_function("document.querySelectorAll('.msg.agent').length >= 2", timeout=90000)
        await asyncio.sleep(8)  # Pause to display generated image/results in demo

        # Get recorded video path
        video_path = await page.video.path()
        print(f"Video recorded to: {video_path}")

        await context.close()
        await browser.close()

        # Rename/copy video to standard name
        target_path = OUTPUT_DIR / "agent_demo.webm"
        if os.path.exists(video_path):
            os.replace(video_path, target_path)
            print(f"Demo video saved as artifact: {target_path}")

if __name__ == "__main__":
    asyncio.run(main())
