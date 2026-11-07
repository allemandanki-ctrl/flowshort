import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.claude_cli import call_claude


def test_haiku_generation(subject):
    # Prompt représentatif inspiré de ton pipeline actuel
    prompt = (
        f"Tu es un expert en création de contenu viral pour les réseaux sociaux. "
        f"Génère un script pour un short sur le sujet : '{subject}'.\n"
        "Structure :\n"
        "1. Un Hook ultra-accrocheur (2 secondes) pour retenir l'attention.\n"
        "2. Le corps du script (concis, percutant).\n"
        "3. Un appel à l'action (CTA) final.\n\n"
        "Réponds en français."
    )

    text, _, _ = call_claude("", prompt, model="haiku")
    return text

if __name__ == "__main__":
    print(test_haiku_generation("Comment investir en bourse avec 100 euros"))
