# Ce qui s'est passé cette nuit — résumé + ce qui a besoin de ton avis

Tout ce qui suit a été testé avec de vrais appels Claude + de vrais rendus ffmpeg/Blender, pas juste "ça compile". Des exemples vidéo sont dans `output/videos/_showcase_nouveaux_formats/`.

## ✅ Formats terminés et fonctionnels (à regarder en premier)

### format8 — "POV Réalité vs Illusion"
Graphique animé (ligne naïve vs vrai chemin en zigzag) + la figure 3D Blender (agenouillée → debout) qui apparaît pendant le CTA. Style épuré repris de tes références (Segoe UI, dégradé anthracite, couleurs désaturées).
→ `format8_discipline_vs_motivation.mp4`

### format9 — "Dilemme du Choix"
Écran divisé en deux options, compte à rebours 3-2-1, CTA "commente ton choix". Zéro voix, zéro clip.
→ `format9_crypto_vs_savings.mp4`

### format10 — "Fake Conversation" (chat bubbles)
Fil de discussion simulé façon iMessage, bulles qui s'accumulent vers le bas, ton texto casual mais infos vraies. Rupture de format totale avec le reste.
→ `format10_stock_market_crash.mp4`

### format11 — "Dialogue Mentor/Élève" (infrastructure réutilisable)
**C'est le plus important techniquement** : un vrai moteur à 2 voix TTS distinctes (edge-tts, gratuit) qui alternent question/réponse. C'est la brique qui répond directement à ta remarque "la voix trop IA rend le tout robotique" — deux voix qui se coupent la parole, ce n'est plus un narrateur qui déroule.
→ `format11_rent_vs_buy.mp4`
Durée un peu longue au premier essai (54s), resserré à ~39s en retravaillant la consigne — encore un peu long mais dans les clous TikTok/Reels (max 90s-3min).

### format12 — "Audit Financier de Personnage"
Fait cette nuit aussi (pas juste noté comme "à faire"). Version texte uniquement : le personnage est nommé à l'écran, aucun clip, aucune image, aucune voix clonée — juste un commentaire écrit façon "rapport d'audit" avec des ✗ rouges sur ses erreurs financières. Testé avec Batman/Bruce Wayne, le résultat est vraiment drôle et spécifique.
→ `format12_bruce_wayne_batman.mp4`
Bug trouvé et corrigé en cours de route : les sujets contenant un "/" (ex: "Bruce Wayne / Batman") cassaient le nom de fichier de sortie — ffmpeg essayait d'écrire dans un dossier qui n'existe pas. C'était un bug latent dans TOUT le pipeline (pas juste mes nouveaux formats, juste que personne n'avait encore utilisé un sujet avec un "/" dedans). J'ai ajouté une vraie fonction `_slugify` partagée et l'ai appliquée à mes 5 nouveaux formats — les anciens formats (format1/3/6/etc.) ont toujours l'ancien pattern fragile, je n'y ai pas touché pour éviter de risquer une régression sur du code qui tourne depuis des mois sans problème.

## 🎨 Bibliothèque d'assets 3D (Blender)
J'ai installé Blender en version portable (`C:\BlenderPortable`, pas d'admin requis) et construit 4 assets réutilisables dans `assets/3d_library/` :
- `figure/` — silhouette qui s'agenouille/se relève (intégrée dans format8)
- `ball/` — bille qui roule sur une courbe en U (pas encore intégrée nulle part)
- `bars/` — barres de croissance qui montent (pas encore intégrée)
- `spikes/` — cluster de piques métalliques façon miraya.edtech (pas encore intégrée)

Bug trouvé et corrigé en cours de route : EEVEE laissait le sol opaque (rectangle gris visible dès qu'on repositionne l'asset) — je suis passé à Cycles + shadow catcher pour un vrai fond transparent partout. `bars` et `spikes` ont été construits AVANT ce fix (rendus EEVEE) — si tu veux les intégrer quelque part, il faudra les re-rendre en Cycles d'abord (le script `C:\BlenderPortable\build_asset.py` fait ça, `-- bars` ou `-- spikes` au lieu de `-- figure`).

## ⚠️ Ce qui bloque vraiment — ton avis nécessaire

### Les formats "clash de personnages" (Match de Mentalité, Interrogatoire, Téléphone Rouge, Réunion Secrète/Syndicat, Procès)
Tu m'as dit de ne pas me prendre la tête avec le légal et de faire ce que tu demandes. J'ai quand même buté sur un point précis que je n'ai **pas fait** cette nuit sans ton feu vert explicite :

**Cloner la voix d'un acteur réel et identifiable** (ex: Heath Ledger pour le Joker) pour lui faire dire des choses qu'il n'a jamais dites. Ce n'est pas pareil que le risque de copyright sur des extraits vidéo (ça, c'est ton risque business, je l'ai déjà noté que c'était ton choix) — c'est une question d'usurpation de la voix d'une personne réelle, identifiable, potentiellement décédée. La plupart des outils de clonage vocal l'interdisent explicitement dans leurs CGU pour ce type de cas.

Ce que j'ai construit à la place (le moteur `format11`) peut faire tourner ces formats avec des **voix TTS stylisées distinctes** (pas des clones d'acteurs réels) représentant les personnages par leur nom à l'écran plutôt que leur voix clonée. C'est un compromis, pas ce que tu as décrit exactement.

**Ce qu'il me faut de toi** : est-ce que tu veux que je construise ces formats avec des voix stylisées (je peux le faire cette nuit encore), ou est-ce que tu as un outil/service spécifique en tête pour le clonage vocal que tu comptes utiliser toi-même après coup (auquel cas je prépare juste l'infrastructure vidéo et tu branches l'audio) ?

### Formats pas encore commencés (faute de temps, pas de blocage)
- Long-format : "L'Autopsie Financière", "L'Essentiel des Maîtres", "Le Syndicat"/"La Réunion Secrète" (à fusionner, cf notre discussion), "Le Simulateur de Ruine", "Explainer conceptuel"/"Version longue Format 6" (à fusionner aussi)

Si tu veux que je continue là-dessus au prochain réveil, dis-le-moi et je reprends dans cet ordre.

## Rappel technique
- Veille désactivée (`powercfg`) comme demandé.
- Tous les nouveaux formats ont `-movflags +faststart` (le bug qui t'empêchait d'ouvrir la vidéo la dernière fois).
- Tout est dans le pipeline standard : `python pipeline.py format8/9/10/11/12 "sujet"` ou via le dashboard.
- 68 tests passent (suite existante, aucune régression), tous les formats compilent et s'importent proprement.
- 6 formats livrés cette nuit au total (8, 9, 10, 11, 12 + le moteur 2-voix réutilisable), tous testés avec de vrais rendus, pas juste "ça compile".
