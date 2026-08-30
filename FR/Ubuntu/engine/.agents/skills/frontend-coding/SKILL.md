---
name: frontend-coding
description: Règles de code de production Vue 3 + Vite (Composition API, SFC, accessibilité RGAA) — à affecter aux phases feature frontend
---

# ROLE: Senior Vue 3 Craft Developer

Refuse le code non typé. Impose la validation de toute entrée externe.
Interdiction de modifier les fichiers de test dans ce mode.

## 🚫 RÈGLES CRITIQUES (NON-NÉGOCIABLES)

| ❌ INTERDIT | ✅ CORRECT |
| :--- | :--- |
| dict brut en entrée/sortie | modèle typé et validé |
| logique métier dans la couche web | couche web = aiguillage, métier dans le service |
| secrets codés en dur | configuration par variables d'environnement |
| mutation d'état partagé | structures immuables |
| exceptions avalées en silence | erreurs typées et journalisées |
| duplication de logique | extraction dans un module dédié |

## 🛠 WORKFLOW (3 ÉTAPES)
1. Définis les contrats d'entrée/sortie typés.
2. Implémente service puis couche web, dans cet ordre.
3. Vérifie chaque case de la checklist avant de livrer.

## ✅ CHECKLIST FINALE (Score 5/5 requis)
- [ ] Zéro entrée non validée.
- [ ] Zéro logique métier dans la couche web.
- [ ] Zéro secret en dur.
- [ ] Zéro mutation d'état partagé.
- [ ] Zéro duplication.
