# Spécification technique — Serveur MCP "Coach Triathlon Garmin"

**Destinataire :** Claude Code
**Objectif :** développer un serveur MCP distant (remote MCP server) exposant des données Garmin et un état opérationnel (plan de semaine, séance du jour, feedback) à un Project Claude.ai personnel de coaching triathlon.

---

## 1. Vue d'ensemble

Construire un serveur MCP en Python qui :
1. Se connecte à Garmin Connect via la librairie `garminconnect` pour lire des données physiologiques et d'activité.
2. Expose ces données comme des **tools MCP** appelables par Claude.
3. Maintient un état opérationnel simple (plan de semaine, séance du jour, log de feedback) dans une base SQLite locale, également exposé via des tools MCP (lecture + écriture).
4. Tourne en HTTPS, accessible publiquement (le serveur sera hébergé sur une VM Oracle Cloud — cf. guide séparé).
5. Est protégé par un segment secret dans l'URL (pas d'OAuth — usage mono-utilisateur, cf. section 5).

Ce serveur est **le seul composant à développer** dans tout le projet de coach IA. Tout le reste (interface, agents, calendrier) utilise des fonctionnalités natives de claude.ai.

---

## 2. Stack technique imposée

- **Langage :** Python 3.11+
- **SDK MCP :** SDK officiel Python `mcp` (https://github.com/modelcontextprotocol/python-sdk), transport **Streamable HTTP** (recommandé par le SDK pour un serveur distant, remplace SSE)
- **Librairie Garmin :** `garminconnect` (PyPI) — https://github.com/cyberjunky/python-garminconnect
- **Base de données :** SQLite (fichier local sur le serveur, pas de service DB séparé)
- **Serveur ASGI :** `uvicorn` pour exposer l'app MCP
- **Gestion des dépendances :** `pyproject.toml` + environnement virtuel (`venv` ou `uv`)
- **Reverse proxy HTTPS :** géré en dehors du code Python (Caddy sur la VM — cf. guide d'hébergement), le serveur Python écoute en HTTP simple sur un port local (ex. `127.0.0.1:8080`)

---

## 3. Arborescence du repo attendue

```
mcp-garmin-coach/
├── pyproject.toml
├── .env.example
├── README.md
├── src/
│   ├── server.py              # point d'entrée, création de l'app MCP, montage des tools
│   ├── auth.py                 # vérification du segment secret dans l'URL
│   ├── garmin_client.py        # wrapper autour de garminconnect (login, cache de session, méthodes get_*)
│   ├── db.py                   # connexion SQLite, migrations/création des tables au démarrage
│   ├── tools/
│   │   ├── garmin_tools.py     # garmin_sync, garmin_get_training_status, etc.
│   │   └── operational_tools.py # get/set_week_plan, get/set_today_session, log_activity_feedback
│   └── models.py               # dataclasses / pydantic models pour les payloads
├── tests/
│   ├── test_garmin_tools.py    # tests avec garminconnect mocké
│   ├── test_operational_tools.py
│   └── test_auth.py
└── deploy/
    ├── coach-mcp.service        # unit systemd
    └── Caddyfile.example        # exemple de config Caddy (référence, la vraie config vit sur la VM)
```

---

## 4. Variables d'environnement (`.env`)

```
GARMIN_EMAIL=...
GARMIN_PASSWORD=...
MCP_SECRET_PATH=<segment aléatoire généré une fois, ex. 64 caractères hex>
DB_PATH=./data/coach.db
LOG_LEVEL=INFO
PORT=8080
```

`.env.example` doit être committé (sans les vraies valeurs) ; `.env` doit être dans `.gitignore`.

Génération recommandée du secret : `python -c "import secrets; print(secrets.token_hex(32))"`

---

## 5. Authentification du connecteur

- Le serveur MCP est monté sous le chemin `/mcp/{MCP_SECRET_PATH}` (ex. `/mcp/8f3e1c9a...`).
- Toute requête sur un chemin ne correspondant pas exactement au secret configuré renvoie **404** (pas 401/403, pour ne pas révéler que le chemin existe).
- Pas d'implémentation OAuth : compromis assumé pour un usage personnel mono-utilisateur (cf. spec produit v6 — section auth).
- Le secret ne doit jamais apparaître dans les logs applicatifs (attention au logging des requêtes HTTP : masquer le path dans les logs, ou logger uniquement la méthode + code retour).

---

## 5bis. Fuseau horaire

Toutes les dates/heures manipulées par le serveur (DB, calculs de créneaux, `garmin_sync`) doivent être traitées en **Europe/Paris**, pas en UTC serveur par défaut — les VM cloud tournent généralement en UTC. À fixer explicitement dans la config de l'app (ex. variable d'environnement `TZ=Europe/Paris` sur le service systemd, ou conversion explicite dans le code) pour éviter un décalage entre "aujourd'hui" côté serveur et "aujourd'hui" côté utilisateur.

---

## 6. Schéma SQLite (`db.py` doit créer ces tables si absentes au démarrage)

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  creneau TEXT NOT NULL,          -- 'matin' | 'soir'
  discipline TEXT NOT NULL,
  objectif TEXT NOT NULL,
  description TEXT NOT NULL,
  duree_minutes INTEGER,
  distance_km REAL,
  nutrition_avant TEXT,
  nutrition_apres TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);

