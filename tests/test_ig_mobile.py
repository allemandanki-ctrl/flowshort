"""
Test de faisabilité : émulation mobile Instagram pour débloquer le bouton musique.

Usage:
  python tests/test_ig_mobile.py

Le navigateur s'ouvre en mode mobile (iPhone 13).
Connecte-toi à Instagram manuellement si demandé, puis appuie sur ENTER ici.
Les screenshots sont sauvegardés dans logs/mobile_test_*.png
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import BASE_DIR

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

IPHONE_13 = {
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "viewport": {"width": 390, "height": 844},
    "is_mobile": True,
    "has_touch": True,
    "device_scale_factor": 3,
}


async def main() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(**IPHONE_13)
        page = await ctx.new_page()

        print("\n>>> Ouverture Instagram mobile...")
        await page.goto("https://www.instagram.com", wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(LOGS_DIR / "mobile_test_01_home.png"))
        print(">>> Screenshot 01 sauvegardé : mobile_test_01_home.png")

        print("\n>>> Si tu n'es pas connecté, connecte-toi maintenant dans la fenêtre.")
        print(">>> Appuie sur ENTER quand tu es sur le feed Instagram.\n")
        input()

        await page.screenshot(path=str(LOGS_DIR / "mobile_test_02_logged_in.png"))
        print(">>> Screenshot 02 sauvegardé : mobile_test_02_logged_in.png")

        # Chercher le bouton "+" ou "Create" pour créer un Reel
        print("\n>>> Recherche du bouton Create / + ...")
        create_selectors = [
            "[aria-label='New post']",
            "[aria-label='Nouveau post']",
            "svg[aria-label='New post']",
            "a[href='/create/style/']",
        ]
        clicked = False
        for sel in create_selectors:
            try:
                el = page.locator(sel).first
                await el.wait_for(state="visible", timeout=4000)
                await el.click()
                print(f">>> Create cliqué via {sel!r}")
                clicked = True
                await page.wait_for_timeout(2000)
                break
            except Exception:
                continue

        if not clicked:
            print(">>> Bouton Create non trouvé automatiquement.")
            print(">>> Clique manuellement sur le bouton '+' ou 'Créer', puis appuie sur ENTER.")
            input()

        await page.screenshot(path=str(LOGS_DIR / "mobile_test_03_after_create.png"))
        print(">>> Screenshot 03 sauvegardé : mobile_test_03_after_create.png")

        print("\n>>> Si un menu est apparu, clique sur 'Reel' ou 'Post', puis ENTER.")
        input()

        await page.screenshot(path=str(LOGS_DIR / "mobile_test_04_create_menu.png"))
        print(">>> Screenshot 04 sauvegardé : mobile_test_04_create_menu.png")

        print("\n>>> Maintenant navigue jusqu'à l'écran d'édition du Reel (après avoir")
        print("    sélectionné une vidéo ou passé l'écran de sélection). Appuie sur ENTER.")
        input()

        await page.screenshot(path=str(LOGS_DIR / "mobile_test_05_edit_screen.png"))
        print(">>> Screenshot 05 sauvegardé : mobile_test_05_edit_screen.png")

        # Vérifier si un bouton musique est présent
        music_selectors = [
            "[aria-label*='Music']",
            "[aria-label*='Musique']",
            "[aria-label*='Audio']",
            "button:has-text('Music')",
            "button:has-text('Musique')",
            "button:has-text('Audio')",
            "[class*='music']",
        ]
        found = []
        for sel in music_selectors:
            count = await page.locator(sel).count()
            if count > 0:
                found.append(f"{sel!r} → {count} élément(s)")

        print("\n" + "="*60)
        if found:
            print("✅ BOUTON MUSIQUE TROUVÉ :")
            for f in found:
                print(f"   {f}")
        else:
            print("❌ AUCUN bouton musique détecté sur cet écran.")
            print("   L'émulation mobile ne débloque pas le bouton musique ici.")
        print("="*60)

        print("\n>>> Appuie sur ENTER pour fermer.")
        input()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
