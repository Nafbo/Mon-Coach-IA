# mcp-garmin-coach

Serveur MCP distant exposant des données Garmin Connect et un état opérationnel (plan de
semaine, séances du jour, feedback) à un Project claude.ai personnel de coaching triathlon.

Écrit en Python, SDK MCP officiel (transport **Streamable HTTP**), déployé derrière Caddy
(HTTPS) sur une VM Oracle Cloud, protégé par un segment secret dans l'URL (pas d'OAuth — usage
mono-utilisateur assumé).

Ce serveur est le seul composant applicatif du projet : le reste (agents, Skills, calendrier,
Notion) utilise des fonctionnalités natives de claude.ai. Le contexte de coaching (profil,
objectifs de course, contraintes d'emploi du temps) vit séparément dans
[`project-instructions.md`](project-instructions.md) — lu par les Skills/routines, à ne pas
fusionner ici.

## Sommaire

- [Stack technique](#stack-technique)
- [Arborescence](#arborescence-du-repo)
- [Configuration (`.env`)](#configuration-env)
- [Installation locale](#installation-locale)
- [Lancement en local](#lancement-en-local)
- [Tests](#tests)
- [Fuseau horaire](#fuseau-horaire)
- [Sécurité / authentification](#sécurité--authentification)
- [Schéma SQLite](#schéma-sqlite)
- [Tools MCP exposés](#tools-mcp-exposés)
- [Écarts volontaires par rapport à la spec initiale](#écarts-volontaires-par-rapport-à-la-spec-initiale)
- [Limites connues](#limites-connues)
- [Déploiement production](#déploiement-production)
- [Mise à jour en production](#mise-à-jour-en-production)

## Stack technique

- **Langage :** Python 3.11+ (dev local historiquement en 3.10 — cf. [Mise à jour en
  production](#mise-à-jour-en-production) pour les implications)
- **SDK MCP :** [`mcp`](https://github.com/modelcontextprotocol/python-sdk) officiel, transport
  Streamable HTTP
- **Librairie Garmin :** [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
- **Base de données :** SQLite, fichier local (pas de service DB séparé)
- **Serveur ASGI :** `uvicorn`
- **Reverse proxy HTTPS :** Caddy (hors du code Python — cf. `deploy/Caddyfile.example`), le
  serveur Python écoute en HTTP simple sur un port local (`127.0.0.1:8080` par défaut)

## Arborescence du repo

```
mcp-garmin-coach/
├── pyproject.toml
├── .env.example
├── README.md                    # ce fichier
├── project-instructions.md      # contexte coaching (profil, objectifs course) — lu par les Skills
├── inspect_new_endpoints.py     # script d'inspection ponctuelle d'un nouvel endpoint Garmin
├── src/
│   ├── server.py                 # point d'entrée, création de l'app MCP, montage des tools
│   ├── auth.py                   # segment secret dans l'URL, protection DNS-rebinding
│   ├── garmin_client.py          # wrapper autour de garminconnect (login, cache session, parsing)
│   ├── workout_builder.py        # construction de payloads de séances structurées (course/vélo)
│   ├── db.py                     # connexion SQLite, création des tables au démarrage
│   ├── tools/
│   │   ├── garmin_tools.py       # tools garmin_*
│   │   └── operational_tools.py  # plan de semaine, séances, feedback
│   └── models.py                 # modèles pydantic, utilitaires de fuseau horaire
├── tests/
└── deploy/
    ├── coach-mcp.service          # unit systemd (template — chemins réels différents, cf. plus bas)
    └── Caddyfile.example          # exemple de config Caddy (référence, vraie config sur la VM)
```

## Configuration (`.env`)

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `GARMIN_EMAIL` | Email du compte Garmin Connect |
| `GARMIN_PASSWORD` | Mot de passe du compte Garmin Connect |
| `MCP_SECRET_PATH` | Segment secret du chemin de montage (`/mcp/<secret>`) — générer avec la commande ci-dessous |
| `DB_PATH` | Chemin du fichier SQLite (défaut `./data/coach.db`) |
| `LOG_LEVEL` | Niveau de log (défaut `INFO`) |
| `PORT` | Port d'écoute HTTP local (défaut `8080`) |
| `PUBLIC_DOMAIN` | Domaine public derrière Caddy (ex. `coach-ia.duckdns.org`, sans schéma ni port). Optionnel en local ; **requis en production** (cf. [Sécurité](#sécurité--authentification)) |

Génération du secret :

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`.env` contient des identifiants réels : ne doit **jamais** être committé (déjà dans
`.gitignore`). Ne jamais non plus créer de copie sous un autre nom (`.env.save`, `.env.bak`...)
dans le dossier du repo — seul `.env` exact est ignoré, une copie mal nommée est à un
`git add -A` malencontreux de fuiter sur le repo public.

## Installation locale

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -e ".[dev]"
# macOS/Linux
.venv/bin/pip install -e ".[dev]"
```

## Lancement en local

Depuis la racine du repo :

```bash
uvicorn src.server:app --host 127.0.0.1 --port 8080 --no-access-log
```

`--no-access-log` est important : le log d'accès par défaut d'uvicorn loggue le chemin complet
de chaque requête (donc le secret). Le logging HTTP applicatif (méthode + code retour, sans le
chemin) est assuré par `AccessLogMiddleware` (`src/auth.py`).

Le serveur crée automatiquement les tables SQLite et le dossier `data/` si absents, puis répond
sur `http://127.0.0.1:8080/mcp/<MCP_SECRET_PATH>`. Toute autre URL renvoie 404.

Le premier appel à un tool `garmin_*` déclenche la connexion à Garmin Connect (identifiants +
MFA si activé) ; le token de session est ensuite mis en cache localement (dossier
`<dossier de DB_PATH>/garmin_tokens/`) pour éviter de se reconnecter à chaque appel.

## Tests

```bash
pytest
```

Les tests mockent entièrement `garminconnect` et utilisent une base SQLite temporaire (en
mémoire) : aucun test n'appelle le vrai compte Garmin. La correction (côté serveur applicatif)
est ainsi vérifiée sans dépendre d'un compte réel — mais ne garantit pas que la version de
`garminconnect` réellement installée expose l'API supposée : cf. [Mise à jour en
production](#mise-à-jour-en-production), incident `TargetType`.

## Fuseau horaire

Toutes les dates/heures générées côté serveur — dates métier (`today`, `week_start_date`, dates
par défaut des tools) **et** timestamps (`synced_at` de `garmin_sync`, `created_at` de
`sessions`/`activity_feedback`, `fetched_at` de `garmin_cache`) — sont en **Europe/Paris**,
indépendamment du fuseau du serveur. Point d'entrée unique : `now_paris()` / `today_paris()`
dans `src/models.py` — aucun `datetime.now()` naïf ni `datetime.now(timezone.utc)` ailleurs dans
le code. Seules les dates fournies en entrée par l'utilisateur (paramètre `date` des tools)
restent au format `YYYY-MM-DD` tel quel, sans conversion.

En production, `TZ=Europe/Paris` est aussi fixé au niveau du service systemd (`deploy/coach-mcp.service`)
pour que les logs et l'horloge système soient cohérents avec le code applicatif.

## Sécurité / authentification

- Le serveur MCP est monté sous `/mcp/{MCP_SECRET_PATH}`. Toute requête sur un chemin ne
  correspondant pas exactement au secret configuré renvoie **404** (jamais 401/403, pour ne pas
  révéler l'existence du endpoint) — comparaison en temps constant (`hmac.compare_digest`,
  `src/auth.py:SecretPathMiddleware`).
- Pas d'OAuth : compromis assumé pour un usage personnel mono-utilisateur.
- Le secret n'apparaît jamais dans les logs (`AccessLogMiddleware` ne logge que méthode + code
  retour, jamais le chemin).
- **`PUBLIC_DOMAIN` requis en production.** Le SDK `mcp` protège par défaut les endpoints
  Streamable HTTP contre le DNS rebinding en validant les en-têtes `Host`/`Origin`, et n'autorise
  que `localhost`/`127.0.0.1` si aucun domaine n'est configuré explicitement
  (`src/auth.py:build_transport_security`). Sans `PUBLIC_DOMAIN` renseigné une fois déployé
  derrière Caddy sur un vrai domaine, toute requête légitime reçoit **421 "Invalid Host
  header"**. `localhost`/`127.0.0.1` restent autorisés en parallèle, donc les tests locaux
  fonctionnent sans configurer cette variable.

## Schéma SQLite

Tables créées automatiquement par `db.py` au démarrage si absentes :

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
  ressenti TEXT NOT NULL,         -- stocke `smiley` (cf. log_activity_feedback)
  note TEXT,                      -- stocke `note` (int) converti en texte
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

`garmin_cache` évite de sur-solliciter l'API Garmin : les tools `garmin_get_*` vérifient d'abord
qu'une donnée fraîche (< 1h, `CACHE_FRESHNESS`) existe en cache avant d'appeler `garminconnect` —
sauf `garmin_sync` qui force toujours un appel réel, et `garmin_get_activity_weather`/
`garmin_get_activity_details` dont le cache n'expire jamais (la météo/les détails d'une activité
terminée sont immuables).

## Tools MCP exposés

22 tools au total. Schémas d'input/output exacts dans le code (`src/tools/garmin_tools.py`,
`src/tools/operational_tools.py`) — la liste ci-dessous donne le rôle de chacun.

### Garmin — données individuelles (cache 1h)

| Tool | Rôle |
|---|---|
| `garmin_sync` | Rafraîchit toutes les données du jour en une fois, écrit dans `garmin_cache` |
| `garmin_get_training_status` | Statut d'entraînement global + charge aiguë/chronique |
| `garmin_get_body_battery` | Body Battery (valeur, chargé/déchargé) |
| `garmin_get_vo2max` | VO2max course/vélo |
| `garmin_get_resting_hr` | Fréquence cardiaque au repos |
| `garmin_get_sleep` | Détail du sommeil (durée, phases, score) |
| `garmin_get_recent_activities` | Activités récentes normalisées (`course`/`velo`/`nage`/`renfo`) |
| `garmin_get_training_load` | Charge aiguë/chronique détaillée, ratio, statut, cibles ACWR |
| `garmin_get_training_load_balance` | Répartition mensuelle de charge (aérobie faible/élevée/anaérobique) |
| `garmin_get_lactate_threshold` | Seuil lactique course (FC + allure) et FTP vélo |
| `garmin_get_intensity_minutes` | Minutes d'intensité modérée/vigoureuse vs objectif hebdo |

### Garmin — activité individuelle (cache sans expiration)

| Tool | Rôle |
|---|---|
| `garmin_get_activity_weather` | Météo au moment d'une activité donnée (`activity_id`) |
| `garmin_get_activity_details` | Séries FC / allure / dénivelé alignées sur un axe temps commun |

### Garmin — séances structurées (écriture)

| Tool | Rôle |
|---|---|
| `garmin_push_workout` | Construit et programme une séance structurée (course/vélo) sur la montre. `dry_run=True` par défaut — ne fait jamais d'appel d'écriture réel sans `dry_run=False` explicite. Anti-doublon + retry à la programmation |
| `garmin_delete_workout` | Supprime un workout (nettoyage, ex. séance de test) |

### Opérationnel (plan de semaine, séances, feedback)

| Tool | Rôle |
|---|---|
| `get_week_plan` / `set_week_plan` | Lecture/écriture du plan de la semaine (vue calculée sur `sessions`) |
| `get_sessions` / `set_sessions` | Lecture/remplacement intégral des séances d'un jour |
| `add_session` | Ajoute une séance sans toucher aux autres séances du jour |
| `delete_session` | Supprime une séance par id |
| `log_activity_feedback` | Enregistre un feedback (`smiley` + `note` 0-10) |

## Écarts volontaires par rapport à la spec initiale

Plusieurs tools s'écartent du schéma initialement envisagé, validés en conditions réelles :

- **`garmin_sync`** : `synced_at` est en **Europe/Paris**, pas en UTC — cohérent avec la
  contrainte générale de fuseau horaire (cf. [Fuseau horaire](#fuseau-horaire)).
- **`garmin_get_recent_activities`** : chaque activité expose des champs supplémentaires.
  - **`activity_id`** (`null` si absent du payload Garmin) — ajouté après coup : sans ce
    champ, impossible d'enchaîner vers `garmin_get_activity_weather`/
    `garmin_get_activity_details` (qui exigent un `activity_id`) sans que l'utilisateur aille
    le chercher manuellement dans l'URL d'une activité sur connect.garmin.com. Trouvé en
    testant les nouveaux tools en conditions réelles.
  - Général : `garmin_type` (type brut Garmin, ex. `trail_running` vs `running`),
    `effet_entrainement` (`trainingEffectLabel`, ex. `TEMPO`, `LACTATE_THRESHOLD`,
    `AEROBIC_BASE`, `RECOVERY`), `effet_aerobie`/`effet_aerobie_message` et
    `effet_anaerobie`/`effet_anaerobie_message` (scores 0-5 Garmin + message qualitatif).
    `null` si absent.
  - Course (`type == "course"`) : `denivele_m`, `allure_moyenne_min_km`,
    `allure_rapide_min_km` (dérivée de `maxSpeed` ; pas d'allure "la plus lente" exposée par
    Garmin au niveau résumé). `null` pour les autres disciplines.
  - Nage (`type == "nage"`) : `piscine_longueur_m`, `nb_longueurs`, `swolf_moyen`,
    `cadence_moyenne_brasses_min`, `brasses_moyenne_longueur`, `brasses_total`,
    `meilleur_100m_sec`, `allure_moyenne_min_100m` (convention natation en min/100m). `null`
    pour les autres disciplines.
- **`log_activity_feedback`** : `ressenti: str` devient `smiley: str` (emoji, non vide) et
  `note` un entier obligatoire 0-10 (converti en texte pour la colonne SQLite existante).
- **`garmin_get_training_load`** : ajout de `cible_charge_chronique_min`/
  `cible_charge_chronique_max` et `acwr_pourcentage`/`acwr_statut` (calculés par Garmin
  lui-même, distincts de `load_status` calculé par ce serveur). `null` si absent.
- **`garmin_get_lactate_threshold`** : forme réelle corrigée après validation contre un vrai
  compte.
  - `speed_and_heart_rate.speed` est à une échelle 10x inférieure au m/s standard (brut
    `0.369` → ×10 → `3.69 m/s` → `4:31/km`).
  - `power` ne concerne qu'un seul sport à la fois (`RUNNING` ou `CYCLING`, jamais les deux) —
    routé vers `running` ou `cycling` selon le champ `sport`.
  - `running.power_watts`/`running.power_watts_per_kg` ajoutés (`functionalThresholdPower` et
    `powerToWeight` quand `sport == "RUNNING"`).
- **`garmin_get_training_load_balance`** : répartition mensuelle de charge ("Training Load
  Focus" Garmin), basé sur `mostRecentTrainingLoadBalance` — donnée nichée dans la même réponse
  que `get_training_status`.
- **Repli sur les derniers jours avec données** pour `garmin_get_training_status`,
  `garmin_get_training_load`, `garmin_get_training_load_balance` et `garmin_get_vo2max` :
  Garmin ne recalcule ces métriques qu'après une activité synchronisée, donc "aujourd'hui" est
  souvent vide. Ces tools remontent jusqu'à 7 jours en arrière et renvoient la **date réelle des
  données** (champ `date`) plutôt que de mélanger silencieusement une donnée ancienne avec la
  date du jour.
- **`garmin_get_activity_weather`** : `temp`/`apparentTemp`/`dewPoint` sont exposés par
  `garminconnect` en **°F**, pas en °C — repéré car `66` pour une soirée d'août pluvieuse à
  Paris serait absurde en Celsius mais cohérent en Fahrenheit (`18.9°C`). Convertis en Celsius
  (`temp_celsius`, `apparent_temp_celsius`, `dew_point_celsius`). `windSpeed`/`windGust`
  laissés bruts (`wind_speed_raw`, `wind_gust_raw`) — pas de valeur assez caractéristique pour
  trancher leur unité (mph vs km/h).
- **`garmin_get_activity_details`** : confirme le couple `metricDescriptors`/
  `activityDetailMetrics`, avec un piège non anticipé — **l'ordre de la liste
  `metricDescriptors` ne correspond pas à l'ordre réel des valeurs dans le tableau `metrics` de
  chaque point** ; seul le champ `metricsIndex` de chaque descripteur fait foi (ex.
  `directRunCadence` est listé en premier mais son `metricsIndex` réel est `4`). Le parsing
  construit une table `clé -> metricsIndex` avant d'indexer. `directSpeed` (vitesse brute) est
  absent sur l'activité utilisée pour valider ce parsing — repli sur `directGradeAdjustedSpeed`
  (vitesse ajustée au dénivelé), à surveiller si `directSpeed` s'avère présent sur d'autres
  types d'activité.
- **`garmin_push_workout`** : validé en écriture réelle (`dry_run=False`) — `upload_workout` et
  `schedule_workout` ont fonctionné du premier coup, `delete_workout` a bien nettoyé la séance
  de test ensuite. La forme réelle de `get_scheduled_workouts` est `{"calendarItems": [...]}`,
  chaque item ayant un champ `title` (pas `workoutName`) et `date`.
  - **Piège confirmé** : `calendarItems` mélange les activités déjà réalisées
    (`itemType: "activity"`) et les séances programmées — sans filtrage, une activité passée
    portant par coïncidence le même nom/date qu'une séance à programmer serait prise à tort
    pour un doublon. `_find_duplicate_scheduled_workout` (`src/tools/garmin_tools.py`) ignore
    désormais les entrées `itemType == "activity"`. Point non exercé : le vrai chemin "doublon
    détecté" n'a pas été déclenché en conditions réelles — son comportement exact reste une
    hypothèse raisonnable plutôt qu'une certitude.
  - Hypothèse non tranchée : la numérotation `stepOrder` (`src/workout_builder.py`) est
    **séquentielle globale** sur tout le workout, y compris à l'intérieur d'un bloc répété (pas
    de redémarrage à 1 par groupe). Le vrai push a été accepté par Garmin avec ce choix, mais
    rien ne garantit que ce soit la convention exacte attendue plutôt qu'une simple tolérance
    de l'API à l'upload.
  - **Piège trouvé au déploiement** : `garminconnect.workout.TargetType` a renommé ses
    attributs entre 0.3.2 (poste local, Python 3.10) et 0.3.9 (VM prod, Python 3.12+) —
    `POWER`→`POWER_ZONE`, `HEART_RATE`→`HEART_RATE_ZONE`, `SPEED`→`SPEED_ZONE` — un changement
    cassant dans une simple mise à jour de patch. Les identifiants numériques sous-jacents
    (`workoutTargetTypeId`), eux, sont stables entre les deux versions (ce sont ceux du
    protocole Garmin, pas un choix de `garminconnect`). `src/workout_builder.py` n'importe donc
    plus `TargetType` et utilise directement ces entiers. Détails de l'incident et procédure
    pour l'éviter à l'avenir : cf. [Mise à jour en production](#mise-à-jour-en-production).
- **`garmin_delete_workout`** : wrapper fin de `GarminClient.delete_workout`, ajouté pour le
  nettoyage de séances de test. Validé en conditions réelles (suppression confirmée via une
  relecture de `get_scheduled_workouts`).

## Limites connues

Les endpoints Garmin Connect utilisés pour la charge d'entraînement détaillée, le seuil
lactique, les minutes d'intensité, la météo et les détails d'activité reposent sur l'API privée
non documentée de Garmin Connect. Le parsing dans `src/garmin_client.py` est basé sur la
structure connue de cette API à la date d'écriture, mais Garmin peut la faire évoluer sans
préavis. Après un `garmin_sync` en conditions réelles, il est recommandé d'inspecter le contenu
de `garmin_cache` (table SQLite) pour confirmer que les champs sont bien peuplés, et d'ajuster le
parsing si nécessaire.

## Déploiement production

Le serveur tourne en systemd sur une VM Oracle Cloud, derrière Caddy (reverse proxy HTTPS).
`deploy/coach-mcp.service` et `deploy/Caddyfile.example` sont des **templates** — les chemins
réels sur la VM actuelle diffèrent (cf. tableau ci-dessous), gardés génériques dans le repo pour
rester réutilisables si l'hébergement change.

### Accès à la VM

- Connexion : `ssh -i ~/.ssh/coach-ia-oracle.key ubuntu@coach-ia.duckdns.org`
  - Utilisateur **`ubuntu`** (pas `coach`, malgré ce que suggère le nom de domaine).
  - Clé privée : `~/.ssh/coach-ia-oracle.key` sur la machine de dev (hors du repo — jamais
    committée, jamais dans le dossier du projet).
  - `sudo` fonctionne sans mot de passe pour `ubuntu`.
- **Si la connexion SSH time-out** (port 22 injoignable) : la Security List Oracle Cloud
  n'autorise le port 22 entrant que depuis une IP explicitement whitelistée, qui devient
  périmée si l'IP publique change. À corriger dans la console OCI :
  `cloud.oracle.com` → **Networking** → **Virtual Cloud Networks** → `coach-mcp-vcn` →
  **Security Lists** → **Default Security List for coach-mcp-vcn** → **Ingress Rules** →
  ajouter/mettre à jour la règle TCP port 22 avec l'IP publique actuelle (`/32`). Les ports 80
  et 443 sont ouverts à `0.0.0.0/0` (nécessaire pour Caddy/HTTPS public), seul le 22 est
  restreint par IP.

### Chemins réels sur la VM

| | |
|---|---|
| Repo | `/home/ubuntu/Mon-Coach-IA` |
| venv | `/home/ubuntu/Mon-Coach-IA/venv` (⚠️ pas `.venv` comme en local) |
| Service systemd | `coach-mcp.service` (`sudo systemctl {start,stop,restart,status} coach-mcp`) |
| `.env` réel | `/home/ubuntu/Mon-Coach-IA/.env` |
| Domaine public | `coach-ia.duckdns.org`, HTTPS géré par Caddy |

`deploy/coach-mcp.service` local reflète ces chemins réels une fois adapté sur la VM — le fichier
qui compte est `/etc/systemd/system/coach-mcp.service` sur la VM elle-même, pas le template du
repo (un `git pull` ne le met pas à jour automatiquement).

## Mise à jour en production

Procédure pour synchroniser la VM avec `main` après ajout, modification ou suppression d'un
tool (ou tout autre changement de code). Écrite pour être suivie de façon autonome.

### Procédure standard

```bash
ssh -i ~/.ssh/coach-ia-oracle.key ubuntu@coach-ia.duckdns.org
cd /home/ubuntu/Mon-Coach-IA

git status                          # vérifier l'absence de modifs locales inattendues avant de pull
git pull origin main

source venv/bin/activate
pip install -e ".[dev]"             # capte tout changement de dépendances (pyproject.toml)

pytest -q                           # NE PAS redémarrer le service si un test échoue — investiguer d'abord

sudo systemctl restart coach-mcp
sudo systemctl status coach-mcp --no-pager
journalctl -u coach-mcp -n 30 --no-pager   # vérifier l'absence d'erreur au démarrage
```

Si `git status` révèle des modifications locales inattendues sur la VM (ex. un fichier de
config adapté sur place comme `deploy/coach-mcp.service`), ne pas les écraser à l'aveugle — les
examiner avant de continuer. Vérifier aussi qu'aucun fichier sensible non prévu ne traîne dans
le dossier (cf. incident `.env.save` : une sauvegarde de `.env` sous un autre nom, non
gitignorée, à un `git add -A` de fuiter sur le repo public — supprimer ce genre de fichier dès
qu'il est repéré).

### Vérification post-déploiement

Tester en conditions réelles contre l'endpoint public plutôt que de se fier aux seuls tests
unitaires — les deux environnements (dev local vs VM prod) peuvent avoir des versions de
dépendances différentes (cf. incident `TargetType` ci-dessous), donc un test qui passe en local
ne garantit pas un comportement identique en prod.

```python
# Client MCP réel (SDK mcp, transport streamable HTTP) :
#   from mcp import ClientSession
#   from mcp.client.streamable_http import streamable_http_client
# URL = f"https://coach-ia.duckdns.org/mcp/{secret}"
#   où `secret` est le MCP_SECRET_PATH RÉEL de la VM — récupéré via SSH
#   (grep '^MCP_SECRET_PATH=' .env | cut -d= -f2 sur la VM), jamais celui du .env local
#   (les deux diffèrent), et jamais affiché en clair dans une conversation.
```

Appeler `list_tools()` pour confirmer que les tools attendus sont exposés (compter, vérifier la
présence des nouveaux/absence des supprimés), puis un appel `dry_run=True` (pour
`garmin_push_workout`) ou en lecture pure (`garmin_get_*`) avant tout test impliquant une
écriture réelle sur le compte Garmin.

### Piège connu : dérive de version entre environnements

Le poste de dev local (Python 3.10) et la VM de prod (Python 3.12+) peuvent installer des
versions différentes des dépendances : `garminconnect>=0.3.2` autorise aussi bien 0.3.2 en local
(plafonné par la contrainte Python de versions plus récentes de la lib) que 0.3.9 en prod. Un
changement d'API mineur côté lib tierce entre ces versions peut casser silencieusement en prod
sans qu'aucun test local ne le détecte — c'est exactement ce qui s'est produit avec
`garminconnect.workout.TargetType` (renommage d'attributs entre 0.3.2 et 0.3.9, cf. [Écarts
volontaires](#écarts-volontaires-par-rapport-à-la-spec-initiale)).

**Toujours lancer `pytest` sur la VM elle-même après `pip install`, avant de redémarrer le
service** — c'est ce qui a permis de trouver ce bug avant tout impact utilisateur plutôt
qu'après un redémarrage à l'aveugle.

### Ajouter/modifier/supprimer un tool — checklist

1. Implémenter/modifier le tool dans `src/tools/garmin_tools.py` ou `operational_tools.py`
   (et `src/garmin_client.py`/`src/workout_builder.py` si de la logique de parsing/construction
   est impliquée), l'enregistrer (ou le retirer) dans le dict retourné par
   `build_garmin_tools`/`build_operational_tools`.
2. Ajouter/mettre à jour les tests correspondants (`tests/test_garmin_tools.py` ou
   `tests/test_workout_builder.py`), avec mock de `GarminClient` — jamais `garminconnect` en
   direct.
3. `pytest` en local — tout doit passer.
4. Si le tool touche un endpoint Garmin dont la forme réelle n'est pas encore connue, valider
   contre un vrai compte avant de figer le parsing (via un script ponctuel type
   `inspect_new_endpoints.py`, ou en conditions réelles sur le serveur de dev local).
5. Mettre à jour ce README : la table des [tools exposés](#tools-mcp-exposés), et la section
   [Écarts volontaires](#écarts-volontaires-par-rapport-à-la-spec-initiale) si le comportement
   réel diffère de ce qui était supposé.
6. `git add`, commit, push sur `main` (ou via une branche + PR pour un changement plus large).
7. Suivre la [procédure standard](#procédure-standard) ci-dessus pour déployer sur la VM —
   `pytest` sur la VM avant redémarrage, jamais après à l'aveugle.
8. Test de fumée sur l'endpoint public (cf. [Vérification post-déploiement](#vérification-post-déploiement)).