CREATE TABLE IF NOT EXISTS activity_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  discipline TEXT NOT NULL,
  ressenti TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS garmin_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
```

`garmin_cache` sert à éviter de sur-solliciter l'API Garmin : les tools `garmin_get_*` doivent d'abord vérifier si une donnée fraîche (< 1h) existe en cache avant d'appeler `garminconnect`, sauf pour `garmin_sync` qui force toujours un appel réel.

---

## 7. Tools MCP à implémenter

Pour chaque tool ci-dessous : nom exact, schéma d'input JSON, schéma d'output JSON, comportement attendu.

**Note pour l'implémentation :** `garminconnect` expose 130+ méthodes et évolue régulièrement ; les noms exacts des méthodes internes à utiliser pour la charge d'entraînement détaillée, le seuil lactique et les minutes d'intensité (7.8-7.10) doivent être vérifiés dans le code source/README de la version installée au moment du développement plutôt que supposés à l'avance.

### 7.1 `garmin_sync`
- Input : `{}`
- Comportement : appelle `garminconnect` pour rafraîchir toutes les données du jour (training status, body battery, sommeil, FC repos, activités récentes, charge d'entraînement détaillée, seuil lactique, minutes d'intensité), écrit dans `garmin_cache`.
- Output : `{ "status": "ok", "synced_at": "<ISO8601 UTC>" }`
- Erreur (ex. login Garmin échoué) : `{ "error": true, "message": "Impossible de se connecter à Garmin Connect: <détail>" }`

### 7.2 `garmin_get_training_status`
- Input : `{}`
- Output : `{ "date": "YYYY-MM-DD", "status": "<str>", "acute_load": <int>, "chronic_load": <int> }`

### 7.3 `garmin_get_body_battery`
- Input : `{ "date": "YYYY-MM-DD" }` (optionnel, défaut aujourd'hui)
- Output : `{ "date": "YYYY-MM-DD", "value": <int 0-100>, "charged": <int>, "drained": <int> }`

### 7.4 `garmin_get_vo2max`
- Input : `{}`
- Output : `{ "date": "YYYY-MM-DD", "running_vo2max": <int|null>, "cycling_vo2max": <int|null> }`

### 7.5 `garmin_get_resting_hr`
- Input : `{ "date": "YYYY-MM-DD" }` (optionnel)
- Output : `{ "date": "YYYY-MM-DD", "resting_hr": <int> }`

### 7.6 `garmin_get_sleep`
- Input : `{ "date": "YYYY-MM-DD" }` (optionnel)
- Output : `{ "date": "YYYY-MM-DD", "duration_minutes": <int>, "deep_minutes": <int>, "light_minutes": <int>, "rem_minutes": <int>, "awake_minutes": <int>, "score": <int|null> }`

### 7.7 `garmin_get_recent_activities`
- Input : `{ "days": <int> }` (optionnel, défaut 7)
- Output : `{ "activities": [ { "date": "YYYY-MM-DD", "type": "<str>", "duration_minutes": <int>, "distance_km": <float|null>, "avg_hr": <int|null>, "max_hr": <int|null> } ] }`
- **Correspondance des types :** Garmin retourne ses propres libellés d'activité (ex. `running`, `cycling`, `lap_swimming`, `open_water_swimming`, `strength_training`). Le champ `type` en sortie doit être normalisé vers le vocabulaire du système (`course`, `velo`, `nage`, `renfo`) via une table de correspondance à maintenir dans `garmin_client.py`, pour que ce champ s'aligne avec `discipline` utilisé partout ailleurs (sessions, feedback). Documenter les types Garmin non reconnus en les faisant passer tels quels plutôt qu'en échouant.

### 7.8 `garmin_get_training_load`
- Input : `{}`
- Output : `{ "date": "YYYY-MM-DD", "acute_load": <int>, "chronic_load": <int>, "load_ratio": <float>, "load_status": "optimal|high|low|no_status" }`
- Comportement : charge d'entraînement détaillée (au-delà du statut global déjà donné par `garmin_get_training_status`) — `load_ratio` = charge aiguë (7j) / charge chronique (28j), `load_status` interprète ce ratio (ex. ratio > 1.5 → "high", < 0.8 → "low"). Sert à la Skill Planification pour détecter un risque de surcharge avant même les signaux de fatigue subjective.

### 7.9 `garmin_get_lactate_threshold`
- Input : `{}`
- Output : `{ "date": "YYYY-MM-DD", "running": { "heart_rate": <int|null>, "pace_min_per_km": <float|null> }, "cycling": { "ftp_watts": <int|null> } }`
- Comportement : lit le seuil lactique course (FC + allure) et la puissance seuil vélo (FTP) si disponibles. **Note :** ces valeurs dépendent de la détection automatique par la montre (nécessite un modèle compatible et des séances récentes à l'intensité seuil) — peuvent être `null` si jamais détectées. Utile pour calibrer les zones d'intensité des séances seuil/VMA plutôt que de se baser sur des zones FC génériques.

### 7.10 `garmin_get_intensity_minutes`
- Input : `{ "week_start_date": "YYYY-MM-DD" }` (optionnel, défaut = semaine courante)
- Output : `{ "week_start_date": "YYYY-MM-DD", "moderate_minutes": <int>, "vigorous_minutes": <int>, "goal_minutes": <int> }`
- Comportement : minutes d'intensité modérée/vigoureuse cumulées sur la semaine vs objectif hebdomadaire Garmin — signal complémentaire de charge globale, utile pour la révision hebdomadaire du plan.

### 7.11 `get_week_plan`
- Input : `{ "week_start_date": "YYYY-MM-DD" }` (optionnel, défaut = lundi de la semaine courante)
- Comportement : agrège les `sessions` des 7 jours de la semaine (aucune table séparée — le plan de semaine est une vue calculée sur `sessions`)
- Output : `{ "week_start_date": "YYYY-MM-DD", "days": [ { "date": "YYYY-MM-DD", "sessions": [ { "id": <int>, "creneau": "matin|soir", "discipline": "<str>", "objectif": "<str>", "description": "<str>", "duree_minutes": <int|null>, "distance_km": <float|null>, "nutrition_avant": "<str|null>", "nutrition_apres": "<str|null>" } ] } ] }`
- Un jour sans séance a `"sessions": []`

### 7.12 `set_week_plan`
- Input : `{ "week_start_date": "YYYY-MM-DD", "days": [ { "date": "...", "sessions": [ {"creneau": "...", "discipline": "...", "objectif": "...", "description": "...", "duree_minutes": ..., "distance_km": ..., "nutrition_avant": ..., "nutrition_apres": ...} ] } ] }`
- Output : `{ "status": "ok" }`
- Comportement : pour chaque date fournie, remplace intégralement les séances existantes de cette date par la nouvelle liste (delete + insert) — équivalent à appeler `set_sessions` pour chaque jour de la semaine

### 7.13 `get_sessions`
- Input : `{ "date": "YYYY-MM-DD" }` (optionnel, défaut aujourd'hui)
- Output : `{ "date": "YYYY-MM-DD", "sessions": [ { "id": <int>, "creneau": "matin|soir", "discipline": "<str>", "objectif": "<str>", "description": "<str>", "duree_minutes": <int|null>, "distance_km": <float|null>, "nutrition_avant": "<str|null>", "nutrition_apres": "<str|null>" } ] }`
- Aucune séance : `{ "date": "YYYY-MM-DD", "sessions": [] }`

### 7.14 `set_sessions`
- Input : `{ "date": "YYYY-MM-DD", "sessions": [ {"creneau": "...", "discipline": "...", "objectif": "...", "description": "...", "duree_minutes": ..., "distance_km": ..., "nutrition_avant": ..., "nutrition_apres": ...} ] }`
- Output : `{ "status": "ok", "count": <int> }`
- Comportement : remplace intégralement les séances existantes pour cette date (delete + insert). C'est ce qu'appelle la tâche programmée du matin.

### 7.15 `add_session`
- Input : `{ "date": "YYYY-MM-DD", "creneau": "matin|soir", "discipline": "<str>", "objectif": "<str>", "description": "<str>", "duree_minutes": <int|null>, "distance_km": <float|null>, "nutrition_avant": "<str|null>", "nutrition_apres": "<str|null>" }`
- Output : `{ "status": "ok", "id": <int> }`
- Comportement : ajoute une séance sans toucher aux autres séances déjà présentes ce jour-là — utile pour un ajustement ponctuel en cours de journée (ex. ajout d'une séance de récupération le soir) sans écraser ce qui existe déjà le matin

### 7.16 `delete_session`
- Input : `{ "id": <int> }`
- Output : `{ "status": "ok" }`

### 7.17 `log_activity_feedback`
- Input : `{ "date": "YYYY-MM-DD", "discipline": "<str>", "ressenti": "<str>", "note": "<str|null>" }`
- Output : `{ "status": "ok" }`
- Comportement : insertion simple (historique conservé, pas d'upsert)

---

## 8. Gestion des erreurs (règle générale)

- Aucune exception non gérée ne doit remonter au client MCP.
- Toute erreur (échec Garmin, DB indisponible, entrée invalide) doit être capturée et renvoyée sous la forme `{ "error": true, "message": "<message clair et actionnable>" }`.
- Les erreurs de login Garmin doivent être explicites (distinguer "identifiants invalides" de "Garmin injoignable" si possible, pour que l'agent Claude sache quoi dire à l'utilisateur).

---

## 9. Logging

- Logger chaque appel de tool (nom du tool, succès/échec, durée) en `INFO`.
- Ne jamais logger : mot de passe Garmin, contenu du token de session Garmin, le segment secret de l'URL.
- Format de log simple, lisible via `journalctl` (le serveur tournera comme service systemd).

---

## 10. Tests attendus

- Tests unitaires sur `tools/garmin_tools.py` avec `garminconnect` mocké (ne pas appeler le vrai compte Garmin dans les tests).
- Tests unitaires sur `tools/operational_tools.py` avec une DB SQLite temporaire (fixture pytest, fichier en mémoire ou tmp_path).
- Test sur `auth.py` : vérifier qu'une requête sans le bon segment secret renvoie bien 404.
- Pas besoin de tests d'intégration contre le vrai Garmin Connect (fragile, dépend d'un compte réel) — à faire manuellement une fois déployé.

---

## 11. Definition of Done

- [ ] Le serveur démarre localement (`uvicorn src.server:app`) et répond sur `/mcp/{secret}`
- [ ] Les 17 tools sont implémentés conformément aux schémas ci-dessus
- [ ] Les tests unitaires passent
- [ ] Le `README.md` explique : comment configurer `.env`, comment lancer en local, comment lancer les tests
- [ ] Le fichier `deploy/coach-mcp.service` est prêt à être copié sur la VM (cf. guide d'hébergement séparé)
- [ ] Aucune donnée sensible (mot de passe, secret) n'est committée dans le repo

---

## 12. Hors périmètre (ne pas développer)

- Pas d'interface web/frontend
- Pas d'orchestrateur multi-agents (géré nativement par claude.ai via Project + Skills)
- Pas de gestion des objectifs de course (gérée par le calendrier Apple natif)
- Pas d'authentification OAuth complète
